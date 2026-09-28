"""净值源假件：供 ``test_portfolio_nav_source.py`` 通过点分路径加载。

放在测试包里而不是生产代码里——它只是一个协议实现，用来验证"按配置加载源"这条链路。
"""

from __future__ import annotations

from datetime import date


class StubNavSource:
    """实现 ``NavSource`` 协议：固定返回 1.0。"""

    def __init__(self, nav: float = 1.0) -> None:
        self.nav = nav

    def latest_nav(self, product_code: str, *, as_of: date) -> tuple[date, float] | None:
        return (as_of, self.nav)
