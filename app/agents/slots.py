"""领域槽位的严格边界、显式补丁和结构化抽取。

槽位只描述当前任务；用户画像始终由服务端提供，不能通过本模块更新。
"""

from __future__ import annotations

import calendar
import copy
import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


SHANGHAI = ZoneInfo("Asia/Shanghai")
_TIMEFRAME_EXPRESSION = re.compile(
    r"20\d{2}\s*(?:[Qq][1-4]|年?第?[一二三四1-4]季度)|"
    r"(?<![\dA-Za-z])\d+\s*[yYmM](?![A-Za-z])|(?:近|最近|过去)[一二两三四五六七八九十\d]+(?:年|个月|月|周|天|日)|"
    r"今年|去年|今天|今日|本月|\d{4}-\d{2}-\d{2}.*?\d{4}-\d{2}-\d{2}",
)
PROTECTED_FIELDS = frozenset({
    "risk_level", "investment_horizon", "investment_goal", "liquidity_need",
    "user_id", "profile", "profile_snapshot", "budget", "account_id",
})


class TimeRange(BaseModel):
    """包含首尾日期的历史观察范围，不代表用户的投资期限。"""

    model_config = ConfigDict(extra="forbid")
    start: str
    end: str
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"

    @model_validator(mode="after")
    def ordered_dates(self):
        if date.fromisoformat(self.start) > date.fromisoformat(self.end):
            raise ValueError("时间范围的开始日期不能晚于结束日期")
        return self


class SlotPatch(BaseModel):
    """set 替换数组，只有 append 显式追加；clear 移除指定字段。"""

    model_config = ConfigDict(extra="forbid")
    set_values: dict[str, Any] = Field(default_factory=dict)
    clear_fields: list[str] = Field(default_factory=list)
    append_values: dict[str, list[Any]] = Field(default_factory=dict)
    source_message_id: str = ""


class SlotExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["stock_agent", "product_agent", "portfolio_agent"] | None = None
    continuation: bool = False
    patch: SlotPatch = Field(default_factory=SlotPatch)


def normalize_timeframe(value: str | dict | TimeRange, *, now: datetime | None = None) -> TimeRange:
    """将 1y、3m、季度和中文历史范围转为上海日历日期。"""
    if isinstance(value, TimeRange):
        return value
    if isinstance(value, dict):
        return TimeRange.model_validate(value)
    now = now or datetime.now(SHANGHAI)
    today = now.replace(tzinfo=SHANGHAI).date() if now.tzinfo is None else now.astimezone(SHANGHAI).date()
    text = value.strip()
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if len(dates) == 2:
        return TimeRange(start=dates[0], end=dates[1])
    quarter = re.search(r"(20\d{2})\s*(?:[Qq]([1-4])|年?第?([一二三四1-4])季度)", text)
    if quarter:
        year = int(quarter.group(1))
        number = quarter.group(2) or quarter.group(3)
        quarter_number = int(number) if number.isdigit() else "一二三四".index(number) + 1
        first_month = (quarter_number - 1) * 3 + 1
        last_month = first_month + 2
        return TimeRange(start=date(year, first_month, 1).isoformat(),
                         end=date(year, last_month, calendar.monthrange(year, last_month)[1]).isoformat())
    recent_days = re.search(r"(?:近|最近|过去)\s*([一二两三四五六七八九十\d]+)\s*(周|天|日)", text)
    if recent_days:
        from datetime import timedelta
        raw, unit = recent_days.groups()
        count = int(raw) if raw.isdigit() else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(raw, 0)
        if count <= 0 or count > 365:
            raise ValueError("无法识别时间范围")
        return TimeRange(start=(today - timedelta(days=count * (7 if unit == "周" else 1))).isoformat(), end=today.isoformat())
    years = re.search(r"(\d+)\s*[yY]\b", text)
    months = re.search(r"(\d+)\s*[mM]\b", text)
    chinese = re.search(r"(?:近|最近|过去|前)?\s*([一二两三四六十\d]+)\s*(年|个月|月)", text)
    if years or months or chinese:
        if years:
            count, unit = int(years.group(1)), "年"
        elif months:
            count, unit = int(months.group(1)), "月"
        else:
            raw, unit = chinese.groups()
            count = int(raw) if raw.isdigit() else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "六": 6, "十": 10}.get(raw, 0)
        if count <= 0 or count > 100:
            raise ValueError("无法识别时间范围")
        total_months = today.year * 12 + today.month - 1 - count * (12 if unit == "年" else 1)
        year, month_index = divmod(total_months, 12)
        month = month_index + 1
        start = date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
        return TimeRange(start=start.isoformat(), end=today.isoformat())
    if "今年" in text:
        return TimeRange(start=date(today.year, 1, 1).isoformat(), end=today.isoformat())
    if "去年" in text:
        return TimeRange(start=date(today.year - 1, 1, 1).isoformat(), end=date(today.year - 1, 12, 31).isoformat())
    if "今天" in text or "今日" in text:
        return TimeRange(start=today.isoformat(), end=today.isoformat())
    if "本月" in text:
        return TimeRange(start=today.replace(day=1).isoformat(), end=today.isoformat())
    raise ValueError("无法识别时间范围")


