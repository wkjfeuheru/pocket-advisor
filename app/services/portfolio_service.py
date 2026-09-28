"""模拟交易服务层：账户、下单成交、持仓与账户口径的唯一实现。"""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from app.core.config import (
    PORTFOLIO_MAX_DEPOSIT_AMOUNT,
    PORTFOLIO_MIN_ORDER_AMOUNT,
)
from app.schemas.portfolio import (
    AccountSnapshot,
    LiquidationResult,
    OrderView,
    PositionView,
    TradeResult,
    TransactionView,
)
from app.services.product_service import NavSource, ProductError, product_fee_rates, require_nav
from app.utils.errors import BusinessError
from app.core.security import is_unique_violation

logger = logging.getLogger(__name__)

SIMULATED_DISCLAIMER = "模拟交易数据，不构成投资建议。"

SHARE_PRECISION = 2


def round_shares(shares: float) -> float:
    """份额向下取整到两位小数。"""
    if shares <= 0:
        return 0.0
    scaled = shares * 10**SHARE_PRECISION
    return math.floor(scaled + 1e-9) / 10**SHARE_PRECISION


def round_money(amount: float) -> float:
    """金额保留两位小数。"""
    return round(amount + 0.0, 2)


@dataclass(frozen=True)
class SubscriptionBreakdown:
    """一笔申购的资金拆解（外扣法）。"""

    fee_rate: float
    fee: float
    shares: float
    invested: float
    cash_out: float
    limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RedemptionBreakdown:
    """一笔赎回的资金拆解。"""

    fee_rate: float
    fee: float
    gross: float
    cash_in: float
    limitations: list[str] = field(default_factory=list)


def subscription_by_amount(
    amount: float, nav: float, fee_rate: float | None, *, field_name: str = "subscription_fee",
) -> SubscriptionBreakdown:
    """按申购金额计算费用与份额（外扣法）。"""
    rate = 0.0 if fee_rate is None else fee_rate
    limitations: list[str] = []
    if fee_rate is None:
        limitations.append(f"fee_unavailable:{field_name}")
    net_invested = amount / (1.0 + rate) if rate else amount
    fee = round_money(amount - net_invested)
    shares = round_shares(net_invested / nav)
    invested = round_money(shares * nav)
    return SubscriptionBreakdown(
        fee_rate=rate,
        fee=fee,
        shares=shares,
        invested=invested,
        cash_out=round_money(invested + fee),
        limitations=limitations,
    )


def subscription_by_shares(
    shares: float, nav: float, fee_rate: float | None, *, field_name: str = "subscription_fee",
) -> SubscriptionBreakdown:
    """按申购份额计算费用与扣款。"""
    rate = 0.0 if fee_rate is None else fee_rate
    limitations: list[str] = []
    if fee_rate is None:
        limitations.append(f"fee_unavailable:{field_name}")
    gross = round_money(shares * nav)
    fee = round_money(gross * rate)
    return SubscriptionBreakdown(
        fee_rate=rate,
        fee=fee,
        shares=round_shares(shares),
        invested=gross,
        cash_out=round_money(gross + fee),
        limitations=limitations,
    )


def redemption(
    shares: float, nav: float, fee_rate: float | None, *, field_name: str = "redemption_fee",
) -> RedemptionBreakdown:
    """按赎回份额计算赎回费与到账金额。"""
    rate = 0.0 if fee_rate is None else fee_rate
    limitations: list[str] = []
    if fee_rate is None:
        limitations.append(f"fee_unavailable:{field_name}")
    gross = round_money(shares * nav)
    fee = round_money(gross * rate)
    return RedemptionBreakdown(
        fee_rate=rate,
        fee=fee,
        gross=gross,
        cash_in=round_money(gross - fee),
        limitations=limitations,
    )


