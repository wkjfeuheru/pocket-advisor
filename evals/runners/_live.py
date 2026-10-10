"""Live 评测共享设施：真实模型 + 假数据源。

live 装配贴近生产链路（``app/agents/runtime.py``）：
- 真实 ``build_chat_model``（ADVISOR_MODEL_*）驱动真实 prompt 与 function-calling；
- 行情走 ``FakeFuyaoOperations``（确定性数据）+ 内存证据库，不触网；
- 中断不打断评测：澄清/配置类 interrupt 用脚本化 resume 回答。

无 ``ADVISOR_MODEL_API_KEY`` 时 ``build_live_model`` 抛 ``LiveSkip``，
runner 捕获后输出 SKIP 状态而不是失败。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.graphs.task_agent import build_advisor_graph
from app.agents.stock.evidence_store import MemoryAgentEvidenceStore
from app.core.settings_models import load_advisor_settings
from evals.runners._fakes import FakeFuyaoOperations


class LiveSkip(RuntimeError):
    """live 评测前置条件不满足（如未配置模型密钥）。"""


def build_live_model():
    """构造真实聊天模型；未配置密钥时抛 LiveSkip。"""
    settings = load_advisor_settings()
    if not settings.api_key:
        raise LiveSkip("ADVISOR_MODEL_API_KEY 未配置，live 评测跳过")
    from app.agents.model import build_chat_model

    return build_chat_model(settings)


class FakeProductLibrary:
    """商品库替身：几个固定代码与风险等级，供 suitability_guard 使用。"""

    _RISKS = {"110011": 2, "110022": 3, "000001": 1, "HIGH01": 5}

    def query_by_code(self, code: str) -> dict[str, Any] | None:
        rank = self._RISKS.get(str(code).upper())
        if rank is None:
            return None
        return {"basic_info": {"risk_level": f"R{rank}"}}


class LiveHarness:
    """一轮 live 请求的驱动器：真实主图 + 假数据源 + 脚本化 resume。"""

    def __init__(self, *, profile: dict[str, Any] | None = None) -> None:
        self.model = build_live_model()
        self.evidence_store = MemoryAgentEvidenceStore()
        self.profile = profile or {"risk_level": "R3", "investment_horizon": "中期",
                                   "investment_goal": "均衡增长", "liquidity_need": "中",
                                   "budget_amount": 10000}
        self.interrupts: list[dict[str, Any]] = []
        self.checkpointer = MemorySaver()
        self._graph = build_advisor_graph(
            model=self.model,
            specialists=self._specialists(),
            checkpointer=self.checkpointer,
            risk_of=FakeProductLibrary().query_by_code,
        )

    def _specialists(self) -> dict[str, Any]:
        # live 路由评测聚焦 classify_intent 之前的链路，专业分支用回显替身：
        # 不影响路由指标，且避免四条分支同时调用真实模型拉高成本与时长。
        from evals.runners._fakes import fake_specialist

        return {name: fake_specialist(name) for name in
                ("stock_agent", "product_agent", "portfolio_agent", "chat_agent")}

    async def send(self, message: str, *, thread_id: str) -> dict[str, Any]:
        """跑一轮并返回观测结果：intents / resolved_task / interrupt / final_result。"""
        config = {"configurable": {"thread_id": thread_id}}
        await self._graph.ainvoke(
            {"messages": [{"role": "user", "content": message}], "profile_snapshot": dict(self.profile)},
            config,
            durability="sync",
        )
        return await self._observe(thread_id)

    async def send_messages(self, messages: list[dict[str, str]], *, thread_id: str) -> dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}
        await self._graph.ainvoke(
            {"messages": list(messages), "profile_snapshot": dict(self.profile)},
            config,
            durability="sync",
        )
        return await self._observe(thread_id)

    async def resume(self, answer: dict[str, Any] | Any, *, thread_id: str) -> dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}
        await self._graph.ainvoke(Command(resume=answer), config, durability="sync")
        return await self._observe(thread_id)

    async def _observe(self, thread_id: str) -> dict[str, Any]:
        snapshot = await self._graph.aget_state({"configurable": {"thread_id": thread_id}})
        values = getattr(snapshot, "values", None) or {}
        payload: dict[str, Any] = {
            "intents": [dict(item) if hasattr(item, "model_dump") else dict(item)
                        for item in values.get("intents") or []],
            "resolved_task": str(values.get("resolved_task") or ""),
            "final_result": str(values.get("final_result") or ""),
            "interrupt": None,
        }
        direct = getattr(snapshot, "interrupts", None) or ()
        found = direct or next(
            (getattr(task, "interrupts", None) or () for task in getattr(snapshot, "tasks", ()) or ()),
            (),
        )
        if found:
            value = getattr(found[0], "value", found[0])
            if isinstance(value, dict):
                payload["interrupt"] = value
                self.interrupts.append(value)
        return payload


def observed_agents(observation: dict[str, Any]) -> list[str]:
    """从 intents 提取实际路由的 agent 集合（保持原序去重）。"""
    seen: list[str] = []
    for item in observation.get("intents") or []:
        agent = str(item.get("agent") or "")
        if agent and agent not in seen:
            seen.append(agent)
    return seen


def observed_kinds(observation: dict[str, Any]) -> dict[str, str]:
    return {str(item.get("agent")): str(item.get("kind") or "general")
            for item in observation.get("intents") or []}


def observed_needs_budget(observation: dict[str, Any]) -> dict[str, bool]:
    return {str(item.get("agent")): bool(item.get("needs_budget"))
            for item in observation.get("intents") or []}


def fake_fuyao() -> FakeFuyaoOperations:
    return FakeFuyaoOperations()