def merge_slot_patch(existing: dict, patch: SlotPatch | dict, model_type: type[BaseModel]) -> dict:
    """校验后返回独立副本；未知字段、画像字段和非法类型均不能写入。"""
    patch = SlotPatch.model_validate(patch)
    touched = set(patch.set_values) | set(patch.clear_fields) | set(patch.append_values)
    if touched & PROTECTED_FIELDS:
        raise ValueError("领域槽位不能更新用户画像或账户字段")
    unknown = touched - set(model_type.model_fields)
    if unknown:
        raise ValueError(f"未知槽位字段: {', '.join(sorted(unknown))}")
    merged = copy.deepcopy(existing)
    for field in patch.clear_fields:
        merged.pop(field, None)
    merged.update(copy.deepcopy(patch.set_values))
    if isinstance(merged.get("timeframe"), str):
        merged["timeframe"] = normalize_timeframe(merged["timeframe"]).model_dump()
    for field, values in patch.append_values.items():
        previous = merged.get(field, [])
        if not isinstance(previous, list):
            raise ValueError(f"{field} 不是数组槽位")
        merged[field] = previous + [copy.deepcopy(value) for value in values if value not in previous]
        merged[field] = list(dict.fromkeys(merged[field])) if all(isinstance(v, str) for v in merged[field]) else merged[field]
    return model_type.model_validate(merged).model_dump(mode="json", exclude_unset=True)


def validate_slots(agent: str, slots: dict) -> list[str]:
    """返回业务必填槽位；类型错误由领域 schema 报错。"""
    typed = SLOT_MODELS[agent].model_validate(slots)
    if agent == "stock_agent":
        if typed.scenario is None:
            return ["scenario"]
        if typed.scenario == "equity_research":
            return [] if typed.symbols else ["symbols"]
        if typed.scenario == "stock_screening":
            return [name for name in ("strategy", "horizon") if getattr(typed, name) is None]
        return []
    if typed.operation is None:
        return ["operation"]
    if agent == "product_agent":
        objects = list(dict.fromkeys(typed.symbols + typed.codes))
        if typed.operation == "compare":
            return [] if len(objects) >= 2 else ["symbols"]
        if typed.operation == "query" and not objects and typed.product_type is None:
            return ["symbols"]
        if typed.operation == "recommend" and typed.product_type is None:
            return ["product_type"]
    return []


