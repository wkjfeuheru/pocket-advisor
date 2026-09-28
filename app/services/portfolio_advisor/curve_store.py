"""对比曲线：从每日快照读出用户线与 AI 线。

前端对比页要两条同口径的归一净值序列。两条线都在 ``ai_portfolio_snapshots`` 里
（``owner`` 区分），所以读取逻辑只有一份。

三条约定：

- **缺快照的日子不补零、不补一**：没有记录就是没有记录。编一个数会让曲线在起点处
  看起来像暴跌，而实际上只是那天没跑快照。
- **两条线各自按日期升序返回**，不强行对齐——用户线天然可能比 AI 线长
  （AI 账户是采纳之后才建立的）。
- **``as_of`` 取两条线里较早的截止日**：整组对比只能信到最旧的那条。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, Sequence

#: 单条曲线默认返回的最近点数。够画 3 个月的交易日。
DEFAULT_LIMIT = 120


class CurveError(ValueError):
    """曲线读取不合法。``code`` 便于上层映射错误响应。"""

    def __init__(self, message: str, *, code: str = "curve_error") -> None:
        super().__init__(message)
        self.code = code


class SnapshotStore(Protocol):
    """每日快照的窄接口。"""

    def list_snapshots(
        self, customer_id: str, owner: str, *, start: date | None = None, end: date | None = None
    ) -> list[tuple[date, float]]: ...


@dataclass
class ComparisonCurves:
    """用户线与 AI 线。两条线各自独立，长度可以不同。"""

    user_curve: list[dict[str, object]] = field(default_factory=list)
    ai_curve: list[dict[str, object]] = field(default_factory=list)
    as_of: date | None = None
    limitations: list[str] = field(default_factory=list)


def _to_points(rows: Sequence[tuple[date, float]], *, limit: int) -> tuple[list[dict[str, object]], int]:
    """转成前端要的形状；跳过非有限净值并返回被跳过的个数。"""
    points: list[dict[str, object]] = []
    skipped = 0
    for day, nav in rows:
        value = float(nav)
        if not math.isfinite(value) or value <= 0:
            skipped += 1
            continue
        points.append({"date": day.isoformat(), "unit_nav": value})
    if limit > 0 and len(points) > limit:
        points = points[-limit:]
    return points, skipped


def build_comparison_curves(
    *,
    store: SnapshotStore,
    customer_id: str,
    start: date | None = None,
    end: date | None = None,
    limit: int = DEFAULT_LIMIT,
) -> ComparisonCurves:
    """读出用户线与 AI 线。任一条为空都不算错误——AI 账户可能还没建立。"""
    if start is not None and end is not None and end < start:
        raise CurveError("结束日期早于开始日期", code="bad_range")

    result = ComparisonCurves()

    user_rows = store.list_snapshots(customer_id, "user", start=start, end=end)
    ai_rows = store.list_snapshots(customer_id, "ai", start=start, end=end)

    user_points, user_skipped = _to_points(user_rows, limit=limit)
    ai_points, ai_skipped = _to_points(ai_rows, limit=limit)
    result.user_curve = user_points
    result.ai_curve = ai_points

    if user_skipped or ai_skipped:
        result.limitations.append(
            f"跳过 {user_skipped + ai_skipped} 个非法净值点（必须为正的有限数）"
        )

    if not user_rows and not ai_rows:
        result.limitations.append("还没有任何每日快照，无法展示收益对比")
        return result

    if not ai_rows:
        result.limitations.append("AI 账户还没有快照记录，尚未产生对比曲线")
    if not user_rows:
        result.limitations.append("用户持仓还没有快照记录")

    ends = [rows[-1][0] for rows in (user_rows, ai_rows) if rows]
    result.as_of = min(ends) if ends else None
    if len(set(ends)) > 1 and result.as_of is not None:
        result.limitations.append(
            f"两条曲线截止日不同，统一按较早的 {result.as_of} 解释"
        )

    return result


__all__ = [
    "DEFAULT_LIMIT",
    "ComparisonCurves",
    "CurveError",
    "SnapshotStore",
    "build_comparison_curves",
]
