"""Runtime configuration loaded from environment variables / .env file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _float_or_none(name: str, default: float | None) -> float | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if value == "" or value.lower() == "none":
        return None
    return float(value)


DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "azure": "",  # Azure uses the deployment name
    "anthropic": "claude-sonnet-4-20250514",
    "ollama": "qwen2.5-coder:14b",
    "mock": "mock",
}


@dataclass
class Settings:
    # ---- LLM ----
    provider: str = "openai"  # openai | azure | anthropic | ollama | mock
    model: str = ""
    temperature: float | None = 0.2
    max_tokens: int = 8000
    max_concurrency: int = 4
    request_timeout: int = 600
    max_retries: int = 3
    role_models: dict[str, str] = field(default_factory=dict)

    # ---- Smart model routing ----
    routing: str = "auto"  # auto = pick small/medium/large model per task ; fixed = one model for everything
    tier_models: dict[str, str] = field(default_factory=dict)  # tier -> "model" or "provider:model"
    token_budget: int = 0  # total LLM tokens for a run; 0 = unlimited
    prices: dict[str, list[float]] = field(default_factory=dict)  # model -> [usd per 1M input, usd per 1M output]

    # ---- Technology decisions ----
    tech_preferences: str = ""  # mandatory customer technology constraints; empty = architect decides

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    azure_api_key: str = ""
    azure_endpoint: str = ""
    azure_api_version: str = "2024-10-21"
    azure_deployment: str = ""

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    ollama_base_url: str = "http://localhost:11434"
    ollama_num_ctx: int = 32768

    # ---- Delivery process ----
    output_dir: str = "output"
    allow_execution: bool = False  # run build/test commands produced by the team
    allow_deploy: bool = False  # run deployment commands (e.g. docker compose up)
    command_timeout: int = 900
    max_qa_iterations: int = 3
    max_questions_per_task: int = 3
    max_context_chars: int = 60000
    interactive: bool = False  # analyst may ask the human clarifying questions

    @classmethod
    def from_env(cls) -> "Settings":
        role_models = {
            key[len("LLM_MODEL_"):].lower(): value
            for key, value in os.environ.items()
            if key.startswith("LLM_MODEL_") and value
        }
        tier_models = {
            tier: os.getenv(f"LLM_TIER_{tier.upper()}", "").strip()
            for tier in ("small", "medium", "large")
            if os.getenv(f"LLM_TIER_{tier.upper()}", "").strip()
        }
        try:
            prices = json.loads(os.getenv("LLM_PRICES", "") or "{}")
        except json.JSONDecodeError:
            prices = {}
        return cls(
            provider=os.getenv("LLM_PROVIDER", "openai").lower(),
            model=os.getenv("LLM_MODEL", ""),
            routing=os.getenv("LLM_ROUTING", "auto").strip().lower() or "auto",
            tier_models=tier_models,
            token_budget=_int("TOKEN_BUDGET", 0),
            prices=prices,
            tech_preferences=os.getenv("TECH_PREFERENCES", ""),
            temperature=_float_or_none("LLM_TEMPERATURE", 0.2),
            max_tokens=_int("LLM_MAX_TOKENS", 8000),
            max_concurrency=_int("LLM_MAX_CONCURRENCY", 4),
            request_timeout=_int("LLM_TIMEOUT", 600),
            max_retries=_int("LLM_MAX_RETRIES", 3),
            role_models=role_models,
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            azure_api_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            azure_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_num_ctx=_int("OLLAMA_NUM_CTX", 32768),
            output_dir=os.getenv("OUTPUT_DIR", "output"),
            allow_execution=_bool("ALLOW_EXECUTION", False),
            allow_deploy=_bool("ALLOW_DEPLOY", False),
            command_timeout=_int("COMMAND_TIMEOUT", 900),
            max_qa_iterations=_int("MAX_QA_ITERATIONS", 3),
            max_questions_per_task=_int("MAX_QUESTIONS_PER_TASK", 3),
            max_context_chars=_int("MAX_CONTEXT_CHARS", 60000),
            interactive=_bool("INTERACTIVE", False),
        )

    def with_overrides(self, **overrides) -> "Settings":
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)

    def resolved_model(self, role: str | None = None) -> str:
        if role and role.lower() in self.role_models:
            return self.role_models[role.lower()]
        if self.model:
            return self.model
        if self.provider == "azure":
            return self.azure_deployment
        return DEFAULT_MODELS.get(self.provider, "")