def structured_task(agent: str, slots: dict, query: str) -> str:
    """worker 获得自包含任务，仍由工具解析名称、查询证据和读取权威账户。"""
    typed = SLOT_MODELS[agent].model_validate(slots)
    payload = json.dumps(typed.model_dump(mode="json", exclude_unset=True), ensure_ascii=False)
    return f"用户当前请求：{query}\n已确认的任务条件：{payload}\n请遵守以上条件；标的名称须经工具解析，时间范围只表示历史观察区间。"


def _obvious_followup(query: str) -> bool:
    if re.fullmatch(r"\s*(?:看|查看|看下|看看)\s*(?:近|最近|过去)[一二两三四五六七八九十\d]+(?:年|个月|月|周|天|日)(?:的?(?:走势|表现|行情|技术面))?\s*[。！!]?", query):
        return True
    if re.fullmatch(r"\s*(?:短期|中期|长期|短线|中线|长线|价值(?:质量)?|成长(?:质量)?|趋势(?:动量)?|近一年|近三个月|\d+[yYmM])\s*[。！!]?", query):
        return True
    return bool(re.search(r"它|这只|那只|这些|刚才|上述|上面|同样|相同|继续|再看|改看|换成|改成|再加|加上|追加|去掉|不要|清除", query))


def _timeframe_from_query(query: str) -> TimeRange | None:
    # 投资期限的「长期」不得当作历史区间；金额、代码中的数字也不能命中。
    expression = _TIMEFRAME_EXPRESSION.search(query)
    return normalize_timeframe(expression.group()) if expression else None


def _raw_objects(query: str, agent: str) -> list[str]:
    codes = re.findall(r"(?<!\d)\d{6}(?:\.(?:SH|SZ|BJ))?(?!\d)", query, re.IGNORECASE)
    if codes:
        return list(dict.fromkeys(codes))
    # 降级时只截取明确位于操作动词后的原文对象，绝不查询或猜测证券代码。
    text = re.sub(r"(?:按)?(?:同样|相同)的?分析", "分析", query)
    match = re.search(r"(?:分析|研究|比较|对比|查询|看看|看一下|看下|再看|看|换成|再加|加上|追加)\s*(.+)", text)
    if not match:
        match = re.search(r"^(.+?)(?:怎么样|的估值|的走势|的收益|估值高吗)", text)
    if not match:
        return []
    object_text = match.group(1)
    time_expression = _TIMEFRAME_EXPRESSION.search(object_text)
    if time_expression:
        object_text = object_text[:time_expression.start()]
    candidate = re.split(r"[，,。？?]|的(?:基本面|技术面|估值|收益|走势|表现)|(?:短期|中期|长期)", object_text)[0].strip()
    objects = [item.strip() for item in re.split(r"和|与|及|、|对比", candidate) if item.strip()]
    generic = {"它", "这只", "那只", "这只基金", "那只基金", "基金", "股票", "大盘", "持仓", "市场", "指数", "怎么调整持仓", "我的持仓", "近一年", "近三个月", "其中股票", "其中的股票", "持仓里的股票"}
    metrics = {"ROE", "ROA", "RSI", "MACD", "PE", "PB", "EPS", "估值", "收益", "回撤", "波动", "持仓"}
    return [item for item in objects if item not in generic and item.upper() not in metrics and not re.search(r"选|推荐|调整|配置|怎么|为什么|哪些|如何", item)]


