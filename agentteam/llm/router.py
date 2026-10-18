"""Smart model routing for token/cost efficiency.

Every LLM call is routed to one of three tiers - small / medium / large - based on:
  1. The task type (classification vs. code generation vs. architecture).
  2. Complexity signals produced by the team itself, at no extra LLM cost:
       - project complexity from the Business Analyst (small | medium | large)
       - per-task complexity from the Project Manager's plan (low | medium | high)
       - defect severity from QA / security / architecture findings
  3. Feedback: escalate to a stronger tier when a model produces an unusable reply,
     when a task fails, or when the same defects survive several fix rounds.
  4. Budget pressure: as TOKEN_BUDGET is consumed, calls are capped to cheaper tiers.
  5. Resilience: if a model is unavailable, fall back to another tier.

Tiers can mix providers, e.g. small=ollama:qwen2.5-coder:7b (free, local) and
large=anthropic:claude-opus-4-20250514.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from .base import LLMClient, LLMError

TIERS = ["small", "medium", "large"]
PROVIDERS = {"openai", "azure", "anthropic", "ollama", "mock"}

DEFAULT_TIER_MODELS = {
    "openai": {"small": "gpt-4o-mini", "medium": "gpt-4.1-mini", "large": "gpt-4.1"},
    "anthropic": {"small": "claude-3-5-haiku-latest", "medium": "claude-sonnet-4-20250514", "large": "claude-opus-4-20250514"},
    "ollama": {"small": "qwen2.5-coder:7b", "medium": "qwen2.5-coder:14b", "large": "qwen2.5-coder:32b"},
    "mock": {"small": "mock-small", "medium": "mock-medium", "large": "mock-large"},
}

# Approximate list prices in USD per 1M tokens [input, output]. Override/extend with LLM_PRICES.
DEFAULT_PRICES = {
    "gpt-4.1-nano": [0.10, 0.40],
    "gpt-4.1-mini": [0.40, 1.60],
    "gpt-4.1": [2.00, 8.00],
    "gpt-4o-mini": [0.15, 0.60],
    "gpt-4o": [2.50, 10.00],
    "o3": [2.00, 8.00],
    "o4-mini": [1.10, 4.40],
    "claude-3-5-haiku": [0.80, 4.00],
    "claude-haiku": [1.00, 5.00],
    "claude-sonnet": [3.00, 15.00],
    "claude-opus": [15.00, 75.00],
    "mock-small": [0.15, 0.60],
    "mock-medium": [0.40, 1.60],
    "mock-large": [2.00, 8.00],
}

COMPLEXITY_DELTA = {"trivial": -1, "low": -1, "small": -1, "simple": -1, "medium": 0, "moderate": 0,
                    "high": 1, "large": 1, "complex": 1, "very_high": 1}


@dataclass(frozen=True)
class TaskProfile:
    tier: str
    min_tier: str = "small"
    max_tier: str = "large"
    adaptive: bool = True  # whether complexity signals may move the tier
    max_tokens: int | None = None
    why: str = ""


TASK_PROFILES: dict[str, TaskProfile] = {
    "analyze_requirements": TaskProfile("large", "large", adaptive=False, why="deep reasoning over ambiguous business input"),
    "refine_requirements": TaskProfile("large", "large", adaptive=False, why="deep reasoning over ambiguous business input"),
    "select_team": TaskProfile("small", max_tier="medium", adaptive=False, max_tokens=3000, why="short pick from a fixed role catalogue"),
    "design_architecture": TaskProfile("large", "large", adaptive=False, why="highest-leverage decisions; mistakes propagate to every task"),
    "threat_model": TaskProfile("medium", "medium", why="structured STRIDE analysis"),
    "plan_project": TaskProfile("large", "medium", why="dependency-aware work breakdown"),
    "implement_task": TaskProfile("medium", why="code generation scaled by task complexity"),
    "fix": TaskProfile("medium", why="targeted fixes scaled by severity and retries"),
    "architecture_review": TaskProfile("medium", "medium", why="cross-component consistency check"),
    "security_review": TaskProfile("medium", "medium", why="OWASP review of the code base"),
    "write_tests": TaskProfile("medium", why="test generation from acceptance criteria"),
    "evaluate_quality": TaskProfile("medium", "medium", why="root-cause analysis of test output"),
    "build_deployment": TaskProfile("medium", why="container / CI / IaC generation"),
    "write_docs": TaskProfile("small", max_tier="medium", why="summarising existing code and artifacts"),
    "answer_question": TaskProfile("small", max_tier="medium", max_tokens=2000, why="short factual answer from own artifacts"),
    "delivery_report": TaskProfile("small", max_tier="medium", adaptive=False, max_tokens=4000, why="summary of known facts"),
}
DEFAULT_PROFILE = TaskProfile("medium", why="default")

# Some roles' answers carry more weight (e.g. architecture contract questions).
ROLE_FLOORS = {("answer_question", "solution_architect"): "medium"}


def parse_spec(spec: str, default_provider: str) -> tuple[str, str]:
    """'model' -> (default_provider, model); 'provider:model' -> (provider, model)."""
    head, sep, tail = spec.partition(":")
    if sep and head.strip().lower() in PROVIDERS and tail.strip():
        return head.strip().lower(), tail.strip()
    return default_provider, spec.strip()


@dataclass
class Route:
    tier: str
    provider: str
    model: str
    reason: str
    max_tokens: int


@dataclass
class _Bucket:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0

    def as_dict(self) -> dict:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "est_cost_usd": round(self.cost, 4)}


@dataclass
class RouterUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    baseline_cost: float = 0.0  # what the same tokens would cost on the large tier
    cost_known: bool = True
    by_role: dict[str, int] = field(default_factory=dict)
    by_tier: dict[str, _Bucket] = field(default_factory=dict)
    by_model: dict[str, _Bucket] = field(default_factory=dict)
    escalations: int = 0
    fallbacks: int = 0
    budget_downgrades: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        savings = self.baseline_cost - self.cost
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "est_cost_usd": round(self.cost, 4) if self.cost_known else None,
            "est_cost_if_all_large_usd": round(self.baseline_cost, 4) if self.cost_known else None,
            "est_savings_usd": round(savings, 4) if self.cost_known else None,
            "est_savings_pct": round(100 * savings / self.baseline_cost, 1) if self.cost_known and self.baseline_cost else None,
            "escalations": self.escalations,
            "fallbacks": self.fallbacks,
            "budget_downgrades": self.budget_downgrades,
            "calls_by_role": dict(self.by_role),
            "by_tier": {k: v.as_dict() for k, v in self.by_tier.items()},
            "by_model": {k: v.as_dict() for k, v in self.by_model.items()},
        }


class ModelRouter:
    """Drop-in replacement for an LLMClient that chooses the model per call."""

    def __init__(self, settings: Settings, events=None):
        self.settings = settings
        self.events = events
        self.usage = RouterUsage()
        self.project_complexity: str | None = None
        self._clients: dict[str, LLMClient] = {}
        self.tiers: dict[str, tuple[str, str]] = self._resolve_tiers()

    # ------------------------------------------------------------------ configuration
    def _resolve_tiers(self) -> dict[str, tuple[str, str]]:
        s = self.settings
        defaults = DEFAULT_TIER_MODELS.get(s.provider, {})
        tiers = {}
        for tier in TIERS:
            spec = s.tier_models.get(tier)
            if spec:
                tiers[tier] = parse_spec(spec, s.provider)
            else:
                # An explicit LLM_MODEL means "use this model" for every unconfigured tier.
                model = s.model or defaults.get(tier) or s.resolved_model()
                tiers[tier] = (s.provider, model)
        return tiers

    def describe(self) -> dict:
        if self.settings.routing == "fixed":
            return {"routing": "fixed", "model": f"{self.settings.provider}:{self.settings.resolved_model()}"}
        return {"routing": "auto", **{t: f"{p}:{m}" for t, (p, m) in self.tiers.items()},
                "token_budget": self.settings.token_budget or "unlimited"}

    def set_project_complexity(self, complexity: str | None) -> None:
        self.project_complexity = (complexity or "").strip().lower() or None
        self._emit("log", agent="model_router",
                   message=f"Project complexity '{self.project_complexity}' -> adaptive tasks shift by "
                           f"{COMPLEXITY_DELTA.get(self.project_complexity or '', 0):+d} tier(s)")

    def _client(self, provider: str) -> LLMClient:
        if provider not in self._clients:
            from . import create_llm

            self._clients[provider] = create_llm(self.settings.with_overrides(provider=provider))
        return self._clients[provider]

    def price(self, provider: str, model: str) -> list[float] | None:
        if provider == "ollama":
            return [0.0, 0.0]
        table = {**DEFAULT_PRICES, **self.settings.prices}
        if model in table:
            return table[model]
        matches = [k for k in table if model.startswith(k)]
        return table[max(matches, key=len)] if matches else None

    def _emit(self, type_: str, **data) -> None:
        if self.events:
            self.events.emit(type_, **data)

    # ------------------------------------------------------------------ routing decision
    def choose(self, role: str | None, task: str | None, complexity: str | None = None, escalation: int = 0) -> Route:
        s = self.settings
        task = task or "default"
        profile = TASK_PROFILES.get(task) or (TASK_PROFILES["fix"] if task.startswith("fix_") else DEFAULT_PROFILE)
        max_tokens = min(profile.max_tokens or s.max_tokens, s.max_tokens)

        if role and role.lower() in s.role_models:
            provider, model = parse_spec(s.role_models[role.lower()], s.provider)
            return Route("pinned", provider, model, f"LLM_MODEL_{role.upper()} override", max_tokens)
        if s.routing == "fixed":
            return Route("fixed", s.provider, s.resolved_model(role), "fixed routing", max_tokens)

        level = TIERS.index(profile.tier)
        reasons = [f"{task}: {profile.why or 'baseline'} -> {profile.tier}"]
        lo, hi = TIERS.index(profile.min_tier), TIERS.index(profile.max_tier)
        floor = ROLE_FLOORS.get((task, role or ""))
        if floor:
            lo = max(lo, TIERS.index(floor))

        if profile.adaptive:
            signal = (complexity or "").strip().lower()
            source = "task complexity"
            if signal not in COMPLEXITY_DELTA:
                signal, source = (self.project_complexity or ""), "project complexity"
            delta = COMPLEXITY_DELTA.get(signal, 0)
            if delta:
                level += delta
                reasons.append(f"{source} '{signal}' {delta:+d}")
        level = max(lo, min(hi, level))
        if floor and TIERS.index(floor) > TIERS.index(profile.tier):
            reasons.append(f"{role} answers need at least {floor}")

        if escalation > 0:
            new_level = min(len(TIERS) - 1, level + escalation)
            if new_level > level:
                self.usage.escalations += 1
                reasons.append(f"escalated +{new_level - level} (retry/failed attempt)")
            level = new_level

        if s.token_budget > 0:
            ratio = self.usage.total_tokens / s.token_budget
            cap = 0 if ratio >= 0.95 else 1 if ratio >= 0.80 else 2
            if level > cap:
                self.usage.budget_downgrades += 1
                reasons.append(f"budget {ratio:.0%} used -> capped at {TIERS[cap]}")
                level = cap

        tier = TIERS[level]
        provider, model = self.tiers[tier]
        return Route(tier, provider, model, "; ".join(reasons), max_tokens)

    # ------------------------------------------------------------------ LLMClient-compatible API
    async def complete(self, system: str, messages: list[dict], *, role: str | None = None, task: str | None = None,
                       complexity: str | None = None, escalation: int = 0, max_tokens: int | None = None,
                       temperature="default") -> str:
        route = self.choose(role, task, complexity, escalation)
        candidates = [route]
        if route.tier in TIERS:  # fallbacks: stronger tiers first, then cheaper ones
            idx = TIERS.index(route.tier)
            for t in [*TIERS[idx + 1:], *reversed(TIERS[:idx])]:
                p, m = self.tiers[t]
                if (p, m) not in {(c.provider, c.model) for c in candidates}:
                    candidates.append(Route(t, p, m, f"fallback from {route.model}", route.max_tokens))

        last_error: Exception | None = None
        for i, r in enumerate(candidates):
            try:
                text, p_tok, c_tok = await self._client(r.provider).complete_with_usage(
                    system, messages, role=role, model=r.model,
                    max_tokens=max_tokens or r.max_tokens, temperature=temperature,
                )
            except LLMError as exc:
                last_error = exc
                if i + 1 < len(candidates):
                    self.usage.fallbacks += 1
                    self._emit("error", agent=role, message=f"Model {r.provider}:{r.model} failed ({exc}); "
                                                            f"falling back to {candidates[i + 1].model}")
                continue
            self._record(r, role, task, p_tok, c_tok)
            return text
        raise LLMError(f"All model tiers failed for task '{task}': {last_error}")

    def _record(self, r: Route, role: str | None, task: str | None, p_tok: int, c_tok: int) -> None:
        u = self.usage
        u.calls += 1
        u.prompt_tokens += p_tok
        u.completion_tokens += c_tok
        if role:
            u.by_role[role] = u.by_role.get(role, 0) + 1
        price = self.price(r.provider, r.model)
        cost = (p_tok * price[0] + c_tok * price[1]) / 1_000_000 if price else 0.0
        large = self.tiers["large"]
        large_price = self.price(*large)
        if price is None or large_price is None:
            u.cost_known = False
        u.cost += cost
        u.baseline_cost += (p_tok * large_price[0] + c_tok * large_price[1]) / 1_000_000 if large_price else 0.0
        for bucket in (u.by_tier.setdefault(r.tier, _Bucket()), u.by_model.setdefault(f"{r.provider}:{r.model}", _Bucket())):
            bucket.calls += 1
            bucket.prompt_tokens += p_tok
            bucket.completion_tokens += c_tok
            bucket.cost += cost
        self._emit("llm_call", agent=role, task=task, tier=r.tier, provider=r.provider, model=r.model, reason=r.reason,
                   prompt_tokens=p_tok, completion_tokens=c_tok, cost=round(cost, 5),
                   total_tokens=u.total_tokens, total_cost=round(u.cost, 4))

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
