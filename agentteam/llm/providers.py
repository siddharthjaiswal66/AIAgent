"""Concrete LLM providers implemented over plain HTTPS (no vendor SDKs needed).

Supported:
  * openai     - OpenAI or any OpenAI-compatible endpoint (Groq, LM Studio, vLLM, OpenRouter...)
  * azure      - Azure OpenAI
  * anthropic  - Anthropic Claude
  * ollama     - Local models through Ollama (free, offline)
"""

from __future__ import annotations

import httpx

from ..config import Settings
from .base import LLMClient, LLMError, RetryableLLMError

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class _HttpClient(LLMClient):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=30))

    async def _post(self, url: str, headers: dict, payload: dict) -> tuple[int, dict | str]:
        try:
            resp = await self._http.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableLLMError(f"{self.name} transport error: {exc}") from exc
        if resp.status_code in _RETRYABLE_STATUS:
            raise RetryableLLMError(f"{self.name} HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, body

    async def aclose(self) -> None:
        await self._http.aclose()


class OpenAIClient(_HttpClient):
    """OpenAI Chat Completions API (also works with OpenAI-compatible servers)."""

    name = "openai"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        # Newer models use max_completion_tokens and may reject temperature; adapt automatically per model.
        self._token_param: dict[str, str] = {}
        self._no_temperature: set[str] = set()

    def _endpoint(self, model: str) -> tuple[str, dict]:
        if not self.settings.openai_api_key and "api.openai.com" in self.settings.openai_base_url:
            raise LLMError("OPENAI_API_KEY is not set")
        headers = {"Content-Type": "application/json"}
        if self.settings.openai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.openai_api_key}"
        return self.settings.openai_base_url.rstrip("/") + "/chat/completions", headers

    async def _complete(self, system, messages, *, model, max_tokens, temperature):
        url, headers = self._endpoint(model)
        swapped_token_param = False
        for _ in range(3):
            token_param = self._token_param.get(model, "max_completion_tokens")
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system}, *messages],
                token_param: max_tokens,
            }
            if temperature is not None and model not in self._no_temperature:
                payload["temperature"] = temperature
            status, body = await self._post(url, headers, payload)
            if status == 400:
                text = str(body).lower()
                rejected = any(w in text for w in ("unsupported", "unrecognized", "not supported", "extra inputs"))
                if rejected and "max_" in text and "tokens" in text and not swapped_token_param:
                    self._token_param[model] = (
                        "max_tokens" if token_param == "max_completion_tokens" else "max_completion_tokens"
                    )
                    swapped_token_param = True
                    continue
                if "temperature" in text and model not in self._no_temperature:
                    self._no_temperature.add(model)
                    continue
            if status >= 400 or not isinstance(body, dict):
                raise LLMError(f"{self.name} HTTP {status}: {str(body)[:1000]}")
            choice = body["choices"][0]
            text = choice["message"].get("content") or ""
            usage = body.get("usage") or {}
            if choice.get("finish_reason") == "length":
                text += "\n<!-- WARNING: response truncated by token limit -->"
            return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        raise LLMError(f"{self.name}: could not negotiate request parameters")


class AzureOpenAIClient(OpenAIClient):
    name = "azure"

    def _endpoint(self, model: str) -> tuple[str, dict]:
        s = self.settings
        if not (s.azure_api_key and s.azure_endpoint):
            raise LLMError("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set")
        deployment = model or s.azure_deployment
        if not deployment:
            raise LLMError("AZURE_OPENAI_DEPLOYMENT (or LLM_MODEL) must be set")
        url = (
            f"{s.azure_endpoint.rstrip('/')}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={s.azure_api_version}"
        )
        return url, {"Content-Type": "application/json", "api-key": s.azure_api_key}


class AnthropicClient(_HttpClient):
    name = "anthropic"

    async def _complete(self, system, messages, *, model, max_tokens, temperature):
        if not self.settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        url = self.settings.anthropic_base_url.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        status, body = await self._post(url, headers, payload)
        if status >= 400 or not isinstance(body, dict):
            raise LLMError(f"anthropic HTTP {status}: {str(body)[:1000]}")
        text = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
        if body.get("stop_reason") == "max_tokens":
            text += "\n<!-- WARNING: response truncated by token limit -->"
        usage = body.get("usage") or {}
        return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


class OllamaClient(_HttpClient):
    name = "ollama"

    async def _complete(self, system, messages, *, model, max_tokens, temperature):
        url = self.settings.ollama_base_url.rstrip("/") + "/api/chat"
        options = {"num_predict": max_tokens, "num_ctx": self.settings.ollama_num_ctx}
        if temperature is not None:
            options["temperature"] = temperature
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "options": options,
        }
        status, body = await self._post(url, {"Content-Type": "application/json"}, payload)
        if status >= 400 or not isinstance(body, dict):
            raise LLMError(f"ollama HTTP {status}: {str(body)[:1000]}")
        text = (body.get("message") or {}).get("content", "")
        return text, body.get("prompt_eval_count", 0), body.get("eval_count", 0)
