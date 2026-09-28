"""Tushare 新闻/事件适配器：无正文、无密钥、可降级。"""

from __future__ import annotations

import asyncio

from app.mcp.tushare_client import TushareClient, load_tushare_client


class _FakePro:
    def __init__(self, *, rows=None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[dict] = []

    def news(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.rows

    def anns(self, **kwargs):
        return self.news(**kwargs)


def test_missing_token_does_not_raise():
    client = TushareClient(token="")
    payload = asyncio.run(client.list_events("600519.SH"))
    assert payload["ok"] is False
    assert payload["code"] == "tushare_unconfigured"
    assert payload["data"] is None


def test_news_body_is_stripped_and_events_are_tagged():
    pro = _FakePro(rows=[{
        "title": "贵州茅台发布业绩预告",
        "content": "完整新闻正文不应出现",
        "source": "上证报",
        "url": "https://example.com/a",
        "datetime": "2026-09-20 09:00:00",
        "channels": "600519.SH",
    }])
    client = TushareClient(token="secret-token", pro=pro)
    payload = asyncio.run(client.list_events("600519.SH"))
    assert payload["ok"] is True
    item = payload["data"][0]
    assert item["title"] == "贵州茅台发布业绩预告"
    assert "content" not in item
    assert "完整新闻正文" not in str(payload)
    assert item["source"] == "上证报"
    assert item["url"] == "https://example.com/a"
    assert item["published_at"]
    assert "600519.SH" in item["related_symbols"]
    assert "earnings_preview" in item["event_tags"]
    assert 0 <= item["confidence"] <= 1


def test_events_keep_symbol_and_thirty_day_window_only():
    import threading
    from datetime import date

    class _WindowPro(_FakePro):
        def __init__(self):
            super().__init__(rows=[
                {"title": "贵州茅台业绩预告", "datetime": "2026-09-20 09:00:00", "channels": "600519.SH", "source": "上证报"},
                {"title": "其他公司解禁", "datetime": "2026-09-20 09:00:00", "channels": "000001.SZ", "source": "上证报"},
                {"title": "贵州茅台旧闻", "datetime": "2020-01-01 09:00:00", "channels": "600519.SH", "source": "上证报"},
            ])
            self.threads: list[int] = []

        def news(self, **kwargs):
            self.threads.append(threading.get_ident())
            self.calls.append(kwargs)
            return super().news(**kwargs)

        def forecast(self, **kwargs):
            self.calls.append({"api": "forecast", **kwargs})
            return [{"ann_title": "茅台业绩预告", "ts_code": "600519.SH", "ann_date": "20260920", "src": "上证报"}]

        def share_float(self, **kwargs):
            self.calls.append({"api": "share_float", **kwargs})
            return []

        def pledge_stat(self, **kwargs):
            self.calls.append({"api": "pledge_stat", **kwargs})
            return []

        def stk_holdertrade(self, **kwargs):
            self.calls.append({"api": "stk_holdertrade", **kwargs})
            return []

    pro = _WindowPro()
    client = TushareClient(token="secret-token", pro=pro)

    async def run():
        return threading.get_ident(), await client.list_events("600519.SH", as_of=date(2026, 9, 27))

    loop_thread, payload = asyncio.run(run())
    titles = [item["title"] for item in payload["data"]]
    assert "其他公司解禁" not in titles
    assert "贵州茅台旧闻" not in titles
    assert any("业绩预告" in title or "茅台业绩预告" in title for title in titles)
    assert pro.calls[0].get("start_date")
    assert pro.threads and pro.threads[0] != loop_thread


def test_permission_rate_limit_and_timeout_are_coded(caplog):
    cases = [
        (PermissionError("token=abc no privilege"), "tushare_forbidden"),
        (RuntimeError("429 rate limited token=abc"), "tushare_rate_limited"),
        (TimeoutError("token=abc timed out"), "tushare_timeout"),
    ]
    for error, code in cases:
        client = TushareClient(token="secret-token", pro=_FakePro(error=error))
        with caplog.at_level("WARNING"):
            payload = asyncio.run(client.list_events("600519.SH"))
        assert payload["ok"] is False
        assert payload["code"] == code
        assert "abc" not in payload["warning"]
        assert "secret-token" not in caplog.text
        assert "abc" not in caplog.text


def test_settings_mask_tushare_token(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "ts-secret")
    from app.core.settings_models import load_advisor_settings, settings_dump

    settings = load_advisor_settings()
    assert settings.tushare_token == "ts-secret"
    dumped = settings_dump(settings)
    assert dumped["tushare_token"] == "***"
    client, warning = load_tushare_client("")
    assert warning == "tushare_unconfigured"
    assert client.token == ""
