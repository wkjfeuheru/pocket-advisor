"""日频净值同步：从净值源拉当日净值并落库。

调度器与外部数据源之间的接缝。**当前没有可用的外部净值源**（Fuyao 的基金端点未验证、
Tushare 无 ``fund_nav``），所以源是一个协议；没有源时作业明确报 ``no_source``，
而不是静默"成功但什么都没写"——后者会让定时任务看起来在跑，而净值永远停在旧值。

行为约定：

- 同步回来的行是 **``real``**，按覆盖规则压过合成数据（这正是要的）。
- **单个标的失败不影响其余**：源偶发超时不该让整批净值全丢。逐标的收集失败原因。
- 净值生效日**以源返回的日期为准**，不强行记成"今天"——源可能补发前一交易日的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, Sequence

from app.services.portfolio_advisor.nav import REAL, NavRow, NavService


class NavSourceError(ValueError):
    """同步前置条件不满足。``code`` 便于上层区分处理。"""

    def __init__(self, message: str, *, code: str = "nav_sync_error") -> None:
        super().__init__(message)
        self.code = code


class NavSource(Protocol):
    """净值源。实现它即可接入真实数据（不改变本模块任何逻辑）。"""

    def latest_nav(self, product_code: str, *, as_of: date) -> tuple[date, float] | None:
        """返回 ``(净值生效日, 净值)``；该日无数据时返回 ``None``。

        实现可以抛异常表示上游故障——调用方会把它记进 ``failed`` 而不中断整批。
        """
        ...


@dataclass
class NavSyncResult:
    """一次同步的结果。

    ``status``：``ok`` 全部成功、``partial`` 部分失败、``failed`` 全部失败、
    ``no_source`` 未配置净值源。
    """

    written: int
    failed: list[dict[str, str]] = field(default_factory=list)
    status: str = "ok"
    as_of: date | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _status_for(written: int, failed: int) -> str:
    if written == 0 and failed > 0:
        return "failed"
    if failed > 0:
        return "partial"
    return "ok"


def sync_daily_nav(
    service: NavService,
    source: NavSource | None,
    *,
    product_codes: Sequence[str],
    as_of: date,
) -> NavSyncResult:
    """把 ``product_codes`` 的当日净值同步进库。

    ``source`` 为 ``None`` 时返回 ``no_source``（不抛错）：调度器需要把这个状态
    暴露到健康检查里，而不是让整轮任务崩掉。
    """
    codes = [str(code) for code in product_codes if str(code)]
    if not codes:
        raise NavSourceError("没有需要同步的标的", code="no_products")

    if source is None:
        return NavSyncResult(written=0, failed=[], status="no_source", as_of=as_of)

    rows: list[NavRow] = []
    failed: list[dict[str, str]] = []

    for code in codes:
        try:
            quote = source.latest_nav(code, as_of=as_of)
        except Exception as exc:  # noqa: BLE001 - 上游异常只记不抛
            failed.append({"product_code": code, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if quote is None:
            # 该日确实没有净值（例如停牌或未披露），不算错误但必须记录。
            failed.append({"product_code": code, "reason": "no_data"})
            continue
        trade_date, nav = quote
        rows.append(NavRow(code, trade_date, float(nav), REAL))

    written = service.upsert(rows) if rows else 0
    return NavSyncResult(
        written=written,
        failed=failed,
        status=_status_for(written, len(failed)),
        as_of=as_of,
    )


__all__ = [
    "NavSource",
    "NavSourceError",
    "NavSyncResult",
    "sync_daily_nav",
]
