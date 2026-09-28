"""Agent 路由已挂到应用，并且只服务普通用户。"""

from __future__ import annotations


def test_agent_router_is_mounted() -> None:
    from app.main import app

    mounted: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            mounted.add(path)
        original = getattr(route, "original_router", None)
        for nested in getattr(original, "routes", []) if original is not None else []:
            nested_path = getattr(nested, "path", None)
            if nested_path:
                mounted.add(nested_path)
    assert "/api/agents/health" in mounted
    assert "/api/agents/chat/stream" in mounted
    assert "/api/agents/runs/{request_id}/resume/stream" in mounted
    assert "/api/agents/runs/{request_id}/clarify/stream" in mounted
    assert "/api/agents/runs/{request_id}/allocation/stream" in mounted
    assert "/agents/run" not in mounted