def _fallback_patch(agent: str, query: str, source_message_id: str) -> SlotExtraction:
    values: dict[str, Any] = {}
    timeframe = _timeframe_from_query(query)
    if timeframe:
        values["timeframe"] = timeframe.model_dump()
    symbols = _raw_objects(query, agent)
    if symbols:
        values["symbols"] = symbols
    if agent == "stock_agent":
        if re.search(r"选股|筛选|选.{0,6}股票|候选池", query):
            values["scenario"] = "stock_screening"
        elif re.search(r"大盘|市场概况|市场情绪|涨跌家数|指数|股市行情|沪深300|中证500|中证1000|科创50|深证成指|创业板指", query):
            values["scenario"] = "market_overview"
            if not re.search(r"\d{6}|上证|深证|沪深|创业板|中证|科创", query):
                values.pop("symbols", None)
        elif symbols or re.search(r"分析|研究|基本面|技术面|估值", query):
            values["scenario"] = "equity_research"
        for pattern, strategy in (("价值|低估|质量", "value_quality"), ("成长|增长", "growth_quality"), ("趋势|动量", "trend_momentum")):
            if re.search(pattern, query):
                values["strategy"] = strategy
        for pattern, horizon in (("短期|短线", "short"), ("中期|中线", "medium"), ("长期|长线", "long")):
            if re.search(pattern, query):
                values["horizon"] = horizon
        if "适合我" in query:
            values["filter_by_profile"] = True
    elif agent == "product_agent":
        if re.search(r"比较|对比", query):
            values["operation"] = "compare"
        elif re.search(r"推荐|挑选|选.{0,5}基金", query):
            values["operation"] = "recommend"
        elif not _obvious_followup(query) or symbols or re.search(r"查询|基金|ETF|etf|理财", query):
            values["operation"] = "query"
        for pattern, product_type in (("基金", "fund"), ("ETF|etf", "etf"), ("理财", "wealth")):
            if re.search(pattern, query):
                values["product_type"] = product_type
    else:
        values.pop("symbols", None)
        if re.search(r"生成|制定|做.{0,5}(?:配置|方案)|配置.{0,5}方案|重新配置", query):
            values["operation"] = "generate"
        elif re.search(r"诊断|风险|回撤|集中度|健康", query):
            values["operation"] = "diagnose"
        elif not re.search(r"调整|优化|怎么办|怎么", query) and (not _obvious_followup(query) or re.search(r"查询|持仓|账户", query)):
            values["operation"] = "query"
    metrics = [metric for metric in ("估值", "收益", "回撤", "波动", "持仓", "ROE", "ROA", "RSI", "MACD", "PE", "PB", "EPS") if metric in query.upper()]
    if metrics:
        values["metrics"] = metrics
    clear_fields = []
    if re.search(r"(?:不要|去掉|清除).{0,4}(?:时间|区间|时间范围)", query):
        clear_fields.append("timeframe")
    append_values = {}
    if symbols and re.search(r"再加|加上|追加", query):
        append_values["symbols"] = values.pop("symbols")
    if metrics and re.search(r"再看|再加|加上|追加", query):
        append_values["metrics"] = values.pop("metrics")
    return SlotExtraction(domain=agent, continuation=_obvious_followup(query), patch=SlotPatch(
        set_values=values, clear_fields=clear_fields, append_values=append_values,
        source_message_id=source_message_id,
    ))


def _dependency_context(results: list[dict]) -> tuple[list[dict], set[str]]:
    """前置结果由主控选取；只有结构化实体字段能授权新标的，叙事不行。"""
    summaries = []
    entities: set[str] = set()
    scalar_keys = {"symbol", "code", "product_code", "stock_code", "thscode", "ts_code"}
    array_keys = {"symbols", "codes", "product_codes", "recommended_codes"}

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in scalar_keys and isinstance(item, str):
                    entities.add(item)
                elif key in array_keys and isinstance(item, list):
                    entities.update(v for v in item if isinstance(v, str))
                elif isinstance(item, (dict, list)):
                    visit(item)
            if any(key in value for key in scalar_keys):
                for key in ("name", "stock_name", "product_name"):
                    if isinstance(value.get(key), str):
                        entities.add(value[key])
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for raw in results:
        if not isinstance(raw, dict) or raw.get("status", "ok") != "ok":
            continue
        summaries.append({key: copy.deepcopy(raw[key]) for key in ("summary", "facts", "data") if key in raw})
        for fact in raw.get("facts") or []:
            if isinstance(fact, dict) and isinstance(fact.get("subject"), str):
                entities.add(fact["subject"])
        visit(raw.get("data") or {})
    return summaries, entities


