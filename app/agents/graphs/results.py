"""四个专业 worker 的结构化结果。

每个 worker 在图状态里独占一个通道（``RESULT_KEYS``），一轮最多写一次。
通道里存 ``model_dump(mode="json")`` 后的 JSON 形态，checkpoint 不依赖自定义类型反序列化；
读取统一走 ``worker_results``。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.agents.graphs.state import AGENT_NAMES
from app.agents.stock.contracts import CandidateStock

WorkerStatus = Literal["ok", "degraded", "failed"]


class _WorkerResultBase(BaseModel):
    """``degraded``：上游缺失但给出了可用的定性说明；``failed``：本分支没有可用结论。"""

    model_config = ConfigDict(extra="forbid")

    status: WorkerStatus = "ok"
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    source: str = ""
    as_of: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"


class StockWorkerResult(_WorkerResultBase):
    agent: Literal["stock_agent"] = "stock_agent"
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    candidate_stocks: list[CandidateStock] = Field(default_factory=list)


class ProductWorkerResult(_WorkerResultBase):
    agent: Literal["product_agent"] = "product_agent"
    recommended_product_codes: list[str] = Field(default_factory=list)


class PortfolioWorkerResult(_WorkerResultBase):
    agent: Literal["portfolio_agent"] = "portfolio_agent"
    recommended_product_codes: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class ChatWorkerResult(_WorkerResultBase):
    agent: Literal["chat_agent"] = "chat_agent"


WorkerResult = Annotated[
    Union[StockWorkerResult, ProductWorkerResult, PortfolioWorkerResult, ChatWorkerResult],
    Field(discriminator="agent"),
]
_ADAPTER: TypeAdapter[Any] = TypeAdapter(WorkerResult)

RESULT_TYPES: dict[str, type[_WorkerResultBase]] = {
    "stock_agent": StockWorkerResult,
    "product_agent": ProductWorkerResult,
    "portfolio_agent": PortfolioWorkerResult,
    "chat_agent": ChatWorkerResult,
}

RESULT_KEYS: dict[str, str] = {
    "stock_agent": "stock_result",
    "product_agent": "product_result",
    "portfolio_agent": "portfolio_result",
    "chat_agent": "chat_result",
}


def failed_result(
    agent: str,
    warning: str,
    *,
    status: WorkerStatus = "failed",
    source: str = "",
    as_of: str = "",
) -> Any:
    return RESULT_TYPES[agent](
        status=status,
        summary=warning,
        warnings=[warning] if warning else [],
        source=source,
        as_of=as_of,
    )


def worker_results(values: Mapping[str, Any]) -> list[Any]:
    """按 ``AGENT_NAMES`` 顺序取出本轮已写入的 worker 结果。"""
    results = []
    for agent in AGENT_NAMES:
        raw = values.get(RESULT_KEYS[agent])
        if raw is None:
            continue
        results.append(raw if isinstance(raw, _WorkerResultBase) else _ADAPTER.validate_python(raw))
    return results


def turn_warnings(values: Mapping[str, Any]) -> list[str]:
    """图级告警 + 各 worker 告警，按出现顺序去重。"""
    items = [*(values.get("turn_warnings") or [])]
    for result in worker_results(values):
        items.extend(result.warnings)
    return list(dict.fromkeys(str(item) for item in items if item))


__all__ = [
    "RESULT_KEYS",
    "RESULT_TYPES",
    "ChatWorkerResult",
    "PortfolioWorkerResult",
    "ProductWorkerResult",
    "StockWorkerResult",
    "WorkerResult",
    "WorkerStatus",
    "failed_result",
    "turn_warnings",
    "worker_results",
]
