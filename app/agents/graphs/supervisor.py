"""受限的任务调度器：领域准备、依赖执行、串行确认及证据定稿。"""
from __future__ import annotations

import copy
import json
import logging
import re
import hashlib
from pydantic import ValidationError
from typing import Any

from anyio import to_thread
from langgraph.config import get_config, get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from app.agents.artifacts import artifact_for_result
from app.agents.compliance_guard import StreamingGuard, render_fact, numeric_claims
from app.agents.public_warnings import public_warnings
from app.agents.graphs.state import (
    AGENT_NAMES, PROFILE_FIELDS, RESULT_KEY, RESULT_MODEL, AdvisorState,
    AnswerPlan, FinalAnswer, Intent, IntentResult, PlanPoint, PlanSection,
    PortfolioResult, RewrittenQuery, StockResult, SuitabilityResult, SupervisorDecision,
    TaskSpec, WorkerBase, WorkerOutcome, WorkerRequest, validate_task_plan, worker_failure,
)
from app.agents.prompts.task_agent_prompts import CLASSIFY_SYSTEM_PROMPT, RESOLVE_SYSTEM_PROMPT
from app.services.portfolio_service import SIMULATED_DISCLAIMER

logger = logging.getLogger(__name__)
_FOLLOWUP = re.compile(r"^(?:它|这只|那只|看近|看最近|再看|再加|同样|清除|取消|改成|换成)|上面|刚才|^(?:短期|中期|长期|价值|成长|趋势|近一年|近三个月)$")
_FORBIDDEN = re.compile(r"保证收益|稳赚|必赚|保收益|承诺收益|建议(?:立即)?(?:买入|卖出|加仓|减仓)|目标价|买卖点")


def text_of(message):
    return str(message.get("content") or "") if isinstance(message, dict) else str(getattr(message, "content", "") or "")


def latest_results(state) -> dict[str, WorkerOutcome]:
    latest = {}
    for raw in (state.get("outcomes") or {}).values():
        item = raw if isinstance(raw, WorkerOutcome) else WorkerOutcome.model_validate(raw)
        if item.task_id not in latest or latest[item.task_id].attempt < item.attempt:
            latest[item.task_id] = item
    return latest


def profile_of(state):
    trusted = (get_config().get("configurable") or {}).get("verified_profile")
    return dict(trusted if trusted is not None else state.get("profile_snapshot") or {})


async def structured(model, schema, messages):
    for attempt in range(2):
        try:
            result = await model.with_structured_output(schema, method="function_calling").ainvoke(messages, get_config())
            return result if isinstance(result, schema) else schema.model_validate(result)
        except ValidationError as exc:
            fields = [".".join(str(part) for part in err["loc"]) for err in exc.errors(include_input=False, include_context=False)]
            logger.warning("advisor_schema_invalid schema=%s fields=%s", schema.__name__, fields)
            if attempt:
                raise
            messages = [*messages, {"role": "system", "content": "上次结构化返回不符合声明的schema。请严格使用枚举值与字段类型；不得添加字段。需要修正的字段：" + "、".join(fields)}]


