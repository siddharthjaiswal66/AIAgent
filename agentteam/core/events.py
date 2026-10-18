"""Event stream used for observability (console, log file and web dashboard)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

Subscriber = Callable[[dict], None]


class EventBus:
    """Synchronous fan-out of run events to subscribers. Events are also kept
    in memory (for late-joining dashboard clients) and appended to a JSONL log."""

    def __init__(self, history_limit: int = 5000):
        self._subscribers: list[Subscriber] = []
        self.history: list[dict] = []
        self._history_limit = history_limit
        self._log_path: Path | None = None
        self._seq = 0

    def set_log_file(self, path: Path | None) -> None:
        self._log_path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def reset(self) -> None:
        self.history.clear()
        self._seq = 0

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def emit(self, type_: str, agent: str | None = None, **data: Any) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "ts": time.time(), "type": type_, "agent": agent, **data}
        self.history.append(event)
        if len(self.history) > self._history_limit:
            del self.history[: len(self.history) - self._history_limit]
        if self._log_path:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:  # never let an observer break the run
                pass
        return event


class RunState:
    """Materialised view of the run, built from events (used by the dashboard)."""

    def __init__(self, events: EventBus):
        self.reset()
        events.subscribe(self._on_event)

    def reset(self) -> None:
        self.status = "idle"
        self.requirement = ""
        self.project_dir = ""
        self.phase = None
        self.phases: list[dict] = []
        self.agents: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.artifacts: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        self.errors: list[str] = []
        self.usage: dict = {}
        self.routing: dict = {}
        self.tech_preferences = ""
        self.llm = {"calls": 0, "tokens": 0, "cost": 0.0, "by_tier": {}, "by_model": {}, "recent": []}

    def _on_event(self, e: dict) -> None:
        t = e["type"]
        if t == "run_started":
            self.reset()
            self.status = "running"
            self.requirement = e.get("requirement", "")
            self.project_dir = e.get("project_dir", "")
            self.routing = e.get("routing") or {}
            self.tech_preferences = e.get("tech_preferences") or ""
        elif t == "llm_call":
            tokens = (e.get("prompt_tokens") or 0) + (e.get("completion_tokens") or 0)
            self.llm["calls"] += 1
            self.llm["tokens"] += tokens
            self.llm["cost"] = e.get("total_cost", self.llm["cost"])
            for key, name in (("by_tier", e.get("tier")), ("by_model", e.get("model"))):
                bucket = self.llm[key].setdefault(name, {"calls": 0, "tokens": 0})
                bucket["calls"] += 1
                bucket["tokens"] += tokens
            self.llm["recent"] = (self.llm["recent"] + [{k: e.get(k) for k in (
                "ts", "agent", "task", "tier", "model", "reason", "prompt_tokens", "completion_tokens")}])[-60:]
        elif t == "phase_started":
            self.phase = e["phase"]
            self.phases.append({"name": e["phase"], "title": e.get("title"), "status": "running"})
        elif t == "phase_completed":
            for p in self.phases:
                if p["name"] == e["phase"]:
                    p["status"] = e.get("status", "done")
        elif t == "agent_joined":
            self.agents[e["agent"]] = {
                "role": e["agent"],
                "title": e.get("title"),
                "justification": e.get("justification", ""),
                "status": "idle",
                "activity": "",
                "owned_paths": e.get("owned_paths", []),
            }
        elif t == "agent_status" and e["agent"] in self.agents:
            self.agents[e["agent"]]["status"] = e.get("status")
            self.agents[e["agent"]]["activity"] = e.get("activity", "")
        elif t == "ownership_assigned":
            for role, paths in e.get("ownership", {}).items():
                if role in self.agents:
                    self.agents[role]["owned_paths"] = paths
        elif t == "task_created":
            self.tasks[e["task"]["id"]] = {**e["task"], "status": "pending"}
        elif t == "task_status":
            task = self.tasks.setdefault(e["task_id"], {"id": e["task_id"]})
            task["status"] = e["status"]
            if e.get("summary"):
                task["summary"] = e["summary"]
        elif t == "message":
            self.messages.append({k: e.get(k) for k in ("ts", "sender", "recipient", "kind", "subject", "content")})
            self.messages = self.messages[-500:]
        elif t == "artifact_published":
            self.artifacts[e["key"]] = {"key": e["key"], "owner": e["agent"], "kind": e.get("kind")}
        elif t == "file_written":
            self.files[e["path"]] = e["agent"]
        elif t == "error":
            self.errors.append(e.get("message", ""))
        elif t == "run_completed":
            self.status = "completed"
            self.usage = e.get("usage", {})
        elif t == "run_failed":
            self.status = "failed"
            self.errors.append(e.get("error", ""))

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "requirement": self.requirement,
            "project_dir": self.project_dir,
            "phase": self.phase,
            "phases": self.phases,
            "agents": list(self.agents.values()),
            "tasks": list(self.tasks.values()),
            "messages": self.messages[-200:],
            "artifacts": list(self.artifacts.values()),
            "files": [{"path": p, "owner": o} for p, o in sorted(self.files.items())],
            "errors": self.errors,
            "usage": self.usage,
            "routing": self.routing,
            "tech_preferences": self.tech_preferences,
            "llm": self.llm,
        }