class PortfolioError(BusinessError):
    """模拟交易业务失败。用 ``code`` 区分原因，不再为每种失败单独建子类。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now()


def _limitations(value: Any) -> list[str]:
    """jsonb 列可能返回 list，也可能返回 JSON 字符串；两种都要能读。"""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _order_view(row: dict[str, Any], names: dict[str, str]) -> OrderView:
    return OrderView(
        order_id=str(row.get("order_id", "")),
        product_code=str(row.get("product_code", "")),
        product_name=names.get(str(row.get("product_code", "")), ""),
        side=str(row.get("side", "buy")),  # type: ignore[arg-type]
        shares=float(row.get("shares") or 0),
        price=float(row.get("price") or 0),
        gross_amount=float(row.get("gross_amount") or 0),
        fee=float(row.get("fee") or 0),
        net_amount=float(row.get("net_amount") or 0),
        realized_pnl=(
            float(row["realized_pnl"]) if row.get("realized_pnl") is not None else None
        ),
        fee_limitations=_limitations(row.get("fee_limitations")),
        created_at=row.get("created_at"),
    )


def _percent(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator * 100.0, 4)


@dataclass
class PortfolioDeps:
    """由组合根注入的存储、商品库、净值来源与时钟。"""

    store: Any = None
    library: Any = None
    nav_source: NavSource | None = None
    clock: Callable[[], datetime] = _now

    def build_store(self) -> Any:
        if self.store is not None:
            return self.store
        raise RuntimeError("PortfolioDeps.store must be provided by the composition root")

    def build_library(self) -> Any:
        if self.library is not None:
            return self.library
        raise RuntimeError("PortfolioDeps.library must be provided by the composition root")

    def build_nav_source(self) -> NavSource:
        if self.nav_source is not None:
            return self.nav_source
        raise RuntimeError("PortfolioDeps.nav_source must be provided by the composition root")


class PortfolioService:
    """模拟交易业务逻辑。"""

    def __init__(self, deps: PortfolioDeps | None = None) -> None:
        self._deps = deps or PortfolioDeps()

    @property
    def store(self) -> Any:
        return self._deps.build_store()

    @property
    def library(self) -> Any:
        return self._deps.build_library()

    @property
    def nav_source(self) -> NavSource:
        return self._deps.build_nav_source()

    def _product_facts_for(self, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = list(dict.fromkeys(str(code) for code in codes if str(code)))
        if not wanted:
            return {}
        try:
            products = self.library.query_by_codes(wanted)
        except Exception:  # noqa: BLE001
            logger.warning("product_facts_lookup_failed codes=%s", wanted)
            return {}
        facts: dict[str, dict[str, Any]] = {}
        for item in products:
            basic = item.get("basic_info") or {}
            code = str(basic.get("code") or "")
            if not code:
                continue
            facts[code] = {
                "name": str(basic.get("name") or ""),
                "type": str(basic.get("type") or ""),
                "risk_level": str(basic.get("risk_level") or ""),
            }
        return facts

    def _names_for(self, codes: Iterable[str]) -> dict[str, str]:
        return {
            code: str(entry.get("name") or "")
            for code, entry in self._product_facts_for(codes).items()
        }

    def _product_or_raise(self, product_code: str) -> dict[str, Any]:
        code = str(product_code).strip()
        if not code:
            raise PortfolioError("请提供商品代码", code="product_not_found")
        product = self.library.query_by_code(code)
        if product is None:
            raise PortfolioError(f"商品 {code} 不存在", code="product_not_found")
        return product

    def _require_on_shelf(self, product: dict[str, Any]) -> None:
        basic = product.get("basic_info") or {}
        if not bool(basic.get("is_active", True)):
            raise PortfolioError(
                f"商品 {basic.get('code') or ''} 已下架，无法申购",
                code="product_offline",
            )

    def _nav_or_raise(self, product_code: str, name: str):
        try:
            return require_nav(self.nav_source, str(product_code).strip(), name)
        except ProductError as exc:
            raise PortfolioError(exc.message, code=exc.code) from exc

    @staticmethod
    def _cash_of(account_row: dict[str, Any]) -> float:
        return float(account_row.get("cash_balance") or 0)

    def list_positions(self, customer_id: str) -> list[PositionView]:
        rows = self.store.list_positions(customer_id)
        if not rows:
            return []
        meta = self._product_facts_for(str(row.get("product_code")) for row in rows)
        priced: list[PositionView] = []
        for row in rows:
            code = str(row.get("product_code"))
            shares = float(row.get("shares") or 0)
            cost_amount = round_money(float(row.get("cost_amount") or 0))
            quote = self.nav_source.latest_nav(code)
            limitations: list[str] = []
            nav = quote.nav if quote else None
            if nav is None:
                limitations.append("pricing_unavailable")
            market_value = round_money(shares * nav) if nav is not None else None
            unrealized = (
                round_money(market_value - cost_amount) if market_value is not None else None
            )
            priced.append(PositionView(
                product_code=code,
                product_name=meta.get(code, {}).get("name", ""),
                product_type=meta.get(code, {}).get("type", ""),
                risk_level=meta.get(code, {}).get("risk_level", ""),
                shares=shares,
                cost_amount=cost_amount,
                avg_cost=float(row.get("avg_cost") or 0),
                nav=nav,
                nav_as_of=quote.as_of if quote else "",
                market_value=market_value,
                unrealized_pnl=unrealized,
                return_rate=_percent(unrealized, cost_amount) if unrealized is not None else None,
                opened_at=row.get("opened_at"),
                updated_at=row.get("updated_at"),
                pricing_status="priced" if nav is not None else "unavailable",
                limitations=limitations,
            ))

        total_value = sum(item.market_value or 0 for item in priced)
        if total_value > 0:
            priced = [
                item.model_copy(update={
                    "weight": round((item.market_value or 0) / total_value * 100.0, 4),
                })
                for item in priced
            ]
        return priced

    def get_account(self, customer_id: str) -> AccountSnapshot:
        account_row = self.store.ensure_account(customer_id)
        positions = self.list_positions(customer_id)
        cash = round_money(self._cash_of(account_row))
        frozen = round_money(float(account_row.get("frozen_balance") or 0))
        market_value = round_money(sum(item.market_value or 0 for item in positions))
        position_cost = round_money(sum(item.cost_amount for item in positions))
        position_pnl = round_money(sum(item.unrealized_pnl or 0 for item in positions))
        total_deposit = round_money(float(account_row.get("total_deposit") or 0))
        realized = round_money(self.store.realized_pnl_total(customer_id))
        total_assets = round_money(cash + frozen + market_value)
        total_pnl = round_money(total_assets - total_deposit)
        issues = [
            f"{item.product_code}:{item.pricing_status}"
            for item in positions
            if item.pricing_status != "priced"
        ]
        return AccountSnapshot(
            customer_id=str(customer_id).upper(),
            cash_balance=cash,
            frozen_balance=frozen,
            market_value=market_value,
            total_assets=total_assets,
            total_deposit=total_deposit,
            total_pnl=total_pnl,
            total_pnl_pct=_percent(total_pnl, total_deposit),
            realized_pnl=realized,
            position_pnl=position_pnl,
            position_cost=position_cost,
            position_count=len(positions),
            market_value_complete=not issues,
            pricing_issues=issues,
            updated_at=account_row.get("updated_at"),
            simulated=True,
            disclaimer=SIMULATED_DISCLAIMER,
        )

    def find_order_by_key(self, customer_id: str, idempotency_key: str) -> OrderView | None:
        row = self.store.find_order_by_key(customer_id, idempotency_key)
        if row is None:
            return None
        names = self._names_for([str(row.get("product_code"))])
        return _order_view(row, names)

    def list_orders(self, customer_id: str, limit: int = 100) -> list[OrderView]:
        rows = self.store.list_orders(customer_id, limit)
        names = self._names_for(str(row.get("product_code")) for row in rows)
        return [_order_view(row, names) for row in rows]

    def list_transactions(self, customer_id: str, limit: int = 100) -> list[TransactionView]:
        rows = self.store.list_transactions(customer_id, limit)
        return [
            TransactionView(
                txn_id=str(row.get("txn_id", "")),
                kind=str(row.get("kind", "deposit")),  # type: ignore[arg-type]
                amount=float(row.get("amount") or 0),
                balance_after=float(row.get("balance_after") or 0),
                ref_id=str(row.get("ref_id") or ""),
                note=str(row.get("note") or ""),
                created_at=row.get("created_at"),
            )
            for row in rows
        ]

    def deposit(
        self, customer_id: str, amount: float, *, idempotency_key: str = "",
    ) -> tuple[TransactionView, AccountSnapshot, bool]:
        cid = str(customer_id).upper()
        value = round_money(float(amount))
        if value <= 0:
            raise PortfolioError("充值金额必须大于 0", code="invalid_amount")
        if value > PORTFOLIO_MAX_DEPOSIT_AMOUNT:
            raise PortfolioError(
                f"单笔充值不得超过 {PORTFOLIO_MAX_DEPOSIT_AMOUNT:,.2f} 元",
                code="invalid_amount",
            )

        self.store.ensure_account(cid)
        existing = self.store.find_transaction_by_key(cid, idempotency_key)
        if existing is not None:
            return self._transaction_view(existing), self.get_account(cid), True

        try:
            with self.store.transaction() as session:
                account = self.store.lock_account(session, cid)
                if account is None:
                    raise PortfolioError("账户不存在", code="invalid_amount")
                balance = round_money(self._cash_of(account) + value)
                self.store.update_cash_balance(session, cid, balance)
                self.store.add_deposit(session, cid, value)
                inserted = self.store.insert_transaction(session, {
                    "txn_id": str(uuid.uuid4()),
                    "customer_id": cid,
                    "kind": "deposit",
                    "amount": value,
                    "balance_after": balance,
                    "ref_id": "",
                    "note": "模拟账户充值",
                    "idempotency_key": idempotency_key,
                })
        except Exception as exc:
            replayed = self._replay_deposit_conflict(cid, idempotency_key, exc)
            if replayed is not None:
                return replayed
            raise

        return self._transaction_view(inserted), self.get_account(cid), False

    @staticmethod
    def _transaction_view(row: dict[str, Any]) -> TransactionView:
        return TransactionView(
            txn_id=str(row.get("txn_id", "")),
            kind=str(row.get("kind", "deposit")),  # type: ignore[arg-type]
            amount=float(row.get("amount") or 0),
            balance_after=float(row.get("balance_after") or 0),
            ref_id=str(row.get("ref_id") or ""),
            note=str(row.get("note") or ""),
            created_at=row.get("created_at"),
        )

    def buy(
        self,
        customer_id: str,
        product_code: str,
        *,
        amount: float | None = None,
        shares: float | None = None,
        idempotency_key: str = "",
    ) -> TradeResult:
        cid = str(customer_id).upper()
        if (amount is None) == (shares is None):
            raise PortfolioError("请且只能提供申购金额或申购份额其中之一", code="invalid_amount")

        self.store.ensure_account(cid)
        replay = self._replay(cid, idempotency_key, product_code)
        if replay is not None:
            return replay

        product = self._product_or_raise(product_code)
        self._require_on_shelf(product)
        basic = product.get("basic_info") or {}
        name = str(basic.get("name") or "")
        quote = self._nav_or_raise(str(product_code).strip(), name)
        subscription_rate, _ = product_fee_rates(product)

        if amount is not None:
            value = round_money(float(amount))
            if value < PORTFOLIO_MIN_ORDER_AMOUNT:
                raise PortfolioError(
                    f"单笔申购金额不得低于 {PORTFOLIO_MIN_ORDER_AMOUNT:,.2f} 元",
                    code="invalid_amount",
                )
            breakdown = subscription_by_amount(value, quote.nav, subscription_rate)
        else:
            wanted = round_shares(float(shares or 0))
            if wanted <= 0:
                raise PortfolioError("申购份额必须大于 0", code="invalid_amount")
            breakdown = subscription_by_shares(wanted, quote.nav, subscription_rate)

        if breakdown.shares <= 0:
            raise PortfolioError("申购金额过低，不足以确认任何份额", code="invalid_amount")

        try:
            with self.store.transaction() as session:
                account = self.store.lock_account(session, cid)
                if account is None:
                    raise PortfolioError("账户不存在", code="invalid_amount")
                balance = self._cash_of(account)
                if balance < breakdown.cash_out:
                    raise PortfolioError(
                        f"可用资金不足：需 {breakdown.cash_out:,.2f} 元，"
                        f"当前可用 {balance:,.2f} 元",
                        code="insufficient_funds",
                    )
                position = self.store.get_position_row(session, cid, str(product_code).strip())
                old_shares = float(position.get("shares") or 0) if position else 0.0
                old_cost = round_money(float(position.get("cost_amount") or 0)) if position else 0.0
                new_shares = round_shares(old_shares + breakdown.shares)
                new_cost = round_money(old_cost + breakdown.cash_out)
                new_balance = round_money(balance - breakdown.cash_out)

                self.store.upsert_position(session, cid, str(product_code).strip(), new_shares, new_cost)
                self.store.update_cash_balance(session, cid, new_balance)
                inserted = self.store.insert_order(session, {
                    "order_id": str(uuid.uuid4()),
                    "customer_id": cid,
                    "product_code": str(product_code).strip(),
                    "side": "buy",
                    "shares": breakdown.shares,
                    "price": quote.nav,
                    "gross_amount": breakdown.invested,
                    "fee": breakdown.fee,
                    "net_amount": breakdown.cash_out,
                    "realized_pnl": None,
                    "fee_limitations": breakdown.limitations,
                    "idempotency_key": idempotency_key,
                })
                self.store.insert_transaction(session, {
                    "txn_id": str(uuid.uuid4()),
                    "customer_id": cid,
                    "kind": "buy",
                    "amount": round_money(-breakdown.cash_out),
                    "balance_after": new_balance,
                    "ref_id": str(product_code).strip(),
                    "note": f"申购 {name}".strip(),
                    "idempotency_key": "",
                })
        except Exception as exc:
            replayed = self._replay_order_conflict(cid, idempotency_key, str(product_code).strip(), exc)
            if replayed is not None:
                return replayed
            raise

        return self._result(cid, inserted, str(product_code).strip())

    def sell(
        self,
        customer_id: str,
        product_code: str,
        *,
        shares: float | None = None,
        all_shares: bool = False,
        idempotency_key: str = "",
    ) -> TradeResult:
        cid = str(customer_id).upper()
        code = str(product_code).strip()
        if not all_shares and shares is None:
            raise PortfolioError("请提供赎回份额或指定全部赎回", code="invalid_amount")

        self.store.ensure_account(cid)
        replay = self._replay(cid, idempotency_key, code)
        if replay is not None:
            return replay

        product = self._product_or_raise(code)
        basic = product.get("basic_info") or {}
        name = str(basic.get("name") or "")
        quote = self._nav_or_raise(code, name)
        _, redemption_rate = product_fee_rates(product)

        try:
            with self.store.transaction() as session:
                account = self.store.lock_account(session, cid)
                if account is None:
                    raise PortfolioError("账户不存在", code="invalid_amount")
                position = self.store.get_position_row(session, cid, code)
                held = float(position.get("shares") or 0) if position else 0.0
                if held <= 0 or position is None:
                    raise PortfolioError(f"没有 {name or code} 的持仓", code="no_position")

                if all_shares:
                    wanted = held
                else:
                    wanted = round_shares(float(shares or 0))
                    if wanted <= 0:
                        raise PortfolioError("赎回份额必须大于 0", code="invalid_amount")
                    if wanted > held:
                        raise PortfolioError(
                            f"持仓份额不足：需 {wanted} 份，当前持有 {held} 份",
                            code="insufficient_shares",
                        )

                held_cost = round_money(float(position.get("cost_amount") or 0))
                if wanted >= held:
                    cost_out = held_cost
                else:
                    cost_out = round_money(held_cost * (wanted / held))

                breakdown = redemption(wanted, quote.nav, redemption_rate)
                realized = round_money(breakdown.cash_in - cost_out)
                remaining_shares = round_shares(held - wanted)
                remaining_cost = round_money(held_cost - cost_out)
                if remaining_shares <= 0:
                    remaining_shares, remaining_cost = 0.0, 0.0
                balance = round_money(self._cash_of(account) + breakdown.cash_in)

                self.store.upsert_position(session, cid, code, remaining_shares, remaining_cost)
                self.store.update_cash_balance(session, cid, balance)
                inserted = self.store.insert_order(session, {
                    "order_id": str(uuid.uuid4()),
                    "customer_id": cid,
                    "product_code": code,
                    "side": "sell",
                    "shares": wanted,
                    "price": quote.nav,
                    "gross_amount": breakdown.gross,
                    "fee": breakdown.fee,
                    "net_amount": breakdown.cash_in,
                    "realized_pnl": realized,
                    "fee_limitations": breakdown.limitations,
                    "idempotency_key": idempotency_key,
                })
                self.store.insert_transaction(session, {
                    "txn_id": str(uuid.uuid4()),
                    "customer_id": cid,
                    "kind": "sell",
                    "amount": breakdown.cash_in,
                    "balance_after": balance,
                    "ref_id": code,
                    "note": f"赎回 {name}".strip(),
                    "idempotency_key": "",
                })
        except Exception as exc:
            replayed = self._replay_order_conflict(cid, idempotency_key, code, exc)
            if replayed is not None:
                return replayed
            raise

        return self._result(cid, inserted, code)

    def liquidate_all(self, customer_id: str) -> LiquidationResult:
        cid = str(customer_id).upper()
        positions = self.store.list_positions(cid)
        orders: list[OrderView] = []
        cleared: list[str] = []
        failed: dict[str, str] = {}
        for row in positions:
            code = str(row.get("product_code"))
            try:
                outcome = self.sell(cid, code, all_shares=True)
                orders.append(outcome.order)
                cleared.append(code)
            except PortfolioError as exc:
                if exc.code == "pricing_unavailable":
                    failed[code] = exc.code
                else:
                    logger.warning("liquidate_failed code=%s error=%s", code, exc)
                    failed[code] = exc.code
            except Exception as exc:  # noqa: BLE001
                logger.warning("liquidate_failed code=%s error=%s", code, exc)
                failed[code] = getattr(exc, "code", "portfolio_error")
        return LiquidationResult(
            orders=orders,
            cleared_codes=cleared,
            failed=failed,
            total_cash_in=round_money(sum(item.net_amount for item in orders)),
            total_realized_pnl=round_money(
                sum(item.realized_pnl or 0 for item in orders)
            ),
            account=self.get_account(cid),
        )

    def _replay_deposit_conflict(
        self, customer_id: str, idempotency_key: str, exc: BaseException,
    ) -> tuple[TransactionView, AccountSnapshot, bool] | None:
        if not idempotency_key or not is_unique_violation(exc):
            return None
        existing = self.store.find_transaction_by_key(customer_id, idempotency_key)
        if existing is None:
            return None
        return self._transaction_view(existing), self.get_account(customer_id), True

    def _replay_order_conflict(
        self, customer_id: str, idempotency_key: str, product_code: str, exc: BaseException,
    ) -> TradeResult | None:
        if not idempotency_key or not is_unique_violation(exc):
            return None
        return self._replay(customer_id, idempotency_key, product_code)

    def _replay(
        self, customer_id: str, idempotency_key: str, product_code: str,
    ) -> TradeResult | None:
        if not idempotency_key:
            return None
        row = self.store.find_order_by_key(customer_id, idempotency_key)
        if row is None:
            return None
        return self._result(customer_id, row, str(row.get("product_code") or product_code), replay=True)

    def _result(
        self, customer_id: str, order_row: dict[str, Any], product_code: str,
        *, replay: bool = False,
    ) -> TradeResult:
        code = str(product_code).strip()
        names = self._names_for([code])
        position = next(
            (
                item for item in self.list_positions(customer_id)
                if item.product_code == code
            ),
            None,
        )
        return TradeResult(
            order=_order_view(order_row, names),
            account=self.get_account(customer_id),
            position=position,
            idempotent_replay=replay,
        )


_service: PortfolioService | None = None


def get_portfolio_service() -> PortfolioService:
    global _service
    if _service is None:
        from app.services.product_service import ProductLibraryNavSource
        from app.crud.product_crud import get_product_library
        from app.crud.portfolio_crud import get_portfolio_store

        library = get_product_library()
        _service = PortfolioService(PortfolioDeps(
            store=get_portfolio_store(),
            library=library,
            nav_source=ProductLibraryNavSource(library),
        ))
    return _service


__all__ = [
    "PortfolioDeps",
    "PortfolioError",
    "PortfolioService",
    "RedemptionBreakdown",
    "SIMULATED_DISCLAIMER",
    "SubscriptionBreakdown",
    "get_portfolio_service",
    "redemption",
    "round_money",
    "round_shares",
    "subscription_by_amount",
    "subscription_by_shares",
]
