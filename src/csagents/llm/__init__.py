from .. import settings
from .base import LLM, AccountReview, LLMError, Reason, ToolCall


def get_llm() -> LLM:
    if settings.load().llm_provider == "claude":
        from .claude import ClaudeLLM

        return ClaudeLLM()
    from .offline import OfflineLLM

    return OfflineLLM()


__all__ = ["LLM", "AccountReview", "LLMError", "Reason", "ToolCall", "get_llm"]
