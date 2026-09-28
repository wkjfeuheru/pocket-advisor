"""AI 账本：以用户持仓建仓、差额成交、单位净值、现金流同步与幂等。

这个模块承载设计文档里最关键的不变量：

**AI 账本永不写用户数据。** 采纳方案只在 ``ai_*`` 表里产生成交与持仓，
``finance.positions`` / ``finance.orders`` / ``finance.cash_transactions`` 一行都不动。
用户持仓是事实，AI 账本是反事实。

三条容易写错的约定：

1. **账户建立不产生成交。** 起点就是用户当时的真实持仓——"存量调仓"口径问的是
   "听了 AI 会不会更好"，而不是"从零重来会怎样"。
2. **采纳后单位净值的变化只能来自费用。** 若沿用采纳前的总资产值当分母、却按成交后
   的股数计价，单位净值会凭空跳升几个百分点（真实市场没涨，钱却多了），整条对比
   曲线就废了。这里显式用"采纳后总资产"重算份额，把这个坑堵死。
3. **费用使实际权重略低于目标。** 目标金额按采纳前总资产计算，买卖费用吃掉现金，
   所以成交后权重必然略偏低。这是真实摩擦，不是 bug。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Protocol, Sequence

from app.core.security import UniqueConstraintError

#: 视为外部现金流、需要按单位净值申购/赎回的类型。
CASH_FLOW_KINDS = ("deposit", "withdraw")


class LedgerError(ValueError):
    """AI 账本操作不合法。``code`` 便于上层映射 HTTP 状态。"""

    def __init__(self, message: str, *, code: str = "invalid_ledger_op") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TradeRecord:
    """一笔 AI 成交。

    ``nav_as_of`` 记录用的是哪天的净值，永不混淆口径。

    两级幂等键，缺一不可：

    - ``adoption_key``：**一次采纳**的键，同一次采纳产生的所有成交共用它，用于
      "这个请求是不是已经处理过"的重放判定。
    - ``trade_key``：``adoption_key:product_code``，**单笔成交**的键，落到
      ``UNIQUE (customer_id, trade_key)`` 上。若所有成交共用 adoption_key，
      第一笔插入成功、第二笔就会撞唯一约束，被误判成"重复请求"而静默回滚 —— 这正是
      "采纳一个三标的方案却只成交了一笔"的成因。
    """

    customer_id: str
    product_code: str
    side: str
    shares: float
    price: float
    gross_amount: float
    fee: float
    net_amount: float
    plan_id: str
    nav_as_of: date
    data_source: str = "real"
    adoption_key: str = ""
    trade_key: str = ""


@dataclass
class AdoptionResult:
    """一次采纳的结果。``replayed=True`` 表示幂等重放，没有产生新成交。"""

    cash_balance: float
    total_assets: float
    total_units: float
    unit_nav: float
    total_fee: float
    trades: list[TradeRecord] = field(default_factory=list)
    replayed: bool = False


@dataclass
class CashFlowResult:
    """一次入金/出金的结果。"""

    cash_balance: float
    total_assets: float
    total_units: float
    unit_nav: float
    units_delta: float
    kind: str


class LedgerStore(Protocol):
    """AI 账本持久化的窄接口。只暴露本模块需要的操作。"""

    def establish_account(
        self, customer_id: str, *, initial_capital: float, opening_plan_id: str, nav_as_of: date
    ) -> dict: ...

    def get_account(self, customer_id: str) -> dict | None: ...

    def update_account(self, customer_id: str, **fields) -> dict: ...

    def get_position(self, customer_id: str, product_code: str) -> dict | None: ...

    def list_positions(self, customer_id: str) -> list[dict]: ...

    def upsert_position(
        self, customer_id: str, product_code: str, *, shares: float, cost_amount: float
    ) -> None: ...

    def insert_trade(self, record: Mapping[str, object]) -> dict: ...

    def find_trade_by_key(self, customer_id: str, adoption_key: str) -> dict | None: ...

    def mark_plan_adopted(self, plan_id: str, *, adopted_at: datetime) -> None: ...

    def get_plan(self, plan_id: str) -> dict | None: ...


def _positive(value: float, *, field_name: str) -> float:
    amount = float(value)
    if amount <= 0:
        raise LedgerError(f"{field_name} 必须为正: {value!r}", code="bad_amount")
    return amount


class LedgerService:
    """AI 账户的用例层。"""

    def __init__(self, store: LedgerStore, *, clock: object | None = None) -> None:
        self._store = store
        self._clock = clock

    def _now(self) -> datetime:
        if self._clock is not None and hasattr(self._clock, "now"):
            return self._clock.now()  # type: ignore[no-any-return]
        return datetime.now()

    # ── 查询 ───────────────────────────────────────────────────────────
    def has_account(self, customer_id: str) -> bool:
        """AI 账户是否已建立。

        采纳流程靠它判断"这次是不是第一次采纳"——首次要镜像用户持仓建仓，
        之后绝不能重建，否则历史净值曲线会断掉。
        """
        return self._store.get_account(customer_id) is not None

    def account(self, customer_id: str) -> dict | None:
        """读取 AI 账户快照；不存在返回 ``None``。"""
        return self._store.get_account(customer_id)

    # ── 建立账户 ───────────────────────────────────────────────────────
    def establish(
        self,
        *,
        customer_id: str,
        initial_capital: float,
        holdings_summary: Mapping[str, float],
        nav_as_of: date,
        navs: Mapping[str, float] | None = None,
        opening_plan_id: str = "",
    ) -> dict:
        """镜像用户当前持仓建立 AI 账户。不产生任何成交。

        份额必须按 ``市值 / 净值`` 算，不能把市值直接当份额——否则同一个 200 元的
        持仓在净值 2.00 时会被当成 200 份（真实是 100 份），后续所有差额成交全错。
        拿不到净值的标的（理财伪代码）退回 1 元/份镜像，并在估值时按成本计价。
        """
        capital = _positive(initial_capital, field_name="initial_capital")
        existing = self._store.get_account(customer_id)
        if existing is not None:
            return existing

        prices = dict(navs or {})
        account = self._store.establish_account(
            customer_id,
            initial_capital=capital,
            opening_plan_id=opening_plan_id,
            nav_as_of=nav_as_of,
        )
        for product_code, market_value in sorted(holdings_summary.items()):
            value = float(market_value)
            if value <= 0:
                continue
            price = float(prices.get(product_code) or 0.0)
            shares = value / price if price > 0 else value
            self._store.upsert_position(
                customer_id, product_code, shares=shares, cost_amount=value
            )
        return account

    # ── 采纳方案 ───────────────────────────────────────────────────────
    def adopt_plan(
        self,
        *,
        customer_id: str,
        plan_id: str,
        weights: Mapping[str, float],
        holdings: Mapping[str, Mapping[str, object]],
        navs: Mapping[str, float],
        nav_as_of: date,
        fee_buy: float = 0.0015,
        fee_sell: float = 0.005,
        idempotency_key: str = "",
    ) -> AdoptionResult:
        """按目标权重从当前持仓做差额调仓，只在 ``ai_*`` 表留下成交。"""
        account = self._store.get_account(customer_id)
        if account is None:
            raise LedgerError(f"AI 账户不存在: {customer_id}", code="no_account")

        # 幂等分支一：调用方显式给了幂等键，且该键已成交过 → 重放，不重复下单。
        # 只有显式键才参与这个分支：若用 plan_id 派生键，把「同方案换键再采」也误判成
        # 重放，就永远走不到下面的 already_adopted 分支了。
        if idempotency_key and self._store.find_trade_by_key(customer_id, idempotency_key) is not None:
            return self._replay(customer_id)

        # 幂等分支二：同一方案已被采纳 → 无论键是什么都拒绝（重复/并发采纳）。
        plan = self._store.get_plan(plan_id)
        if plan is not None and plan.get("status") == "adopted" and plan.get("adopted_at"):
            raise LedgerError(
                f"方案已被采纳，不可重复采纳: {plan_id}", code="already_adopted"
            )

        key = idempotency_key or f"adopt:{plan_id}"

        cash = float(account["cash_balance"])
        total_fee = 0.0
        trades: list[TradeRecord] = []

        target_codes = {code for code in weights if code != "cash"}
        all_codes = target_codes | set(holdings)

        # ── 先做整体可行性检查，再动任何一笔 ────────────────────────────
        # 必须在循环前做完：否则"第一笔成交成功、第二笔因缺净值报错"会留下一个
        # **部分采纳**的组合（实测过），而调用方只看到一个异常，以为什么都没发生。
        for product_code in sorted(all_codes):
            current_row = holdings.get(product_code) or {}
            if not bool(current_row.get("priceable", True)):
                continue
            target_weight = float(weights.get(product_code, 0.0))
            current_value = float(current_row.get("market_value") or 0.0)
            target_value = float(account["initial_capital"]) * target_weight
            needs_trade = (
                product_code in target_codes or current_value > 0
            ) and abs(target_value - current_value) >= 0.01
            if needs_trade and not navs.get(product_code):
                raise LedgerError(
                    f"{product_code} 缺少可用净值，无法成交", code="missing_nav"
                )

        for product_code in sorted(all_codes):
            current_row = holdings.get(product_code) or {}
            current_value = float(current_row.get("market_value") or 0.0)
            priceable = bool(current_row.get("priceable", True))
            target_weight = float(weights.get(product_code, 0.0))

            if not priceable:
                # 不可定价标的：保持不动，不交易、不重估。
                continue

            price = navs.get(product_code)
            if price is None or float(price) <= 0:
                if target_weight > 0:
                    raise LedgerError(
                        f"{product_code} 缺少可用净值，无法成交", code="missing_nav"
                    )
                continue

            target_value = float(account["initial_capital"]) * target_weight

            # dividend/拆分不在本期范围：目标金额按采纳前总资产直接计算。
            delta = target_value - current_value
            if abs(delta) < 0.01:
                continue

            price = float(price)
            side = "buy" if delta > 0 else "sell"
            gross = abs(delta)
            shares = gross / price
            fee = gross * (fee_buy if side == "buy" else fee_sell)
            total_fee += fee

            if side == "buy":
                cash -= gross + fee
            else:
                cash += gross - fee

            existing = self._store.get_position(customer_id, product_code) or {}
            held = float(existing.get("shares") or 0.0)
            cost = float(existing.get("cost_amount") or 0.0)
            if side == "buy":
                new_shares = held + shares
                new_cost = cost + gross
            else:
                # 卖出时成本必须**按比例**扣减，不能按成交额扣。
                # 按成交额扣会在"成本价与现价不同"时凭空销毁或创造本金，
                # 进而让单位净值跳变（例如卖出成本 800 的仓位只收回 300，
                # 本金被扣掉 300 却只减少 300 的市值，净值就假涨）。
                sold_fraction = min(1.0, shares / held) if held > 0 else 1.0
                new_shares = max(0.0, held - shares)
                new_cost = cost * (1.0 - sold_fraction)
            self._store.upsert_position(
                customer_id, product_code, shares=new_shares, cost_amount=new_cost
            )

            record = TradeRecord(
                customer_id=customer_id,
                product_code=product_code,
                side=side,
                shares=shares,
                price=price,
                gross_amount=gross,
                fee=fee,
                net_amount=gross + fee if side == "buy" else gross - fee,
                plan_id=plan_id,
                nav_as_of=nav_as_of,
                adoption_key=key,
                trade_key=f"{key}:{product_code}",
            )
            try:
                self._store.insert_trade(record.__dict__)
            except UniqueConstraintError:
                # 并发下另一个请求抢到了同一笔成交键：整体幂等重放。
                return self._replay(customer_id)
            trades.append(record)

        # 采纳后重算份额。
        #
        # total_units 是**份额**，不是本金，也不是总资产。两个错误写法都会让净值失真：
        #   - total_units = 总资产 → unit_nav 恒为 1.0，费用与调仓摩擦全部消失；
        #   - total_units = Σ成本 + 费用 → 建仓时成本是按**市值**镜像的（可能远高于
        #     本金），份额被高估，净值反而假涨（实测 +4.67%）。
        #
        # 正确做法：份额是初始本金的固定换算（1 份 = 1 元），采纳调仓只改变资产与费用，
        # 不改变份额。于是 unit_nav = (市值 + 现金) / 份额，费用与买卖价差自然体现为
        # 净值下降，而调仓本身不会凭空抬高净值。
        positions = self._store.list_positions(customer_id)
        market_value = 0.0
        for row in positions:
            product_code = str(row["product_code"])
            shares = float(row.get("shares") or 0.0)
            price = navs.get(product_code)
            market_value += shares * float(price) if price else float(row.get("cost_amount") or 0.0)

        total_assets = market_value + cash
        total_units = float(account["total_units"])
        unit_nav = 1.0 if total_units <= 0 else total_assets / total_units

        account = self._store.update_account(
            customer_id,
            cash_balance=cash,
            total_units=total_units,
            unit_nav=unit_nav,
            nav_as_of=nav_as_of,
        )
        self._store.mark_plan_adopted(plan_id, adopted_at=self._now())

        return AdoptionResult(
            cash_balance=cash,
            total_assets=total_assets,
            total_units=total_units,
            unit_nav=float(account["unit_nav"]),
            total_fee=total_fee,
            trades=trades,
            replayed=False,
        )

    # ── 现金流同步 ─────────────────────────────────────────────────────
    def apply_cash_flow(
        self,
        *,
        customer_id: str,
        kind: str,
        amount: float,
        nav_as_of: date,
    ) -> CashFlowResult:
        """入金按当日单位净值申购，出金按比例赎回。入金本身不产生收益。"""
        if kind not in CASH_FLOW_KINDS:
            raise LedgerError(f"未知现金流类型: {kind}", code="bad_kind")
        value = _positive(amount, field_name="amount")

        account = self._store.get_account(customer_id)
        if account is None:
            raise LedgerError(f"AI 账户不存在: {customer_id}", code="no_account")

        unit_nav = float(account["unit_nav"] or 0.0)
        if unit_nav <= 0:
            raise LedgerError("单位净值非法，无法处理现金流", code="bad_unit_nav")

        units_delta = value / unit_nav
        cash = float(account["cash_balance"])
        total_units = float(account["total_units"])

        if kind == "deposit":
            cash += value
            total_units += units_delta
            inflow = float(account["cumulative_inflow"]) + value
            outflow = float(account["cumulative_outflow"])
        else:
            cash -= value
            total_units -= units_delta
            inflow = float(account["cumulative_inflow"])
            outflow = float(account["cumulative_outflow"]) + value

        # 单位净值＝(原总资产 ± 现金流) / 新份额；现金流不改变净值。
        previous_assets = unit_nav * float(account["total_units"])
        total_assets = previous_assets + (value if kind == "deposit" else -value)
        new_nav = 1.0 if total_units <= 0 else total_assets / total_units

        self._store.update_account(
            customer_id,
            cash_balance=cash,
            total_units=total_units,
            unit_nav=new_nav,
            cumulative_inflow=inflow,
            cumulative_outflow=outflow,
            nav_as_of=nav_as_of,
        )

        return CashFlowResult(
            cash_balance=cash,
            total_assets=total_assets,
            total_units=total_units,
            unit_nav=new_nav,
            units_delta=units_delta,
            kind=kind,
        )

    # ── 内部 ───────────────────────────────────────────────────────────
    def _replay(self, customer_id: str) -> AdoptionResult:
        account = self._store.get_account(customer_id) or {}
        cash = float(account.get("cash_balance") or 0.0)
        unit_nav = float(account.get("unit_nav") or 1.0)
        total_units = float(account.get("total_units") or 0.0)
        market_value = sum(
            float(row.get("cost_amount") or 0.0) for row in self._store.list_positions(customer_id)
        )
        return AdoptionResult(
            cash_balance=cash,
            total_assets=market_value + cash,
            total_units=total_units,
            unit_nav=unit_nav,
            total_fee=0.0,
            trades=[],
            replayed=True,
        )


__all__ = [
    "CASH_FLOW_KINDS",
    "AdoptionResult",
    "CashFlowResult",
    "LedgerError",
    "LedgerService",
    "LedgerStore",
    "TradeRecord",
]
