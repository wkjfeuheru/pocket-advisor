"""持仓配置采纳结果的公开投影。

只保留用户需要核对的成交摘要。原始权重、调试字段和未登记键一律不出去。
"""

from __future__ import annotations

from typing import Any

PUBLIC_SCHEMA = "portfolio-public-v1"

_ADOPTED_FIELDS = (
    "plan_id",
    "tier",
    "tier_label",
    "cash_balance",
    "total_assets",
    "unit_nav",
    "total_fee",
    "trade_count",
    "established",
    "nav_as_of",
)


def public_portfolio_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """把采纳结果收成公开 artifact；不是采纳结果则返回空字典。"""
    if str((payload or {}).get("scenario") or "") != "allocation_adopted":
        return {}
    adopted = (payload or {}).get("allocation_adopted")
    if not isinstance(adopted, dict):
        return {}
    public = {key: adopted[key] for key in _ADOPTED_FIELDS if key in adopted}
    if not public:
        return {}
    return {
        "schema_version": PUBLIC_SCHEMA,
        "scenario": "allocation_adopted",
        "allocation_adopted": public,
    }


__all__ = ["PUBLIC_SCHEMA", "public_portfolio_artifact"]