def _authorize_patch(agent: str, extraction: SlotExtraction, source: str, existing: dict,
                     dependency_entities: set[str], warnings: list[str]) -> None:
    """分类描述只是任务定位提示；值必须有当前原文、明确继承或前置数据支持。"""
    patch = extraction.patch
    inherited = existing if extraction.continuation and _obvious_followup(source) else {}
    rejected: set[str] = set()
    parsed_source = _fallback_patch(agent, source, "").patch.set_values
    for fields in (patch.set_values, patch.append_values):
        for field in ("symbols", "codes", "industries", "index_scope", "metrics", "limitations"):
            if field not in fields or not isinstance(fields[field], list):
                continue
            values = fields[field]
            authorized = [item for item in values if isinstance(item, str) and (
                item.casefold() in source.casefold() or item in (inherited.get(field) or [])
                or field in {"symbols", "codes"} and item in dependency_entities
            )]
            if len(authorized) != len(values):
                rejected.add(field)
            if authorized:
                fields[field] = authorized
            else:
                fields.pop(field, None)
    if "timeframe" in patch.set_values:
        try:
            requested = normalize_timeframe(patch.set_values["timeframe"])
            ranges = [normalize_timeframe(match.group()) for match in _TIMEFRAME_EXPRESSION.finditer(source)]
            if not any(requested == value for value in ranges) and patch.set_values["timeframe"] != inherited.get("timeframe"):
                patch.set_values.pop("timeframe")
                rejected.add("timeframe")
        except ValueError:
            # 非法值保留给 schema 边界报错，不能因此恢复旧范围。
            pass
    for field in ("strategy", "horizon", "product_type", "filter_by_profile"):
        if field in patch.set_values and patch.set_values[field] != parsed_source.get(field) and patch.set_values[field] != inherited.get(field):
            patch.set_values.pop(field)
            rejected.add(field)
    if rejected:
        warnings.append("已忽略缺乏用户原文或前置实体支持的任务条件：" + "、".join(sorted(rejected)))


def _clarification(agent: str, missing: list[str]) -> tuple[str, list[dict]]:
    if not missing:
        return "", []
    if agent == "portfolio_agent" and "operation" in missing:
        return "你希望查询持仓、诊断组合风险，还是生成配置方案？", [
            {"id": "query", "label": "查询持仓"}, {"id": "diagnose", "label": "诊断组合风险"},
            {"id": "generate", "label": "生成配置方案"},
        ]
    labels = {"scenario": "股票分析场景", "symbols": "明确的标的名称或代码（比较至少需要两个）",
              "strategy": "选股策略（价值质量、成长质量或趋势动量）", "horizon": "投资期限（短期、中期或长期）",
              "operation": "操作类型", "product_type": "产品类型（基金、ETF 或理财）"}
    return "请补充" + "、".join(labels.get(field, field) for field in missing) + "。", []


