"""净值同步调度器的生命周期。

APScheduler 只在真正启动调度时导入。**装不上它也不该拖垮整个应用**——调度是增强能力，
业务 API 必须照常可用（与 ``init_agents`` 的降级策略一致）。

因此对外只有一个上下文管理器： ``nav_scheduler()``。它要么启动调度并登记每日作业，
要么把失败原因记进 ``status``，绝不向调用方抛异常。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass
class SchedulerStatus:
    """调度器状态。便于健康检查暴露"调度到底起没起"。"""

    status: str = "starting"
    reason: str = ""
    job_id: str = ""


def _build_scheduler() -> Any:
    """构造一个进程内调度器。APScheduler 缺失时抛 ``ImportError``。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.services.portfolio_advisor.nav_job import SHANGHAI

    return AsyncIOScheduler(timezone=SHANGHAI)


@contextmanager
def nav_scheduler(*, enabled: bool = True) -> Iterator[SchedulerStatus]:
    """启动净值同步调度；任何失败都降级为 ``status="degraded"``，不抛异常。

    ``enabled=False`` 时直接返回 ``disabled``，用于测试与不需要调度的部署。
    """
    status = SchedulerStatus()
    if not enabled:
        status.status = "disabled"
        status.reason = "已显式关闭"
        yield status
        return

    scheduler = None
    try:
        from app.services.portfolio_advisor.nav_job import (
            enabled_product_codes,
            register_nav_job,
        )
        from app.services.portfolio_advisor.nav import NavService
        from app.services.portfolio_advisor.nav_source import get_nav_source

        scheduler = _build_scheduler()
        job = register_nav_job(
            scheduler,
            service=NavService(_nav_store()),
            source=get_nav_source(),
            product_codes=enabled_product_codes(_product_library()),
        )
        scheduler.start()
        status.status = "running"
        status.job_id = getattr(job, "id", "portfolio_nav_sync")
        if get_nav_source() is None:
            # 作业会跑但只会记录 no_source：必须让这件事可见。
            status.reason = "未配置净值源，作业将记录 no_source"
            logger.warning("净值调度已启动，但未配置净值源：每日作业只会记录 no_source")
        else:
            logger.info("净值调度已启动：每个交易日 18:00 同步日频净值")
    except ImportError as exc:
        status.status = "degraded"
        status.reason = f"APScheduler 未安装: {exc}"
        logger.warning("净值调度未启动（%s）", status.reason)
    except Exception as exc:  # noqa: BLE001 - 调度失败不得影响业务 API
        status.status = "degraded"
        status.reason = f"{type(exc).__name__}: {exc}"
        logger.exception("净值调度启动失败")

    try:
        yield status
    finally:
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                logger.warning("净值调度关闭失败", exc_info=True)


def _nav_store() -> Any:
    from app.crud.portfolio_advisor_crud import get_nav_store

    return get_nav_store()


def _product_library() -> Any:
    from app.crud.product_crud import get_product_library

    return get_product_library()


__all__ = ["SchedulerStatus", "nav_scheduler"]
