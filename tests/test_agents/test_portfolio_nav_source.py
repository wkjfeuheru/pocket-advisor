"""净值源解析与调度器降级。

两条关键约定：

- **没有净值源时如实返回 ``None``**，让每日作业报 ``no_source``——静默"成功但没写"
  会让定时任务看起来在跑，而净值永远停在旧值。
- **调度起不来不能拖垮应用**：APScheduler 未安装、商品池读不到、净值源配置写错，
  都只降级为 ``degraded`` 并记原因，不向调用方抛异常。
"""

from __future__ import annotations

import pytest

from app.services.portfolio_advisor.nav_source import (
    NAV_SOURCE_ENV,
    get_nav_source,
    reset_nav_source_cache,
)
from app.services.portfolio_advisor.scheduler import nav_scheduler


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_nav_source_cache()
    yield
    reset_nav_source_cache()


def test_no_source_configured_returns_none(monkeypatch):
    monkeypatch.delenv(NAV_SOURCE_ENV, raising=False)

    assert get_nav_source() is None


def test_a_configured_source_is_loaded_by_dotted_path(monkeypatch):
    monkeypatch.setenv(NAV_SOURCE_ENV, "tests.test_agents.nav_source_stub:StubNavSource")

    source = get_nav_source()

    assert source is not None
    assert hasattr(source, "latest_nav")


def test_a_broken_source_spec_degrades_to_none_instead_of_raising(monkeypatch):
    for spec in ("no-colon-here", "nonexistent.module:Thing", "tests.test_agents.nav_source_stub:Missing"):
        reset_nav_source_cache()
        monkeypatch.setenv(NAV_SOURCE_ENV, spec)

        assert get_nav_source() is None, f"{spec} 应当降级为 None"


def test_the_resolved_source_is_cached(monkeypatch):
    monkeypatch.setenv(NAV_SOURCE_ENV, "tests.test_agents.nav_source_stub:StubNavSource")

    first = get_nav_source()
    second = get_nav_source()

    assert first is second


def test_the_scheduler_can_be_disabled_explicitly():
    with nav_scheduler(enabled=False) as status:
        assert status.status == "disabled"
        assert status.reason


def test_the_scheduler_degrades_gracefully_when_anything_fails(monkeypatch):
    """无论启动过程哪里出错，都只降级，绝不抛异常。"""
    from app.services.portfolio_advisor import scheduler as scheduler_module

    def _boom():
        raise RuntimeError("模拟启动失败")

    monkeypatch.setattr(scheduler_module, "_build_scheduler", _boom)

    with nav_scheduler() as status:
        assert status.status == "degraded"
        assert "模拟启动失败" in status.reason


def test_the_scheduler_reports_degraded_when_apscheduler_is_missing(monkeypatch):
    """装不上 APScheduler 时必须是可解释的 degraded，而不是 500。"""
    import builtins

    from app.services.portfolio_advisor import scheduler as scheduler_module

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name.startswith("apscheduler"):
            raise ImportError("No module named 'apscheduler'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with nav_scheduler() as status:
        assert status.status == "degraded"
        assert "APScheduler" in status.reason