async def extract_slots(model, agent: str, query: str, history: list, existing: dict, *,
                        source_message_id: str = "", pending_fields: list[str] | None = None,
                        source_query: str | None = None, dependency_results: list[dict] | None = None) -> dict:
    """领域自有校验；模型只抽取 patch，合并与场景切换由确定性代码控制。"""
    model_type = SLOT_MODELS[agent]
    warnings: list[str] = []
    pending_fields = pending_fields or []
    source_query = query if source_query is None else source_query
    dependency_context, dependency_entities = _dependency_context(dependency_results or [])
    used_fallback = False
    schema_description = json.dumps(model_type.model_json_schema(), ensure_ascii=False)
    prompt = (
        f"你是 {agent} 的任务槽位抽取器，仅输出结构化补丁。合法槽位 schema：{schema_description}。"
        "只提取当前领域。任务槽位禁止修改用户画像、用户 ID、账户或预算；"
        "timeframe 是历史观察起止日期，horizon 是 short/medium/long 投资期限，二者独立。"
        f"今天上海日期为 {datetime.now(SHANGHAI).date().isoformat()}。"
        "标的保留原文，不要编造代码。数组默认 set 替换，用户明确追加才 append；删除用 clear。"
        "只有明确的代词、补充条件或继续同一分析才 continuation=true。新对象或新场景为新任务；"
        "用户明确要求同样的分析时可继承旧条件。无法确定操作或对象时省略对应字段，禁止猜测。"
        "query 是当前子任务的定位提示，不是用户授权或事实来源。仅从 source_query 原文、明确继承条件、"
        "dependency_results 的结构化实体和事实提取槽位；禁止复制 query 提示里新增的标的、指标或时间。"
        "多项同域任务各自只抽取 query 定位的那项条件，不得把 source_query 的所有标的混到本任务。"
    )
    inputs = [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps({
        "query": query, "history": [getattr(item, "content", item) for item in history
            if (item.get("role") if isinstance(item, dict) else getattr(item, "type", "")) != "tool"], "existing": existing,
        "pending_fields": pending_fields,
        "source_query": source_query, "dependency_results": dependency_context,
    }, ensure_ascii=False, default=str)}]
    try:
        bound = model.with_structured_output(SlotExtraction, method="function_calling")
        raw = await bound.ainvoke(inputs)
        extraction = SlotExtraction.model_validate(raw)
        if extraction.domain is not None and extraction.domain != agent:
            raise ValueError("抽取结果属于其他领域")
        # 请求 ID 是调用方提供的审计标识，不能由模型生成或更改。
        extraction.patch.source_message_id = source_message_id
    except (NotImplementedError, AssertionError, AttributeError, TypeError):
        used_fallback = True
        extraction = _fallback_patch(agent, query, source_message_id)
    except Exception:
        used_fallback = True
        warnings.append("结构化槽位抽取失败，已保守解析当前请求；缺失条件需要确认。")
        extraction = _fallback_patch(agent, query, source_message_id)

    values = extraction.patch.set_values
    if agent == "stock_agent":
        for field in ("symbols", "index_scope"):
            if field in values:
                values[field] = [item for item in values[field] if item not in {"A股", "A股市场", "全市场", "全部A股", "股票"}]
        explicit_codes = re.findall(r"(?<!\d)\d{6}\.(?:SH|SZ|BJ)(?![A-Za-z])", query, re.IGNORECASE)
        if not explicit_codes:
            for match in re.finditer(r"(?<!\d)\d{6}\.(?:SH|SZ|BJ)(?![A-Za-z])", source_query, re.IGNORECASE):
                before = source_query[max(0, match.start() - 22):match.start()]
                if any(str(name) in before and str(name) in query for name in values.get("symbols") or []):
                    explicit_codes.append(match.group())
        if explicit_codes:
            values["symbols"] = list(dict.fromkeys(code.upper() for code in explicit_codes))
        if (re.search(r"指数|大盘|沪深300|中证500|中证1000|科创50|深证成指|创业板指", query)
                and re.search(r"指数|大盘|沪深300|中证500|中证1000|科创50|深证成指|创业板指", source_query)
                and not re.search(r"选股|筛选|选.{0,6}股票|成分股|候选", query)):
            values["scenario"] = "market_overview"
        if values.get("scenario") == "market_overview" and values.get("symbols"):
            # index_scope 是选股的指数范围。普通指数查询已有明确标的时不再次解析同名范围。
            values.pop("index_scope", None)
            if "index_scope" not in extraction.patch.clear_fields:
                extraction.patch.clear_fields.append("index_scope")
    _authorize_patch(agent, extraction, source_query, existing, dependency_entities, warnings)
    if dependency_entities and not values.get("symbols") and "symbols" in model_type.model_fields and re.search(r"其中|持仓.{0,5}股票|组合.{0,5}股票", query):
        # 不扩展到依赖中的账户 ID、金额等；股票仅接收证券代码或可信实体名称。
        candidates = sorted(value for value in dependency_entities if agent != "stock_agent" or
                            re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", value, re.IGNORECASE))
        if candidates and (agent != "stock_agent" or len(candidates) <= 3):
            values["symbols"] = candidates
        elif candidates:
            warnings.append("前置结果包含超过三只股票，请明确本次分析的标的。")
    category = "scenario" if agent == "stock_agent" else "operation"
    # 澄清裸回复只在调用方明确提供待补字段时接受，不从不完整旧状态推测 pending。
    if "symbols" in pending_fields and not values.get("symbols") and "symbols" in model_type.model_fields:
        plain = query.strip().rstrip("。！!")
        generic = {"它", "这只", "那只", "基金", "股票", "大盘", "持仓", "市场", "随便"}
        asks_new_operation = re.match(r"(?:请|帮我)?(?:查询|查看|看看|分析|比较|推荐|筛选|选股|生成|诊断|调整)", plain)
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9.、，, ]{2,40}", plain) and plain not in generic and not asks_new_operation and not re.search(
            r"随便|不知道|不确定|怎么|为什么", plain,
        ):
            names = [item.strip() for item in re.split(r"[、，,]|和|与", plain) if item.strip()]
            names = [item for item in names if item.casefold() in source_query.casefold() or item in dependency_entities]
            if names:
                values["symbols"] = names
            if names and used_fallback and category not in pending_fields:
                # 裸对象补答里的「基金」只是名称的一部分；默认 query 不是用户切换操作。
                values.pop(category, None)
    touched = {field for field, value in values.items() if value != existing.get(field)} | set(extraction.patch.append_values) | set(extraction.patch.clear_fields)
    answers_pending = bool(pending_fields and touched and touched <= set(pending_fields))
    continuation = bool(existing) and (answers_pending or (extraction.continuation and _obvious_followup(query)))
    changed_category = category in values and values[category] != existing.get(category) and existing.get(category) is not None
    changed_objects = any(field in values and values[field] != existing.get(field, []) for field in ("symbols", "codes"))
    same_analysis = bool(re.search(r"同样|相同|按刚才|沿用", query))
    if changed_category or (changed_objects and not same_analysis and not answers_pending):
        continuation = False
    pending_unanswered = bool(pending_fields and existing and not touched)
    base = existing if continuation or pending_unanswered else {}
    try:
        slots = merge_slot_patch(base, extraction.patch, model_type)
    except ValueError:
        warnings.append("槽位补丁包含非法字段或值，已忽略并请求确认。")
        slots = model_type.model_validate(base).model_dump(mode="json", exclude_unset=True)
    missing = validate_slots(agent, slots)
    question, options = _clarification(agent, missing)
    return {"slots": slots, "continuation": continuation, "missing": missing,
            "question": question, "warnings": warnings, "options": options}


class _SlotModels(Mapping[str, type[BaseModel]]):
    """按访问导入领域模块，让领域 schema 也能独立导入公共 TimeRange。"""

    def __getitem__(self, agent: str) -> type[BaseModel]:
        if agent == "stock_agent":
            from app.agents.stock.slots import StockSlots
            return StockSlots
        if agent == "product_agent":
            from app.agents.product.slots import ProductSlots
            return ProductSlots
        if agent == "portfolio_agent":
            from app.agents.portfolio.slots import PortfolioSlots
            return PortfolioSlots
        raise KeyError(agent)

    def __iter__(self):
        return iter(("stock_agent", "product_agent", "portfolio_agent"))

    def __len__(self):
        return 3


SLOT_MODELS: Mapping[str, type[BaseModel]] = _SlotModels()

__all__ = ["SLOT_MODELS", "SlotExtraction", "SlotPatch", "TimeRange", "extract_slots",
           "merge_slot_patch", "normalize_timeframe", "structured_task", "validate_slots"]