def build_supervisor_graph(*, model, specialists, checkpointer=None, risk_of=None,
                           context_limit=20, allocation_runner=None, slot_resolvers=None,
                           max_batches=3, max_tasks=8):
    from app.agents.graphs.task_agent import AllBranchesFailed, is_degradable, risk_rank
    from app.agents.slots import extract_slots, structured_task

    resolvers = slot_resolvers or {}

    def tasks_of(state):
        return [item if isinstance(item, TaskSpec) else TaskSpec.model_validate(item)
                for item in state.get("task_specs") or []]

    def active_context(context, agent):
        bucket = (context.get("slots") or {}).get(agent) or {}
        key = bucket.get("active_context_id")
        return key or "", copy.deepcopy((bucket.get("contexts") or {}).get(key) or {})

    async def plan(state):
        history = list(state.get("messages") or [])[-max(context_limit, 1):]
        query = state.get("user_query") or (text_of(history[-1]) if history else "")
        context = copy.deepcopy(state.get("session_context") or {"version": 2, "slots": {}, "pending_tasks": []})
        pending = context.get("pending_tasks") or []
        resolved = query
        if len(history) > 1:
            try:
                rewritten = await structured(model, RewrittenQuery, [
                    {"role": "system", "content": RESOLVE_SYSTEM_PROMPT}, *history,
                ])
                resolved = rewritten.task or query
            except Exception as exc:
                if not is_degradable(exc):
                    raise
        routing = CLASSIFY_SYSTEM_PROMPT + "\n每任务填写唯一 task_id；dependencies 只引用本计划 task_id。最多八项。主控不提取领域槽位。存在待办时填写pending_action：用户补充当前所问字段为continue，明确换话题为switch，不确定为unclear；不要把简短补答误判闲聊。"
        # 数据库保留上下文历史，路由模型只需要当前活动槽位和待办，避免随会话无限膨胀。
        routing_context = {"slots": {agent: active_context(context, agent)[1]
                                      for agent in AGENT_NAMES if agent != "chat_agent"},
                           "pending_tasks": pending}
        routing += "\n当前领域槽位与待补充任务（用于理解追问，不是事实证据）：" + json.dumps(routing_context, ensure_ascii=False, default=str)
        result = await structured(model, IntentResult, [
            {"role": "system", "content": routing}, *history,
            {"role": "user", "content": f"当前问题：{query}\n结合历史的问题：{resolved}"},
        ])
        intents = result.intents or [Intent(agent="chat_agent", task=query)]
        tasks = [TaskSpec(**{**item.model_dump(), "task_id": item.task_id or f"task-{index + 1}"})
                 for index, item in enumerate(intents)]
        deferred_prepared = {}
        # 未指定领域的短补充只能继续明确的单项待办，不能凭空新建执行任务。
        if pending and result.pending_action in {"continue", "unclear"} and not re.search(r"取消.*(?:刚才|任务|选股|配置)|换个话题|不(?:用|要)继续", query):
            first = pending[0]
            first_task = TaskSpec.model_validate(first["task"])
            first_task.pending_fields = list(first.get("missing") or [])
            first_task.defer_input = False
            if tasks[0].agent in {first_task.agent, "chat_agent"}:
                # 保留待办的前置任务，但不搬运上轮证据；本轮重新执行必要依赖。
                restored = first.get("dependency_tasks") or []
                tasks = [TaskSpec.model_validate({**item, "defer_input": False, "refresh_only": True}) for item in restored] + [first_task]
                active_ids = {t.task_id for t in tasks}
                for item in pending[1:]:
                    for ancestor in item.get("dependency_tasks") or []:
                        if ancestor["task_id"] not in active_ids:
                            tasks.append(TaskSpec.model_validate({**ancestor, "defer_input": True, "refresh_only": True}))
                    deferred = TaskSpec.model_validate({**item["task"], "pending_fields": item.get("missing") or [], "defer_input": True})
                    tasks.append(deferred)
                    bucket = (context.get("slots") or {}).get(deferred.agent) or {}
                    record = (bucket.get("contexts") or {}).get(deferred.context_id) or {}
                    deferred_prepared[deferred.task_id] = {"slots": record.get("slots") or {},
                        "missing": item.get("missing") or [], "question": item.get("question") or "请补充下一个任务的条件。",
                        "options": item.get("options") or [], "context_id": deferred.context_id, "agent": deferred.agent}
                tasks = list({t.task_id: t for t in tasks}.values())
                context["pending_tasks"] = []
        else:
            context["pending_tasks"] = []
        validate_task_plan(tasks, max_tasks=max_tasks)
        run_key = str((get_config().get("configurable") or {}).get("request_id")
                      or (getattr(history[-1], "id", "") if history else "")
                      or hashlib.sha256((query + str(len(history))).encode()).hexdigest()[:16])
        for task in tasks:
            existing_id, _ = active_context(context, task.agent)
            if not task.context_id:
                task.context_id = existing_id if _FOLLOWUP.search(query) and existing_id else f"{run_key}:{task.task_id}"
        return {"planned": True, "task_specs": tasks, "intents": result.intents,
                "resolved_task": resolved, "user_query": query, "session_context": context,
                "profile_snapshot": profile_of(state), "batch_count": 0,
                "decision_count": 0, "attempted_tasks": {}, "prepared": deferred_prepared}

    def slot_node(agent):
        async def prepare(state):
            task = state["active_task"]
            context = state.get("session_context") or {}
            _, prior = active_context(context, agent)
            # 明确保存的待办优先使用其自身上下文，避免另一个同域任务污染。
            bucket = ((context.get("slots") or {}).get(agent) or {}).get("contexts") or {}
            if task.context_id in bucket:
                prior = bucket[task.context_id]
            if task.refresh_only:
                from app.agents.slots import validate_slots
                slots = copy.deepcopy(prior.get("slots") or {})
                if agent == "portfolio_agent" and slots.get("operation") == "generate":
                    # 已采纳的前置动作不能因下游澄清在另一请求里再次执行。
                    slots["operation"] = "query"
                return {"prepared": {task.task_id: {"slots": slots, "missing": validate_slots(agent, slots),
                        "question": "前置任务条件需要重新确认。", "context_id": task.context_id, "agent": agent}}}
            if task.defer_input:
                from app.agents.slots import validate_slots
                slots = copy.deepcopy(prior.get("slots") or {})
                return {"prepared": {task.task_id: {"slots": slots, "missing": task.pending_fields or validate_slots(agent, slots),
                        "question": "请补充下一个任务的条件。", "context_id": task.context_id, "agent": agent}}}
            history = list(state.get("messages") or [])[-context_limit:]
            dependency_results = [latest_results(state)[key].result.model_dump() for key in task.dependencies]
            current_query = state.get("user_query") or task.task
            continuation = bool(_FOLLOWUP.search(current_query)) or task.context_id in bucket
            # 分类模型的任务描述不能成为新增用户参数或操作授权的来源。
            extraction_query = current_query if continuation or task.pending_fields else task.task
            info = await extract_slots(model, agent, extraction_query, history,
                                       prior.get("slots") or {}, source_message_id=str(getattr(history[-1], "id", "") if history else ""),
                                       pending_fields=task.pending_fields or None, source_query=current_query,
                                       dependency_results=dependency_results)
            resolver = resolvers.get(agent)
            if not info.get("missing") and resolver is not None:
                try:
                    resolved = await resolver(dict(info["slots"]))
                    if "slots" in resolved:
                        info.update(resolved)
                    else:
                        info["slots"] = resolved
                except Exception as exc:
                    if not is_degradable(exc):
                        raise
                    info.update(missing=["symbols"], question="标的解析暂不可用，请稍后重试。")
            # 每任务都有独立新 ID；只有提取器确认语义延续时沿用已存在的 ID。
            if info.get("continuation"):
                prior_id, _ = active_context(context, agent)
                info["context_id"] = task.context_id if task.context_id in bucket else prior_id or task.context_id
            else:
                info["context_id"] = task.context_id
            info["agent"] = agent
            info["source_message_id"] = str(getattr(history[-1], "id", "") if history else "")
            if agent == "portfolio_agent" and info["slots"].get("operation") == "generate":
                explicit = re.search(r"(?:生成|制定|做|给|提供|出).{0,12}(?:配置|调仓|再平衡).{0,6}(?:方案|建议)|(?:生成|制定|做).{0,6}(?:配置|调仓)|(?:配置|调仓|再平衡)方案", current_query)
                if "operation" in task.pending_fields:
                    explicit = explicit or re.fullmatch(r"(?:请|帮我|我要|我想|选择)?(?:生成方案|制定方案|generate)[。！!]?", current_query.strip())
                if not explicit:
                    info["slots"].pop("operation", None)
                    info.update(missing=["operation"], question="您希望先了解持仓风险，还是明确生成配置方案？")
            return {"prepared": {task.task_id: info}}
        return prepare

    def worker_node(agent):
        async def execute(state):
            task = state["active_task"]
            prepared = state["prepared"][task.task_id]
            latest = latest_results(state)
            dependencies = [latest[key].result.model_dump() for key in task.dependencies]
            request = WorkerRequest(task=task, slots=prepared["slots"], user_query=state["user_query"],
                                    profile=profile_of(state), dependencies=dependencies)
            worker_state = dict(state)
            task_text = task.task if agent == "chat_agent" else structured_task(agent, request.slots, task.task)
            worker_state.update(task_spec=task, domain_slots=request.slots, user_query=request.user_query,
                                profile_snapshot=request.profile, dependency_results=dependencies,
                                worker_request=request, allowed_dependency_task_ids=task.dependencies,
                                messages=[{"role": "user", "content": task_text}])
            config = get_config()
            configurable = {**config.get("configurable", {}), "task_id": task.task_id,
                            "dependency_task_ids": task.dependencies}
            clarification = None
            try:
                # child RunnableConfig is supplied explicitly via a RunnableLambda, never mutable global state.
                from langchain_core.runnables import RunnableLambda
                async def invoke_specialist(_):
                    return await specialists[agent](task_text, worker_state)
                result = await RunnableLambda(invoke_specialist).ainvoke(
                        None, {**config, "configurable": configurable})
                result = result if isinstance(result, RESULT_MODEL[agent]) else RESULT_MODEL[agent].model_validate(result)
            except Exception as exc:
                if not is_degradable(exc):
                    raise
                from app.agents.evidence import NeedsClarification
                if isinstance(exc, NeedsClarification):
                    clarification = dict(exc.payload)
                result = worker_failure(agent, "该任务暂不可用")
            attempt = state["attempted_tasks"][task.task_id]
            outcome = WorkerOutcome(task_id=task.task_id, agent=agent, attempt=attempt,
                                    result=result, clarification=clarification)
            return {"outcomes": {f"{task.task_id}:{attempt}": outcome}}
        return execute

    def synchronize_context(state, tasks, latest):
        context = copy.deepcopy(state.get("session_context") or {})
        context.setdefault("version", 2)
        buckets = context.setdefault("slots", {})
        pending = []
        by_id = {item.task_id: item for item in tasks}
        for task in tasks:
            info = (state.get("prepared") or {}).get(task.task_id)
            if not info or task.agent == "chat_agent":
                continue
            bucket = buckets.setdefault(task.agent, {"active_context_id": "", "contexts": {}})
            cid = info.get("context_id") or task.context_id
            task.context_id = cid
            bucket["contexts"][cid] = {"slots": info["slots"], "source_request_id":
                str((get_config().get("configurable") or {}).get("request_id") or ""),
                "source_message_id": info.get("source_message_id") or ""}
            bucket["active_context_id"] = cid
            clarification = latest.get(task.task_id).clarification if task.task_id in latest else None
            if info.get("missing") or clarification or task.defer_input:
                task.context_id = cid
                # transitively persist dependency definitions, never their old numeric results.
                ancestors = {}
                def collect(key):
                    for parent in by_id[key].dependencies:
                        collect(parent)
                        ancestors[parent] = by_id[parent].model_dump()
                collect(task.task_id)
                pending.append({"task": task.model_dump(), "missing": info.get("missing") or (clarification or {}).get("fields") or [],
                                "question": (clarification or {}).get("question") or info.get("question") or "请补充任务条件。",
                                "options": (clarification or {}).get("options") or info.get("options") or [],
                                "dependency_tasks": list(ancestors.values())})
        context["pending_tasks"] = pending
        return context

    async def supervisor(state):
        if not state.get("planned"):
            return Command(update=await plan(state), goto="supervisor")
        tasks = tasks_of(state)
        prepared = state.get("prepared") or {}
        latest = latest_results(state)
        absent = [task for task in tasks if not task.defer_input and task.task_id not in prepared and all(
            key in latest and latest[key].result.status == "ok" for key in task.dependencies)]
        if absent:
            sends = []
            chat = {}
            for task in absent:
                if task.agent == "chat_agent":
                    chat[task.task_id] = {"slots": {}, "missing": [], "context_id": task.context_id}
                else:
                    sends.append(Send(task.agent.replace("_agent", "_slot_extractor"), {**state, "active_task": task}))
            return Command(update={"prepared": chat}, goto=sends or "supervisor")
        attempts = dict(state.get("attempted_tasks") or {})
        warnings = list(state.get("turn_warnings") or [])
        context = synchronize_context(state, tasks, latest)
        actionable = [task for task in tasks if not task.defer_input and task.task_id in prepared and not prepared[task.task_id].get("missing")
                      and task.task_id not in latest and all(
                          key in latest and latest[key].result.status == "ok" for key in task.dependencies)]
        # Replanning is limited to feedback after real work, and cannot override dependency/budget gates.
        decisions = state.get("decision_count", 0)
        market_only_complete = bool(latest) and all(isinstance(item.result, StockResult)
            and item.result.result is not None and (
                item.result.result.scenario == "market_overview" and item.result.result.market_overview
                and (item.result.result.market_overview.get("indices") or item.result.result.market_overview.get("historical_indices"))
                or any(code in warning for warning in item.result.warnings for code in ("stock_snapshot_stale", "missing_screening_result")))
            for item in latest.values())
        if latest and not market_only_complete and decisions < state.get("batch_count", 0) and state.get("batch_count", 0) < max_batches:
            try:
                decision = await structured(model, SupervisorDecision, [
                    {"role": "system", "content": "检查任务覆盖和证据缺口。只提出原始问题明确要求的必要补查；不得把普通大盘问询扩展成资金流、融资或个股筛选。不要重复成功任务。数据源明确不支持的指标不再补查；partial但已有可用证据应直接汇总。补查必须填写retry_queries中的不同且可用的明确查询方法，不能只重复原问题。"},
                    {"role": "user", "content": json.dumps({"query": state["user_query"], "tasks": [t.model_dump() for t in tasks],
                        "results": {k: v.model_dump() for k, v in latest.items()}}, ensure_ascii=False, default=str)},
                ])
                for key in decision.retry_tasks:
                    item = latest.get(key)
                    task = next((t for t in tasks if t.task_id == key), None)
                    retry_query = str(decision.retry_queries.get(key) or "").strip()
                    if (item and task and item.result.status != "ok" and not item.clarification
                            and item.attempt < 2 and decision.reason.strip()
                            and retry_query and re.sub(r"[\W_]+", "", retry_query) != re.sub(r"[\W_]+", "", task.task)):
                        if all(d in latest and latest[d].result.status == "ok" for d in task.dependencies):
                            task.task = retry_query
                            actionable.append(task)
                if decision.additional_tasks:
                    additions = [TaskSpec(**{**t.model_dump(), "task_id": t.task_id or f"task-{len(tasks)+i+1}"})
                                 for i, t in enumerate(decision.additional_tasks)]
                    for task in additions:
                        run_id = str((get_config().get("configurable") or {}).get("request_id") or "feedback")
                        task.context_id = f"{run_id}:{task.task_id}"
                    validate_task_plan(tasks + additions, max_tasks=max_tasks)
                    return Command(update={"task_specs": tasks + additions, "decision_count": decisions + 1,
                                           "session_context": context}, goto="supervisor")
            except Exception as exc:
                if not is_degradable(exc):
                    raise
                warnings.append("反馈调度不可用，已按已验证依赖继续执行")
            decisions += 1
        generation = [t for t in actionable if t.agent == "portfolio_agent" and prepared[t.task_id]["slots"].get("operation") == "generate" and allocation_runner]
        readonly = [t for t in actionable if t not in generation]
        profile = profile_of(state)
        need_profile = [t for t in actionable if t.kind == "personalized" or t in generation
                        or prepared[t.task_id]["slots"].get("operation") == "recommend"
                        or prepared[t.task_id]["slots"].get("filter_by_profile")]
        missing = [field for field in PROFILE_FIELDS if need_profile and not profile.get(field)]
        if any(t.needs_budget for t in need_profile) and not profile.get("budget_amount"):
            missing.append("budget_amount")
        if missing:
            confirmed = interrupt({"kind": "profile", "missing_fields": missing, "profile": profile})
            if not isinstance(confirmed, dict) or any(not confirmed.get(field) for field in missing):
                raise ValueError("画像确认不完整")
            profile.update(confirmed)
        if actionable and state.get("batch_count", 0) < max_batches:
            selected = readonly or generation[:1]
            for task in selected:
                attempts[task.task_id] = attempts.get(task.task_id, 0) + 1
                logger.info("advisor_task_dispatch request_id=%s task_id=%s agent=%s attempt=%s batch=%s dependencies=%s",
                            (get_config().get("configurable") or {}).get("request_id", ""), task.task_id,
                            task.agent, attempts[task.task_id], state.get("batch_count", 0) + 1, task.dependencies)
            update = {"batch_count": state.get("batch_count", 0) + 1,
                      "attempted_tasks": attempts, "decision_count": decisions,
                      "task_specs": tasks, "profile_snapshot": profile, "session_context": context, "turn_warnings": warnings}
            if not readonly and generation:
                update["active_task"] = generation[0]
                return Command(update=update, goto="allocation_confirm")
            return Command(update=update, goto=[Send(t.agent, {**state, **update, "active_task": t}) for t in selected])
        if actionable:
            warnings.append("执行预算已耗尽，部分任务尚未完成")
        skipped = [t.task for t in tasks if not t.defer_input and t.task_id not in latest and not prepared.get(t.task_id, {}).get("missing")]
        warnings.extend(f"未完成：{text}" for text in skipped)
        if latest and all(item.result.status == "failed" for item in latest.values()) and not context["pending_tasks"]:
            raise AllBranchesFailed("全部专业分支失败")
        # Legacy single-result fields are a public-artifact compatibility projection, never the task ledger.
        projection = {key: None for key in RESULT_KEY.values()}
        for task in tasks:
            if task.task_id in latest:
                projection[RESULT_KEY[task.agent]] = latest[task.task_id].result
        return Command(update={**projection, "session_context": context, "turn_warnings": list(dict.fromkeys(warnings)),
                               "profile_snapshot": profile, "decision_count": decisions, "task_specs": tasks}, goto="suitability_guard")

    async def allocation_confirm(state):
        task = state["active_task"]
        config = get_config()
        cfg = config.get("configurable") or {}
        trusted_state = {**state, "task_spec": task, "domain_slots": state["prepared"][task.task_id]["slots"],
                         "profile_snapshot": profile_of(state), "request_id": cfg.get("request_id"),
                         "session_id": cfg.get("session_id"), "task_id": task.task_id}
        try:
            result = await to_thread.run_sync(lambda: allocation_runner(
                customer_id=str(cfg.get("customer_id") or ""), state=trusted_state, interrupt=interrupt))
            result = result if isinstance(result, PortfolioResult) else PortfolioResult.model_validate(result)
        except Exception as exc:
            if not is_degradable(exc):
                raise
            result = worker_failure("portfolio_agent", "配置方案暂不可用")
        attempt = state["attempted_tasks"][task.task_id]
        return {"outcomes": {f"{task.task_id}:{attempt}": WorkerOutcome(task_id=task.task_id,
                agent=task.agent, attempt=attempt, result=result)}}

    def suitability_guard(state):
        rank = risk_rank(str(profile_of(state).get("risk_level") or ""))
        allowed, blocked = [], []
        for item in latest_results(state).values():
            if item.result.status == "failed":
                continue
            for code in getattr(item.result, "recommended_codes", []):
                product_rank = risk_of(code) if risk_of else None
                if isinstance(product_rank, int) and rank is not None and product_rank <= rank:
                    allowed.append(code)
                else:
                    blocked.append(code)
        return {"suitability": SuitabilityResult(allowed=list(dict.fromkeys(allowed)), blocked=list(dict.fromkeys(blocked)))}

    _PLAN_PROMPT = (
        "你是投研回答的骨架规划器，只输出结构化骨架，不要写成品文字。"
        "每个要点用一句话说明\u201c要讲什么\u201d，并在 fact_keys 中列出该要点需要引用的 facts 键。"
        "骨架不得写无引用的业务数值或证券代码；允许列表编号和分析参数（五日均线、近一季、近一年等）。"
        "推荐代码只能放在 recommendations 字段，且只能取自\u201c允许推荐\u201d列表。"
        "覆盖原始问题、参数限制与未完成项；不得把历史或缺失数据当作当前事实。"
        "涉及数据局限时用\u201c本次口径…，未覆盖…\u201d的陈述式表达，不使用\u201c无法/不能\u201d等否定式措辞。"
    )
    _ANSWER_PROMPT = (
        "你是中文投研写作助手。严格依据给定的回答骨架（sections）与可用事实（facts）成文。规则："
        "1) 行情、收益、估值等业务数值必须写成对应事实占位符（形如 {{key|metric}}），逐字复制；"
        "2) 允许列表编号、分析周期和定性叙述（五日均线、近一季、四舍五入等），不允许编造业务数据；"
        "占位符仅展开数值与单位，不要在它后面再写单位；标的名称、指标名称在句中正常书写。"
        "优先使用subject_name中的真实标的名称；引用必须匹配当前所述指标，不得把现价引用用于ROE等其他指标；日期由服务端统一追加，不要自行书写；"
        "3) 不得出现\u201c保证收益/稳赚/必赚/目标价/买卖点/建议买入/卖出/加仓/减仓\u201d等交易指令或收益承诺；"
        "4) 严格按骨架的小节顺序与要点组织，不新增要点或事实；"
        "5) 先直接回答用户问题，再给依据和局限；数据缺口合并为简短说明，不堆积告警或英文错误码。"
        "描述数据覆盖范围与局限时使用陈述式：先说明本次数据的口径与截止时间，再说明未覆盖的范围"
        "（如\u201c本次数据口径为最新交易日快照，未覆盖近一季、近一年等区间序列\u201d）；"
        "不使用\u201c无法/不能/无法据此\u201d等否定式措辞，也不用\u201c缺失/不足\u201d做开头。"
        "使用中文业务名称，不复述字段名或工具名。只输出回答正文，不要复述本提示。"
    )

    def _plan_text_invalid(text: str, facts: dict) -> bool:
        """骨架文本不得出现数字、中文数词或禁语。"""
        labels = [str(fact.get("subject_name") or "") for fact in facts.values()
                  if fact.get("subject_name") and fact.get("subject_name") != fact.get("subject")]
        return bool(_FORBIDDEN.search(text) or numeric_claims(text, labels=labels))

    def _validate_plan(plan: AnswerPlan, facts: dict, allowed: set[str]) -> tuple[AnswerPlan, list[str]]:
        """骨架的**字段级**校验：剔除越权事实键与推荐码，丢弃含数字/禁语的字段。

        与旧的\u201c整篇重新生成\u201d不同，这里只做局部修补，因此可以放在隐藏阶段，
        用户看不到任何重试痕迹，也就为后续的增量流式输出留出了空间。
        """
        problems: list[str] = []
        sections: list[PlanSection] = []
        for section in plan.sections:
            heading = str(section.heading or "").strip()
            if heading and _plan_text_invalid(heading, facts):
                problems.append("heading_invalid")
                heading = ""
            points: list[PlanPoint] = []
            for point in section.points:
                text = str(point.point or "").strip()
                keys = list(dict.fromkeys(str(key) for key in point.fact_keys))
                valid_keys = [key for key in keys if key in facts]
                if len(valid_keys) != len(keys):
                    problems.append("unknown_fact_reference")
                if not text or _plan_text_invalid(text, facts):
                    problems.append("point_invalid")
                    continue
                points.append(PlanPoint(point=text, fact_keys=valid_keys))
            if heading or points:
                sections.append(PlanSection(heading=heading, points=points))
        declared = [str(code) for code in plan.recommendations]
        recommendations = [code for code in dict.fromkeys(declared) if code in allowed]
        if len(recommendations) != len(set(declared)):
            problems.append("recommendation_not_allowed")
        return AnswerPlan(sections=sections, recommendations=recommendations), problems

    async def build_plan(payload: dict, facts: dict, allowed: set[str]) -> AnswerPlan:
        """隐藏阶段：产出并校验骨架，最多一次带反馈的局部修补（不重写整篇、不入用户流）。"""
        feedback = ""
        plan = AnswerPlan()
        for _ in range(2):
            try:
                candidate = await structured(model, AnswerPlan, [
                    {"role": "system", "content": _PLAN_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str) + "\n" + feedback},
                ])
            except Exception as exc:
                if not is_degradable(exc):
                    raise
                return plan
            plan, problems = _validate_plan(candidate, facts, allowed)
            if not problems:
                return plan
            feedback = "上一版骨架存在以下问题，请修正：" + "、".join(sorted(set(problems)))
        logger.warning("advisor_plan_corrected reasons=%s", sorted(set(problems)))
        return plan

    async def render_answer(plan: AnswerPlan, facts: dict, payload: dict) -> tuple[str, list[str]]:
        """可见阶段：在骨架约束下做受约束的自然语言**流式**成文，并逐块过滤。

        已过滤增量通过 LangGraph 的 custom 流下发（``writer``），同时累积为权威文本，
        因此 `"".join(tokens) == final.answer` 恒成立。
        """
        if not plan.sections:
            return "", []
        guard = StreamingGuard(facts)
        writer = get_stream_writer()
        fact_view = {key: {"placeholder": fact["placeholder"], "subject": fact.get("subject"),
                          "subject_name": fact.get("subject_name") or fact.get("subject"),
                          "metric": fact.get("metric"), "as_of": fact.get("as_of"), "rendered": render_fact(fact)}
                     for key, fact in facts.items()}
        messages = [
            {"role": "system", "content": _ANSWER_PROMPT},
            {"role": "user", "content": json.dumps(
                {"original_query": payload["original_query"],
                 "sections": [section.model_dump() for section in plan.sections],
                 "facts": fact_view, "missing": payload["missing"]},
                ensure_ascii=False, default=str)},
        ]
        parts: list[str] = []
        async for chunk in model.astream(messages, get_config()):
            delta = guard.push(text_of(chunk))
            if delta:
                writer({"text": delta})
                parts.append(delta)
        tail = guard.finalize()
        if tail:
            writer({"text": tail})
            parts.append(tail)
        return "".join(parts), guard.warnings()

    async def compliance_node(state):
        latest = latest_results(state)
        pending = state["session_context"].get("pending_tasks") or []
        if not latest and pending:
            text = pending[0]["question"] + "\n\n" + SIMULATED_DISCLAIMER
            get_stream_writer()({"text": text})
            return {"final": FinalAnswer(answer=text)}
        facts = {}
        materials = []
        for task_id, item in latest.items():
            result = item.result
            names = {}
            def collect_names(value):
                if isinstance(value, dict):
                    code = value.get("thscode") or value.get("code")
                    if code and value.get("name"):
                        names[str(code)] = str(value["name"])
                    for nested in value.values():
                        collect_names(nested)
                elif isinstance(value, list):
                    for nested in value:
                        collect_names(nested)
            collect_names(result.model_dump())
            for index, fact in enumerate(result.facts):
                key = f"{task_id}:{index}"
                # 占位符原文随事实下发，避免模型自行拼写 task-id/index 之类的模板字样。
                facts[key] = {**fact, "subject_name": names.get(fact.get("subject"), fact.get("subject")),
                              "placeholder": "{{" + key + "|" + str(fact.get("metric") or "") + "}}"}
            public_result = result.model_dump()
            public_result["warnings"] = public_warnings(result.warnings)
            materials.append({"task_id": task_id, "result": public_result, "artifact": artifact_for_result(result)})
        payload = {"original_query": state["user_query"], "tasks": [t.model_dump() for t in tasks_of(state)],
                   "slots": state.get("prepared"), "results": materials, "facts": facts,
                   "profile": profile_of(state), "suitability": state["suitability"].model_dump(),
                   "missing": public_warnings(state.get("turn_warnings") or [])}
        allowed = set(state["suitability"].allowed)

        # 隐藏阶段：骨架（不产出任何用户可见 token）。
        plan = await build_plan(payload, facts, allowed)
        # 可见阶段：受约束流式成文（已过滤增量经 custom 流下发）。
        prose, blocked = await render_answer(plan, facts, payload)
        if blocked:
            # 过滤器拦截属于安全兜底，只记日志；不把内部英文码当用户可见告警下发。
            logger.warning("advisor_answer_blocked request_id=%s reasons=%s",
                           (get_config().get("configurable") or {}).get("request_id", ""), sorted(set(blocked)))

        text = prose
        if not text:
            text = "已完成可验证的数据查询，具体数据请查看结构化结果。" if latest else "需要补充任务条件后才能继续。"
            if facts:
                text += "\n" + "\n".join(render_fact(fact, detailed=True) for fact in facts.values())
        if pending:
            text += "\n\n" + pending[0]["question"]
        warnings = public_warnings(state.get("turn_warnings") or [])
        if warnings:
            text += "\n\n" + "\n".join(warnings)
        cutoffs = list(dict.fromkeys(item.result.as_of for item in latest.values() if item.result.facts and item.result.as_of))
        if cutoffs:
            text += "\n\n数据截止（接口记录）：" + "；".join(cutoffs)
        calendar_days = list(dict.fromkeys((item.result.result.market_overview or {}).get("last_calendar_trade_date")
            for item in latest.values() if isinstance(item.result, StockResult) and item.result.result is not None
            and item.result.result.scenario == "market_overview"))
        if any(calendar_days):
            text += "；交易日历最近交易日：" + "、".join(day for day in calendar_days if day)
        report_days = list(dict.fromkeys(str(fact.get("as_of") or "")[:10] for fact in facts.values()
            if fact.get("metric") in {"roe", "gross_margin", "leverage", "revenue_trend", "profit_trend"} and fact.get("as_of")))
        if report_days:
            text += "\n财务指标报告截止日：" + "、".join(report_days)
        # 推荐码行不进入用户可见文本（仅保留在 FinalAnswer.recommendations，供内部与评估使用）。
        if plan.recommendations:
            logger.info("advisor_recommendations request_id=%s count=%s",
                        (get_config().get("configurable") or {}).get("request_id", ""), len(plan.recommendations))
        text += "\n\n" + SIMULATED_DISCLAIMER
        # 把流式未覆盖的尾部（未完成项/告警/免责声明）作为最后一段 token 下发，
        # 保证 "".join(tokens) == final.answer，前端在 done 前后内容不跳变。
        remainder = text[len(prose):] if text.startswith(prose) else ""
        if remainder:
            get_stream_writer()({"text": remainder})
        return {"final": FinalAnswer(answer=text, recommendations=plan.recommendations)}

    graph = StateGraph(AdvisorState)
    graph.add_node("supervisor", supervisor)
    for agent in AGENT_NAMES:
        if agent != "chat_agent":
            name = agent.replace("_agent", "_slot_extractor")
            graph.add_node(name, slot_node(agent))
            graph.add_edge(name, "supervisor")
        graph.add_node(agent, worker_node(agent))
        graph.add_edge(agent, "supervisor")
    graph.add_node("allocation_confirm", allocation_confirm)
    graph.add_edge("allocation_confirm", "supervisor")
    graph.add_node("suitability_guard", suitability_guard)
    graph.add_node("compliance_node", compliance_node)
    graph.add_edge(START, "supervisor")
    graph.add_edge("suitability_guard", "compliance_node")
    graph.add_edge("compliance_node", END)
    return graph.compile(checkpointer=checkpointer)
