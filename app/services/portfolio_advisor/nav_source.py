"""净值源的解析。

**当前没有可用的外部净值源**：Fuyao 的基金端点在仓库里只被命名、未被接入，
Tushare 在本仓只有新闻与公司事件（无 ``fund_nav``），当日快照表
``finance.product_performance`` 也不是时间序列。因此这里的默认实现**明确返回 ``None``**，
让每日作业如实报 ``no_source``，而不是假装成功。

这不是"占位实现"式的敷衍：``NavSource`` 是一个两方法的协议，接入真实源时只需写一个类，
并在 ``get_nav_source()`` 里返回它——作业、调度、覆盖规则、状态上报全都不用改。

候选接入方式（任一落地即可，均需实测）：
- Fuyao ``https://fuyao.aicubes.cn/mcp/fund``（未接入当前客户端）
- 由运维定期导入 CSV（``scripts/import_nav.py``），此时根本不需要外部源
"""

from __future__ import annotations

import logging
import os

from app.services.portfolio_advisor.nav_sync import NavSource

logger = logging.getLogger(__name__)

#: 显式指定净值源实现的环境变量。留空表示未配置。
#: 取值形如 ``module.path:ClassName``；类需实现 ``NavSource`` 协议并有无参构造函数。
NAV_SOURCE_ENV = "PORTFOLIO_NAV_SOURCE"

_cached: NavSource | None = None
_resolved = False


def get_nav_source() -> NavSource | None:
    """返回已配置的净值源；未配置时返回 ``None``。

    结果会缓存：解析只做一次，避免每个交易日重复导入与构造。
    """
    global _cached, _resolved
    if _resolved:
        return _cached

    _resolved = True
    spec = (os.getenv(NAV_SOURCE_ENV) or "").strip()
    if not spec:
        logger.info(
            "未配置 %s：日频净值需通过 scripts/import_nav.py 导入，每日同步作业将记录 no_source",
            NAV_SOURCE_ENV,
        )
        _cached = None
        return None

    try:
        module_path, _, class_name = spec.partition(":")
        if not module_path or not class_name:
            raise ValueError(f"格式应为 module.path:ClassName，实际为 {spec!r}")
        import importlib

        module = importlib.import_module(module_path)
        factory = getattr(module, class_name)
        instance = factory()
        logger.info("净值源已加载: %s", spec)
        from typing import cast

        _cached = cast(NavSource, instance)
    except Exception as exc:  # noqa: BLE001 - 配置错误只降级，不影响业务
        logger.warning("加载净值源失败（%s），按未配置处理: %s", spec, exc)
        _cached = None
    return _cached


def reset_nav_source_cache() -> None:
    """测试辅助：清掉缓存，让下一次调用重新解析环境变量。"""
    global _cached, _resolved
    _cached = None
    _resolved = False


__all__ = ["NAV_SOURCE_ENV", "get_nav_source", "reset_nav_source_cache"]
