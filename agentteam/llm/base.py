"""Provider-agnostic LLM client interface."""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..config import Settings


class LLMError(RuntimeError):
    """Raised when an LLM call fails permanently."""


class RetryableLLMError(LLMError):
    """Raised for transient failures (rate limits, 5xx, timeouts)."""


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_role: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "calls_by_role": dict(self.by_role),
        }


class LLMClient(ABC):
    """Base class. Subclasses implement `_complete`; this class adds
    concurrency limiting, retries with exponential backoff and usage tracking."""

    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.usage = Usage()
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))

    async def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        role: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None | str = "default",
    ) -> str:
        text, _, _ = await self.complete_with_usage(
            system, messages, role=role, model=model, max_tokens=max_tokens, temperature=temperature
        )
        return text

    async def complete_with_usage(
        self,
        system: str,
        messages: list[dict],
        *,
        role: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None | str = "default",
    ) -> tuple[str, int, int]:
        model = model or self.settings.resolved_model(role)
        max_tokens = max_tokens or self.settings.max_tokens
        temp = self.settings.temperature if temperature == "default" else temperature

        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                async with self._semaphore:
                    text, p_tokens, c_tokens = await self._complete(
                        system, messages, model=model, max_tokens=max_tokens, temperature=temp
                    )
                self.usage.calls += 1
                self.usage.prompt_tokens += p_tokens
                self.usage.completion_tokens += c_tokens
                if role:
                    self.usage.by_role[role] = self.usage.by_role.get(role, 0) + 1
                return text, p_tokens, c_tokens
            except RetryableLLMError as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    break
                delay = min(60.0, (2 ** attempt) * 2 + random.random())
                await asyncio.sleep(delay)
        raise LLMError(f"LLM call failed after retries: {last_error}")

    @abstractmethod
    async def _complete(
        self,
        system: str,
        messages: list[dict],
        *,
        model: str,
        max_tokens: int,
        temperature: float | None,
    ) -> tuple[str, int, int]:
        """Return (text, prompt_tokens, completion_tokens)."""

    async def aclose(self) -> None:  # pragma: no cover - optional override
        return None
