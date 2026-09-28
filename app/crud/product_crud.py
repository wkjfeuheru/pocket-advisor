"""产品库数据访问层（``finance.products`` 及持仓/业绩子表）。

热路径只组装 ``basic_info`` / ``performance`` / ``fee``，不加载十大重仓。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from app.db.session import ensure_schema, session_scope
from app.core.config import PRODUCT_PERFORMANCE_FRESHNESS_DAYS
from app.db.models.product import Product, ProductHolding, ProductPerformance

_FEE_FIELDS = ("management_fee", "custody_fee", "subscription_fee", "redemption_fee")

_PRODUCT_FIELDS = (
    "code", "name", "type", "establish_date", "scale", "manager", "company",
    "management_fee", "custody_fee", "subscription_fee", "redemption_fee",
    "risk_level", "recommended_holding_period", "investment_target", "investment_strategy",
    "min_investment",
)
_TEXT_DEFAULTS = {
    "name", "type", "establish_date", "manager", "company",
    "redemption_fee", "risk_level", "recommended_holding_period",
    "investment_target", "investment_strategy",
}


def _freshness(as_of: str, max_age_days: int) -> str:
    """按截止日期判断区块新鲜度；无法解析时保守返回 unknown。"""
    if not as_of:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    return "stale" if datetime.now(timezone.utc) - parsed > timedelta(days=max_age_days) else "fresh"


def _performance_dict(row: ProductPerformance | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "nav": row.nav,
        "return_1m": row.return_1m,
        "return_3m": row.return_3m,
        "return_6m": row.return_6m,
        "return_1y": row.return_1y,
        "return_3y": row.return_3y,
        "max_drawdown": row.max_drawdown,
        "volatility": row.volatility,
        "sharpe_ratio": row.sharpe_ratio,
        "update_date": row.update_date,
    }


class PostgresProductLibrary:
    """产品库的 PostgreSQL 存储。"""

    def query_by_codes(self, codes: list[str]) -> list[dict[str, Any]]:
        """按代码批量读取产品，保留输入顺序并忽略未找到的代码。

        固定两次查询（产品一次、最新业绩一次），不随标的数增长。
        """
        normalized = list(dict.fromkeys(str(code).strip() for code in codes if str(code).strip()))
        if not normalized:
            return []
        ensure_schema()
        with session_scope() as session:
            rows = session.scalars(
                select(Product).where(Product.code.in_(normalized))
            ).all()
            by_code = {row.code: row for row in rows}
            # 业绩表是追加日志，同一次读只取每个代码 id 最大的一条。
            latest: dict[str, ProductPerformance] = {}
            for row in session.scalars(
                select(ProductPerformance)
                .where(ProductPerformance.product_code.in_(normalized))
                .order_by(ProductPerformance.id.asc())
            ).all():
                latest[row.product_code] = row
            return [
                _build_product_row(by_code[code], latest.get(code))
                for code in normalized
                if code in by_code
            ]

    def query_by_code(self, code: str) -> dict[str, Any] | None:
        ensure_schema()
        with session_scope() as session:
            row = session.get(Product, str(code).strip())
            if row is None:
                return None
            performance_row = session.scalars(
                select(ProductPerformance)
                .where(ProductPerformance.product_code == row.code)
                .order_by(ProductPerformance.id.desc())
                .limit(1)
            ).first()
            return _build_product_row(row, performance_row)

    def list_products(
        self, product_type: str = "fund", *, include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """货架商品摘要。默认只返回上架的基金；``all`` 不按类型过滤。"""
        ensure_schema()
        kind = (product_type or "fund").strip()
        statement = select(
            Product.code, Product.name, Product.type, Product.scale, Product.is_active,
        )
        if kind.lower() != "all":
            statement = statement.where(Product.type == kind)
        if not include_inactive:
            statement = statement.where(Product.is_active.is_(True))
        statement = statement.order_by(Product.code)
        with session_scope() as session:
            rows = session.execute(statement).all()
            return [
                {
                    "code": row.code,
                    "name": row.name,
                    "type": row.type,
                    "scale": row.scale,
                    "is_active": bool(row.is_active),
                }
                for row in rows
            ]

    def list_products_for_nav(self) -> list[dict[str, Any]]:
        """净值播种/导入用的产品池：代码、名称、类型与风险等级。

        只要风险等级，不要整份产品详情：合成数据按风险分档波动，而 ``list_products``
        返回的摘要里没有这个字段。``risk_level`` 在 ``finance.products`` 上（不在业绩表），
        缺失时给空串，由调用方回退到默认档位——货架上确实有产品没有这个字段，
        不该因此报错。
        """
        ensure_schema()
        with session_scope() as session:
            rows = session.execute(
                select(Product.code, Product.name, Product.type, Product.risk_level)
                .where(Product.is_active.is_(True))
                .order_by(Product.code)
            ).all()
            return [
                {
                    "code": row.code,
                    "name": row.name,
                    "type": row.type,
                    "risk_level": row.risk_level or "",
                }
                for row in rows
            ]

    def set_product_active(self, code: str, active: bool) -> bool:
        """上架/下架商品（软状态），返回是否命中商品。"""
        ensure_schema()
        with session_scope() as session:
            row = session.get(Product, str(code).strip())
            if row is None:
                return False
            row.is_active = bool(active)
            return True

    def upsert_product(self, data: dict[str, Any]) -> bool:
        code = str(data.get("code", "")).strip()
        if not code or not str(data.get("name", "")).strip():
            return False
        ensure_schema()
        with session_scope() as session:
            row = session.get(Product, code)
            if row is None:
                row = Product(code=code)
                session.add(row)
            for field in _PRODUCT_FIELDS:
                if field == "code":
                    continue
                value = data.get(field, "" if field in _TEXT_DEFAULTS else None)
                setattr(row, field, value)
            row.name = str(data.get("name", "")).strip()
            return True

    def upsert_holdings(self, code: str, holdings: list[dict[str, Any]]) -> bool:
        ensure_schema()
        with session_scope() as session:
            if session.get(Product, code) is None:
                return False
            session.execute(
                delete(ProductHolding).where(ProductHolding.product_code == code)
            )
            for item in holdings:
                session.add(ProductHolding(
                    product_code=code,
                    stock_name=item.get("stock_name", item.get("name", "")),
                    stock_code=item.get("stock_code", item.get("code", "")),
                    weight=item.get("weight"),
                    rank=item.get("rank"),
                    report_date=item.get("report_date", ""),
                ))
            return True

    def upsert_performance(self, code: str, perf: dict[str, Any]) -> bool:
        """追加一条业绩/净值记录（读取端按 ``id`` 最大取最新）。"""
        ensure_schema()
        with session_scope() as session:
            if session.get(Product, code) is None:
                return False
            session.add(ProductPerformance(
                product_code=code,
                nav=perf.get("nav"),
                return_1m=perf.get("return_1m", perf.get("1m")),
                return_3m=perf.get("return_3m", perf.get("3m")),
                return_6m=perf.get("return_6m", perf.get("6m")),
                return_1y=perf.get("return_1y", perf.get("1y")),
                return_3y=perf.get("return_3y", perf.get("3y")),
                max_drawdown=perf.get("max_drawdown"),
                volatility=perf.get("volatility"),
                sharpe_ratio=perf.get("sharpe_ratio"),
                update_date=perf.get("update_date", ""),
            ))
            return True

    def list_holdings(self, code: str) -> list[dict[str, Any]]:
        """读取某商品的重仓。没有记录时返回空列表，不编造持仓。"""
        ensure_schema()
        with session_scope() as session:
            rows = session.scalars(
                select(ProductHolding)
                .where(ProductHolding.product_code == str(code).strip())
                .order_by(ProductHolding.rank.asc().nulls_last(), ProductHolding.id.asc())
            ).all()
            return [
                {
                    "stock_name": row.stock_name,
                    "stock_code": row.stock_code,
                    "weight": row.weight,
                    "rank": row.rank,
                    "report_date": row.report_date,
                    "source": "postgresql",
                    "as_of": row.report_date or "",
                }
                for row in rows
            ]

    def delete_product(self, code: str) -> bool:
        ensure_schema()
        with session_scope() as session:
            row = session.get(Product, str(code).strip())
            if row is None:
                return False
            session.delete(row)
            return True


def _build_product_row(row: Product, performance_row: ProductPerformance | None) -> dict[str, Any]:
    """组装货架/成交所需区块；不加载十大重仓。

    纯函数，不碰 session：批量与单条读取共用同一份字段口径，避免两条读路径漂移。
    """
    basic_info = {
        "code": row.code,
        "name": row.name,
        "type": row.type,
        "establish_date": row.establish_date,
        "scale": row.scale,
        "manager": row.manager,
        "company": row.company,
        "redemption_fee": row.redemption_fee,
        "risk_level": row.risk_level,
        "recommended_holding_period": row.recommended_holding_period,
        "investment_target": row.investment_target,
        "investment_strategy": row.investment_strategy,
        # NUMERIC 读回是 Decimal，必须转成 float；未披露保持 None（未知），不是 0。
        "min_investment": float(row.min_investment) if row.min_investment is not None else None,
        "is_active": bool(row.is_active),
        "source": "postgresql",
        "as_of": "",
        "freshness": "unknown",
    }
    fee = {field: getattr(row, field) for field in _FEE_FIELDS}
    performance_data = _performance_dict(performance_row)
    performance_as_of = str(performance_data.get("update_date") or "")

    return {
        "basic_info": basic_info,
        "performance": {
            **performance_data,
            "source": "postgresql",
            "as_of": performance_as_of,
            "freshness": _freshness(performance_as_of, PRODUCT_PERFORMANCE_FRESHNESS_DAYS),
        },
        "fee": fee,
    }


_product_library: PostgresProductLibrary | None = None


def get_product_library() -> PostgresProductLibrary:
    global _product_library
    if _product_library is None:
        _product_library = PostgresProductLibrary()
    return _product_library


__all__ = ["PostgresProductLibrary", "get_product_library"]
