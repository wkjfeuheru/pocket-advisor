"""按 agent 分派公开 artifact 投影器。

未登记的 agent 一律不公开 artifact。股票与持仓各自只保留自己的白名单字段。
"""

from __future__ import annotations

from typing import Any, Callable

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


def public_artifacts(results: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """遍历结构化 worker 结果，逐条用它自己的投影器生成公开 artifact；候选股只来自股票结果。"""
    artifacts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for result in results or []:
        for item in getattr(result, "artifacts", None) or []:
            if not isinstance(item, dict):
                continue
            projected = project_artifact(result.agent, item)
            if projected:
                artifacts.append(projected)
        for item in getattr(result, "candidate_stocks", None) or []:
            candidates.append({"thscode": item.thscode, "name": item.name, "score": item.score})
    return artifacts, candidates


__all__ = ["project_artifact", "public_artifacts"]
