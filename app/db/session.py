"""SQLAlchemy engine 与 session 工厂。

不通过 ORM ``create_all`` 建表。首次使用时用 ``ensure_schema()`` 幂等应用 DDL。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings_models import load_postgres_settings


_engine = None
_engine_lock = threading.Lock()
_session_factory: sessionmaker[Session] | None = None
_schema_ready = False
_schema_lock = threading.Lock()


def get_engine():
    """返回惰性构造的全局 engine。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                settings = load_postgres_settings()
                _engine = create_engine(
                    settings.sqlalchemy_url(),
                    future=True,
                    pool_pre_ping=True,
                    connect_args={"connect_timeout": settings.connect_timeout},
                )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """返回全局 session 工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False, future=True,
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个提交/回滚语义唯一的会话上下文。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ensure_schema() -> None:
    """惰性应用一次幂等 DDL；并发下只执行一次。"""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        from app.db.schema import schema_statements

        engine = get_engine()
        with engine.begin() as connection:
            for statement in schema_statements():
                connection.exec_driver_sql(statement)
        _schema_ready = True


def reset_state() -> None:
    """测试辅助：丢弃 engine/session/建表状态，让下次调用重新构造。"""
    global _engine, _session_factory, _schema_ready
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
        _schema_ready = False


__all__ = [
    "ensure_schema",
    "get_engine",
    "get_session_factory",
    "reset_state",
    "session_scope",
]
