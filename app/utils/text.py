"""文本处理。后续 Agent 提示词清洗可放在这里。"""

from __future__ import annotations


def normalize_whitespace(text: str) -> str:
    return " ".join((text or "").split())


__all__ = ["normalize_whitespace"]
