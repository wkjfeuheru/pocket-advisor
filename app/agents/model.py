"""OpenAI-compatible 对话模型。意图分类由调用方显式指定 function-calling。"""

from __future__ import annotations

from app.core.settings_models import AdvisorSettings, load_advisor_settings


def build_chat_model(settings: AdvisorSettings | None = None):
    """构造聊天模型。缺少密钥时失败，且异常文本不包含密钥本身。"""
    resolved = settings or load_advisor_settings()
    if not resolved.api_key:
        raise RuntimeError("advisor model is not configured")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=resolved.model_name,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        temperature=0.2,
    )


__all__ = ["build_chat_model"]
