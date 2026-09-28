"""ORM 声明式基类。

表结构的唯一事实源仍是 ``migrations/*.sql``。模型只做映射，不通过
``create_all`` 建表。业务模型与已落地的投顾表由 :func:`load_all_models` 导入。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""


def load_all_models() -> None:
    """导入已有迁移对应的模型，供 metadata 发现。"""
    from app.db.models import advisor_profile as _advisor_profile  # noqa: F401
    from app.db.models import agent_session as _agent_session  # noqa: F401
    from app.db.models import agent_evidence as _agent_evidence  # noqa: F401
    from app.db.models import portfolio as _portfolio  # noqa: F401
    from app.db.models import product as _product  # noqa: F401
    from app.db.models import stock_feature as _stock_feature  # noqa: F401
    from app.db.models import user as _user  # noqa: F401


__all__ = ["Base", "load_all_models"]
