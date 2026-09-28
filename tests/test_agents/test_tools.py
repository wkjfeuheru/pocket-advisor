"""已落地的投顾模型必须注册；工具注册表保持只读。"""

from __future__ import annotations


def test_tool_registry_is_readonly() -> None:
    from app.agents.tools import ALL_TOOLS
    from app.agents.tools.guard import is_forbidden_tool

    assert ALL_TOOLS
    assert all(not is_forbidden_tool(tool.name) for tool in ALL_TOOLS)


def test_agent_session_model_is_registered() -> None:
    from app.db.base import Base, load_all_models

    load_all_models()
    names = {table.name for table in Base.metadata.tables.values()}
    assert "agent_sessions" in names
    assert "agent_messages" in names
    assert "agent_turns" in names
    assert "advisor_profiles" in names
