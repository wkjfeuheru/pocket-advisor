"""应用日志配置。"""

from __future__ import annotations

import logging


def configure_logging(level: int = logging.INFO) -> None:
    """配置根日志。已有 handler 时只调整级别，避免重复输出。"""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


__all__ = ["configure_logging"]
