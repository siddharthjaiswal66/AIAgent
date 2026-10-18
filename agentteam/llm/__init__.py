"""LLM client factory."""

from __future__ import annotations

from ..config import Settings
from .base import LLMClient, LLMError


def create_llm(settings: Settings) -> LLMClient:
    provider = settings.provider.lower()
    if provider == "openai":
        from .providers import OpenAIClient

        return OpenAIClient(settings)
    if provider in {"azure", "azure_openai", "azureopenai"}:
        from .providers import AzureOpenAIClient

        return AzureOpenAIClient(settings)
    if provider in {"anthropic", "claude"}:
        from .providers import AnthropicClient

        return AnthropicClient(settings)
    if provider == "ollama":
        from .providers import OllamaClient

        return OllamaClient(settings)
    if provider == "mock":
        from .mock import MockLLM

        return MockLLM(settings)
    raise LLMError(f"Unknown LLM provider '{settings.provider}'. Use openai|azure|anthropic|ollama|mock")


__all__ = ["create_llm", "LLMClient", "LLMError"]
