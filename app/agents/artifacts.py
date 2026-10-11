"""按 worker 结果类型分派公开 artifact 投影器。

未登记的 agent 一律不公开 artifact。股票与持仓各自只保留自己的白名单字段。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from app.agents.graphs.state import PortfolioResult, StockResult, WorkerBase
from app.agents.portfolio.evidence import public_portfolio_artifact
from app.agents.stock.evidence import public_stock_artifact

Projector = Callable[[dict[str, Any]], dict[str, Any]]

#: agent → 投影器。没有登记的 agent 一律不公开 artifact（失败关闭）。
_PROJECTORS: dict[str, Projector] = {
    "stock_agent": public_stock_artifact,
    "portfolio_agent": public_portfolio_artifact,
}


def project_artifact(agent: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按 agent 选择投影器；未登记返回空字典，绝不回退到别的领域白名单。"""
    projector = _PROJECTORS.get(str(agent or ""))
    if projector is None:
        return {}
    return projector(dict(payload or {}))


def artifact_for_result(result: WorkerBase) -> dict[str, Any]:
    """把单个 worker 结果投影成公开 artifact；没有可公开内容时返回空字典。

    - 股票：公开投影的输入 = ``StockResultV1.model_dump()`` 再补回 ``candidate_stocks``
      （前端从 artifact 事件读取候选池，见 ``candidate_stocks`` 契约）。
    - 持仓：只有采纳结果（``adopted``）才产 artifact；挂起的 ``offer`` 走 interrupt 载荷。
    """
    if isinstance(result, StockResult) and result.result is not None:
        payload = dict(result.result.model_dump())
        payload["candidate_stocks"] = [item.model_dump() for item in result.candidates]
        return project_artifact("stock_agent", payload)
    if isinstance(result, PortfolioResult) and result.adopted is not None:
        return project_artifact(
            "portfolio_agent",
            {"scenario": "allocation_adopted", "allocation_adopted": result.adopted.model_dump()},
        )
    return {}


def public_artifacts(
    results: Sequence[WorkerBase],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """遍历 worker 结果，逐条用其类型对应的投影器生成公开 artifact，并汇总候选池。"""
    artifacts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for result in results or []:
        if not isinstance(result, WorkerBase):
            continue
        projected = artifact_for_result(result)
        if projected:
            artifacts.append(projected)
        if isinstance(result, StockResult):
            for item in result.candidates:
                candidates.append({
                    "thscode": item.thscode,
                    "name": item.name,
                    "score": item.score,
                })
    return artifacts, candidates


__all__ = ["artifact_for_result", "project_artifact", "public_artifacts"]
