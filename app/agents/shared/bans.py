"""正文禁令引擎。

领域只提供词表/正则片段，查找与清洗的机制在这里统一实现。片段按原样用 ``|`` 连接
（不做转义），因此既可以是词（``"买入"``）也可以是模式（``r"仓位\\s*\\d+\\s*%"``）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: 空词表用一条永不匹配的模式表示，避免调用方到处判空。
_NEVER = r"(?!)"


def build_pattern(fragments: Iterable[str]) -> re.Pattern[str]:
    """把片段编译成一条交替正则。片段被当作正则而不是字面量。"""
    items = [str(fragment) for fragment in fragments if str(fragment)]
    if not items:
        return re.compile(_NEVER)
    return re.compile("|".join(items))


def find_forbidden(text: str, pattern: re.Pattern[str]) -> list[str]:
    """返回命中的片段（保序去重），供日志与测试定位。"""
    hits: list[str] = []
    for match in pattern.finditer(text or ""):
        word = match.group(0)
        if word and word not in hits:
            hits.append(word)
    return hits


def strip_forbidden(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub("", text or "")


__all__ = ["build_pattern", "find_forbidden", "strip_forbidden"]
