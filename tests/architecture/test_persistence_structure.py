"""守卫持久化接缝：

- 所有数据访问模块共享 ``app.db.session`` 的 ORM session 工厂与幂等建表入口；
- schema 的应用顺序只在 ``app.db.schema`` 声明一处，而且与 ``migrations/`` 实际文件一致；
- 各 crud 模块不内联 SQLAlchemy engine（避免绕过共享接缝）。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_MODULES = (
    "app.crud.user_crud",
    "app.crud.product_crud",
    "app.crud.portfolio_crud",
    "app.crud.stock_feature_crud",
)
STORE_PATHS = (
    REPO_ROOT / "app" / "crud" / "user_crud.py",
    REPO_ROOT / "app" / "crud" / "product_crud.py",
    REPO_ROOT / "app" / "crud" / "portfolio_crud.py",
    REPO_ROOT / "app" / "crud" / "stock_feature_crud.py",
)


def test_store_modules_reuse_shared_session_seam() -> None:
    """每个 crud 模块都从 ``app.db.session`` 取会话/建表接缝，而非自建 engine。"""
    from app.db import session as db

    for module_name in STORE_MODULES:
        module = __import__(module_name, fromlist=["*"])
        assert getattr(module, "session_scope") is db.session_scope, (
            f"{module_name} 必须复用 app.db.session.session_scope"
        )
        assert getattr(module, "ensure_schema") is db.ensure_schema, (
            f"{module_name} 必须复用 app.db.session.ensure_schema"
        )


def test_models_share_one_declarative_base() -> None:
    from app.db.models.user import User
    from app.db.base import Base
    from app.db.models.portfolio import Account
    from app.db.models.product import Product

    assert issubclass(User, Base)
    assert issubclass(Product, Base)
    assert issubclass(Account, Base)
    assert User.metadata is Base.metadata


def test_all_store_modules_import_session_scope() -> None:
    for path in STORE_PATHS:
        text = path.read_text(encoding="utf-8")
        assert "from app.db.session import" in text, f"{path.name} 必须从 app.db.session 导入会话接缝"
        assert "create_engine" not in text, f"{path.name} 不得自行创建 engine"


def test_schema_order_lives_in_one_place_and_matches_files() -> None:
    from app.db import schema

    sql_dir = schema.sql_dir()
    for filename in schema.SCHEMA_APPLY_ORDER:
        assert (sql_dir / filename).is_file(), f"缺少迁移脚本 {filename}"
    for filename in schema.SEED_SCHEMA_FILES:
        assert (sql_dir / filename).is_file(), f"缺少种子脚本 {filename}"
    # 应用顺序里不得重复文件。
    assert len(set(schema.SCHEMA_APPLY_ORDER)) == len(schema.SCHEMA_APPLY_ORDER)


def test_no_undefined_global_in_persistence_layer() -> None:
    """app.db.session 与各 crud 模块不得出现未定义全局名。"""
    import builtins

    def unresolved(path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = set(dir(builtins))
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    defined.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    used.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        return sorted(used - defined - {"__file__", "__name__", "__doc__"})

    paths = [REPO_ROOT / "app" / "db" / "session.py", *STORE_PATHS]
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {', '.join(names)}"
        for path in paths
        for names in (unresolved(path),)
        if names
    ]
    assert not offenders, "\n".join(offenders)
