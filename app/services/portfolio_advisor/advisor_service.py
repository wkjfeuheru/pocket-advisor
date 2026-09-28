"""持仓配置接口层的用例编排。

职责：把"取谁的持仓/商品池/净值"与"算方案/落库/采纳"接起来，并把失败统一成
带 ``code`` 的业务错误，供路由映射 HTTP 状态。

取数全部走已有的 store 接口（``PortfolioStore`` / ``ProductLibrary`` / ``NavStore``），
因此本模块可以在测试里用内存假件替换，不需要数据库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Protocol

logger = logging.getLogger(__name__)

from app.agents.portfolio.contracts import AllocationInterruptError
from app.agents.portfolio.validation import ValidationConstraints
from app.services.portfolio_advisor.adopt import AdoptError, adopt_selected_plan
from app.services.portfolio_advisor.allocation import (
    AllocationError,
    AllocationResult,
    generate_and_persist,
)
from app.services.portfolio_advisor.curve_store import build_comparison_curves
from app.services.portfolio_advisor.ledger import LedgerService
from app.services.portfolio_advisor.snapshot import write_both_curves
from app.services.portfolio_advisor.valuation import value_portfolio

#: 生成方案时默认看的净值窗口（自然日）。与设计文档"合成阶段默认 3 个月"一致。
DEFAULT_LOOKBACK_DAYS = 120


class AdvisorServiceError(ValueError):
    """接口层业务错误。``code`` 由路由映射成 HTTP 状态。"""

    def __init__(self, message: str, *, code: str = "advisor_error") -> None:
        super().__init__(message)
        self.code = code


class PortfolioReader(Protocol):
    """读取用户持仓与账户（由 ``PostgresPortfolioStore`` 实现）。"""

    def list_positions(self, customer_id: str) -> list[dict]: ...


class ProductLibrary(Protocol):
    def list_products_for_nav(self) -> list[dict]: ...

    def query_by_codes(self, codes: list[str]) -> list[dict]: ...


class NavReader(Protocol):
    def history(self, product_code: str, start: date, end: date) -> list[tuple[date, float]]: ...


@dataclass
class AdvisorDeps:
    """接口层的依赖集合。全部可替换，便于单测。"""

    portfolio: PortfolioReader
    library: ProductLibrary
    navs: NavReader
    plan_store: Any
    ledger: LedgerService
    constraints: ValidationConstraints = field(default_factory=ValidationConstraints)
    #: 每日快照存储。缺省为空表示未接线，此时对比曲线为空并给出说明。
    snapshots: Any = None


def _latest_navs(
    deps: AdvisorDeps, codes: list[str], *, as_of: date, lookback_days: int
) -> tuple[dict[str, float], date | None, list[str]]:
    """各标的最新净值 + 整组数据的截止日 + 缺净值的标的。

    ``as_of`` 取各标的最后一条真实净值的**较早者**：整组数据只能信到最旧的那条，
    与 ``nav_series.build_nav_series`` 的口径保持一致。
    """
    start = as_of - timedelta(days=lookback_days)
    navs: dict[str, float] = {}
    last_days: list[date] = []
    missing: list[str] = []
    for code in codes:
        rows = deps.navs.history(code, start, as_of)
        if not rows:
            missing.append(code)
            continue
        day, value = rows[-1]
        navs[code] = float(value)
        last_days.append(day)
    return navs, (min(last_days) if last_days else None), missing


def _stored_baseline(store: Any, customer_id: str, owner: str) -> float | None:
    """读回该曲线既有的份额基线；没有则 ``None``。

    基线必须跨天稳定，否则 ``unit_nav = 市值 / 当天市值`` 恒等于 1，曲线是一条直线。
    """
    try:
        row = store.latest_snapshot(customer_id, owner)
    except Exception:  # noqa: BLE001 - 读不到就当作没有基线
        return None
    if not row:
        return None
    value = row.get("baseline_units")
    return float(value) if value else None


def user_placeholder_units(
    market_values: Mapping[str, float], navs: Mapping[str, float]
) -> float:
    """首次写快照时给一个占位份额，好让估值算得出 total_assets。

    调用方拿到估值后会**用真实的 total_assets 覆盖**这个占位值再落库
    （见 ``write_daily_snapshots``），所以它只需要是一个正数。
    这里用"按净值重算的市值"而不是持仓行里的 market_value：后者可能与净值口径不一致。
    """
    total = 0.0
    for code, value in market_values.items():
        price = navs.get(code)
        total += float(value) if price else float(value)
    return total if total > 0 else 1.0


class AdvisorService:
    """持仓配置的接口层实现。"""

    def __init__(self, deps: AdvisorDeps) -> None:
        self._deps = deps

    # ── 生成 ───────────────────────────────────────────────────────────
    def generate(
        self,
        customer_id: str,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
        as_of: date | None = None,
        candidates: Mapping[Any, Mapping[str, float]] | None = None,
    ) -> dict[str, Any]:
        """取持仓与商品池，生成三档方案并落库。"""
        deps = self._deps
        positions = deps.portfolio.list_positions(customer_id)
        if not positions:
            raise AdvisorServiceError("当前没有持仓，无法给出调仓建议", code="no_positions")

        products = {item["code"]: dict(item) for item in deps.library.list_products_for_nav()}
        if not products:
            raise AdvisorServiceError("商品库为空，无法给出配置建议", code="no_products")

        held = {str(row["product_code"]) for row in positions}
        # 既持标的也要有画像信息才能做风险分档；不在在售池里的（下架/理财伪代码）
        # 不进入候选，但仍会出现在方案明细里（可能被建议减仓）。
        for code in held:
            if code not in products:
                detail = deps.library.query_by_codes([code])
                if detail:
                    products[code] = dict(detail[0])

        # 持仓权重按市值占比。market_value 由上游估值提供；缺失时用成本兜底。
        market_values = {}
        for row in positions:
            code = str(row["product_code"])
            value = row.get("market_value")
            if value is None:
                value = float(row.get("cost_amount") or 0.0)
            market_values[code] = float(value)

        total = sum(market_values.values())
        if total <= 0:
            raise AdvisorServiceError("持仓市值为 0，无法计算权重", code="no_positions")
        current_weights = {code: value / total for code, value in market_values.items()}

        today = as_of or date.today()
        navs, nav_as_of, missing = _latest_navs(
            deps, sorted(set(products) | held), as_of=today, lookback_days=DEFAULT_LOOKBACK_DAYS
        )

        # 不可定价的标的标记出来：候选会跳过它们，但明细仍能显示"建议减仓"。
        # 既持标的也一样——没有净值就不该出现在"建议新增/加仓"的候选里，
        # 否则会生成一个**永远无法采纳**的方案（采纳时需要净值才能成交）。
        for code in products:
            if code not in navs:
                products[code]["priceable"] = False
        for code in held:
            if code not in navs:
                products.setdefault(code, {})
                products[code]["priceable"] = False

        try:
            result: AllocationResult = generate_and_persist(
                deps.plan_store,
                customer_id=customer_id,
                current_weights=current_weights,
                products=products,
                constraints=deps.constraints,
                candidates=candidates,
                initial_capital=total,
                nav_as_of=nav_as_of,
                request_id=request_id,
                session_id=session_id,
            )
        except AllocationError as exc:
            raise AdvisorServiceError(str(exc), code=exc.code) from exc

        return {
            "customer_id": customer_id,
            "total_assets": total,
            "nav_as_of": nav_as_of,
            "missing_nav": missing,
            "plans": [self._plan_view(p.plan_id, p.generated) for p in result.persisted],
            "unavailable": [
                {
                    "tier": plan.tier.value,
                    "tier_label": plan.label,
                    "unavailable_reason": plan.unavailable_reason,
                }
                for plan in result.unavailable
            ],
        }

    # ── 读取 ───────────────────────────────────────────────────────────
    def list_plans(self, customer_id: str, *, limit: int = 50) -> dict[str, Any]:
        rows = self._deps.plan_store.list_plans(customer_id, limit=limit)
        return {
            "customer_id": customer_id,
            "plans": [
                {
                    "plan_id": row["plan_id"],
                    "tier": row["tier"],
                    "tier_label": _TIER_LABELS.get(row["tier"], row["tier"]),
                    "status": row["status"],
                    "turnover_rate": row.get("turnover_rate"),
                    "estimated_fee": row.get("estimated_fee"),
                    "unavailable_reason": row.get("unavailable_reason") or "",
                }
                for row in rows
            ],
        }

    def get_plan(self, customer_id: str, plan_id: str) -> dict[str, Any]:
        plan = self._deps.plan_store.get_plan(plan_id)
        if plan is None or str(plan.get("customer_id")) != str(customer_id):
            # 不区分"不存在"与"不是你的"：避免用 id 探测别人的方案是否存在。
            raise AdvisorServiceError(f"方案不存在: {plan_id}", code="plan_not_found")
        return {
            "plan_id": plan["plan_id"],
            "tier": plan["tier"],
            "tier_label": _TIER_LABELS.get(plan["tier"], plan["tier"]),
            "status": plan["status"],
            "turnover_rate": plan.get("turnover_rate"),
            "estimated_fee": plan.get("estimated_fee"),
            "rationale": plan.get("rationale") or "",
            "limitations": list(plan.get("limitations") or []),
            "unavailable_reason": plan.get("unavailable_reason") or "",
            "adopted_at": plan.get("adopted_at"),
            "items": [
                {
                    "product_code": item["product_code"],
                    "action": item["action"],
                    "current_weight": item.get("current_weight"),
                    "target_weight": item.get("target_weight"),
                    "delta_weight": item.get("delta_weight"),
                    "reason": item.get("reason") or "",
                }
                for item in plan.get("items") or []
            ],
        }

    # ── 采纳 ───────────────────────────────────────────────────────────
    def adopt(
        self,
        customer_id: str,
        plan_id: str,
        *,
        request_id: str | None = None,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """采纳一个方案：写入 AI 账本。用户持仓不动。"""
        deps = self._deps
        plan = self.get_plan(customer_id, plan_id)  # 复用存在性与归属校验

        positions = deps.portfolio.list_positions(customer_id)
        if not positions:
            raise AdvisorServiceError("当前没有持仓，无法建仓", code="no_positions")

        market_values = {}
        for row in positions:
            code = str(row["product_code"])
            value = row.get("market_value")
            if value is None:
                value = float(row.get("cost_amount") or 0.0)
            market_values[code] = float(value)
        total = sum(market_values.values())

        today = as_of or date.today()
        navs, nav_as_of, missing = _latest_navs(
            deps, sorted({i["product_code"] for i in plan["items"]} | set(market_values)),
            as_of=today, lookback_days=DEFAULT_LOOKBACK_DAYS,
        )
        if missing:
            raise AdvisorServiceError(
                f"以下标的缺少净值，无法成交: {', '.join(sorted(missing))}",
                code="missing_nav",
            )

        from app.agents.portfolio.contracts import build_allocation_payload

        payload = build_allocation_payload(
            [
                {
                    "plan_id": plan_id,
                    "tier": plan["tier"],
                    "turnover_rate": plan.get("turnover_rate"),
                    "estimated_fee": plan.get("estimated_fee"),
                }
            ]
        )
        holdings = {
            row["product_code"]: {
                "shares": float(row.get("shares") or 0.0),
                "market_value": market_values[str(row["product_code"])],
                "priceable": str(row["product_code"]) in navs,
            }
            for row in positions
        }
        # 方案明细里非既持的标的也必须参与成交，否则新增仓位不会被执行。
        for item in plan["items"]:
            code = item["product_code"]
            if code not in holdings and code in navs:
                holdings[code] = {"shares": 0.0, "market_value": 0.0, "priceable": True}

        try:
            outcome = adopt_selected_plan(
                customer_id=customer_id,
                payload=payload,
                resume_value={"plan_id": plan_id},
                plans=deps.plan_store,
                ledger=deps.ledger,
                current_holdings=holdings,
                navs=navs,
                nav_as_of=nav_as_of or today,
                initial_capital=total,
            )
        except (AdoptError, AllocationInterruptError) as exc:
            raise AdvisorServiceError(str(exc), code=getattr(exc, "code", "adopt_failed")) from exc

        return {
            "plan_id": outcome.plan_id,
            "tier": outcome.tier.value,
            "tier_label": _TIER_LABELS.get(outcome.tier.value, outcome.tier.value),
            "cash_balance": outcome.cash_balance,
            "total_assets": outcome.total_assets,
            "unit_nav": outcome.unit_nav,
            "total_fee": outcome.total_fee,
            "trade_count": outcome.trade_count,
            "established": outcome.established,
            "nav_as_of": nav_as_of,
        }

    # ── 对比 ───────────────────────────────────────────────────────────
    def ai_account(self, customer_id: str) -> dict[str, Any]:
        """AI 账户快照 + 与用户账户的口径对照 + 两条对比曲线。"""
        deps = self._deps
        account = deps.ledger.account(customer_id)
        positions = deps.portfolio.list_positions(customer_id)
        market_values = {
            str(row["product_code"]): float(
                row.get("market_value")
                if row.get("market_value") is not None
                else row.get("cost_amount") or 0.0
            )
            for row in positions
        }
        today = date.today()
        navs, nav_as_of, missing = _latest_navs(
            deps, sorted(market_values), as_of=today, lookback_days=DEFAULT_LOOKBACK_DAYS
        )
        user = value_portfolio(
            positions=[
                {
                    "product_code": code,
                    "shares": float(
                        next(
                            (r.get("shares") or 0.0 for r in positions if str(r["product_code"]) == code),
                            0.0,
                        )
                    ),
                    "cost_amount": next(
                        (float(r.get("cost_amount") or 0.0) for r in positions if str(r["product_code"]) == code),
                        0.0,
                    ),
                    "priceable": code in navs,
                }
                for code in market_values
            ],
            cash_balance=0.0,
            navs=navs,
            total_units=0.0,  # 用户没有"份额"概念，单位净值只对 AI 账户有意义
            nav_as_of=nav_as_of,
        )

        curves = self._comparison_curves(customer_id)
        limitations = list(user.limitations) + list(curves.limitations)
        return {
            "customer_id": customer_id,
            "ai_account": account,
            "user_total_assets": user.total_assets,
            "nav_as_of": nav_as_of or curves.as_of,
            "missing_nav": missing,
            "limitations": limitations,
            # 对比页用这两条归一净值曲线；缺快照时是空列表（不补点）。
            "user_curve": curves.user_curve,
            "ai_curve": curves.ai_curve,
            "curve_as_of": curves.as_of.isoformat() if curves.as_of else None,
        }

    def _comparison_curves(self, customer_id: str):
        """读两条对比曲线。未配置快照存储时返回空曲线而不是报错。"""
        store = getattr(self._deps, "snapshots", None)
        if store is None:
            from app.services.portfolio_advisor.curve_store import ComparisonCurves

            return ComparisonCurves(limitations=["尚未配置快照存储，暂无对比曲线"])
        try:
            return build_comparison_curves(store=store, customer_id=customer_id)
        except Exception as exc:  # noqa: BLE001 - 曲线读不到不该让整个页面 500
            from app.services.portfolio_advisor.curve_store import ComparisonCurves

            # 把异常写进日志：只回一个类型名给前端，排查时会毫无线索。
            logger.exception("读取对比曲线失败")
            return ComparisonCurves(
                limitations=[f"读取对比曲线失败（{type(exc).__name__}），请查看服务端日志"]
            )

    def write_daily_snapshots(self, customer_id: str, *, as_of: date | None = None) -> list[Any]:
        """把今天的用户线与 AI 线固化成快照（幂等，同日重跑覆盖）。"""
        store = getattr(self._deps, "snapshots", None)
        if store is None:
            return []

        deps = self._deps
        day = as_of or date.today()
        positions = deps.portfolio.list_positions(customer_id)
        market_values = {
            str(row["product_code"]): float(
                row.get("market_value")
                if row.get("market_value") is not None
                else row.get("cost_amount") or 0.0
            )
            for row in positions
        }
        navs, nav_as_of, _ = _latest_navs(
            deps, sorted(market_values), as_of=day, lookback_days=DEFAULT_LOOKBACK_DAYS
        )
        # 基线必须与估值用的是**同一份市值**。
        # 早期实现拿"持仓行里的 market_value 之和"当基线，而估值按 份额×净值 重算，
        # 两者不一致，导致首日 unit_nav = 75,526/100,000 = 0.755 而不是 1.0。
        # 正确顺序：先估出 total_assets，再用它当基线；之后从快照继承。
        baseline = _stored_baseline(store, customer_id, "user")
        user = value_portfolio(
            positions=[
                {
                    "product_code": code,
                    "shares": float(
                        next(
                            (r.get("shares") or 0.0 for r in positions if str(r["product_code"]) == code),
                            0.0,
                        )
                    ),
                    "cost_amount": next(
                        (float(r.get("cost_amount") or 0.0) for r in positions if str(r["product_code"]) == code),
                        0.0,
                    ),
                    "priceable": code in navs,
                }
                for code in market_values
            ],
            cash_balance=0.0,
            navs=navs,
            total_units=baseline or user_placeholder_units(market_values, navs),
            nav_as_of=nav_as_of,
        )

        account = deps.ledger.account(customer_id)
        ai_payload: dict[str, Any] | None = None
        if account:
            # 账户本身已经持有 unit_nav 与 total_units，总资产 = 两者之积，
            # 因此不必再去读 AI 持仓明细（少一次查询，也少一处口径分歧）。
            cash = float(account.get("cash_balance") or 0.0)
            total_assets = float(account.get("unit_nav") or 0.0) * float(
                account.get("total_units") or 0.0
            )
            ai_payload = {
                "total_assets": total_assets,
                "cash_balance": cash,
                "market_value": max(0.0, total_assets - cash),
                "total_units": float(account.get("total_units") or 0.0),
            }

        return write_both_curves(
            store,
            customer_id=customer_id,
            snapshot_date=day,
            user={
                "total_assets": user.total_assets,
                "cash_balance": 0.0,
                "market_value": user.market_value,
                # 首次写快照时，基线就是这次的估值结果——这样首日 unit_nav 必然是 1.0；
                # 之后由 write_snapshot 从既有快照继承，不再随市值漂移。
                "total_units": baseline or user.total_assets,
                "limitations": user.limitations,
                # 回撤与波动需要**整条曲线**才能算，单点估值给不出来。
                # 这里留空，等有足够历史后由单独的统计任务回填——
                # 宁可为空，也不要在这里估一个数塞进库里当既成事实。
                "max_drawdown": None,
                "annualized_volatility": None,
            },
            ai=ai_payload,
            nav_as_of=nav_as_of,
        )

    # ── 内部 ───────────────────────────────────────────────────────────
    def _plan_view(self, plan_id: str, generated: Any) -> dict[str, Any]:
        return {
            "plan_id": plan_id,
            "tier": generated.tier.value,
            "tier_label": generated.label,
            "turnover_rate": generated.turnover_rate,
            "estimated_fee": generated.estimated_fee,
            "max_single_weight": generated.max_single_weight,
            "concentration": generated.concentration,
            "weights": dict(generated.weights),
            "limitations": list(generated.limitations),
            "unavailable_reason": "",
        }


_TIER_LABELS = {"light": "微调", "moderate": "中度", "rebuild": "重构"}

_advisor_service: AdvisorService | None = None


def get_advisor_service() -> AdvisorService:
    """生产实现：真实持仓、商品池、净值与方案存储。"""
    global _advisor_service
    if _advisor_service is None:
        from app.crud.portfolio_advisor_crud import (
            get_ledger_store,
            get_nav_store,
            get_plan_store,
            get_snapshot_store,
        )
        from app.crud.portfolio_crud import get_portfolio_store
        from app.crud.product_crud import get_product_library

        _advisor_service = AdvisorService(
            AdvisorDeps(
                portfolio=get_portfolio_store(),
                library=get_product_library(),
                navs=get_nav_store(),
                plan_store=get_plan_store(),
                ledger=LedgerService(get_ledger_store()),
                snapshots=get_snapshot_store(),
            )
        )
    return _advisor_service


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "AdvisorDeps",
    "AdvisorService",
    "AdvisorServiceError",
    "get_advisor_service",
]
