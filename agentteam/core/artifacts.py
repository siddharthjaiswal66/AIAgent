"""Shared, read-mostly artifact board.

Each artifact (requirements, architecture, plan, test report, ...) has exactly
one owner. Anyone can read it; only the owner can publish new versions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import EventBus


class ArtifactOwnershipError(PermissionError):
    pass


@dataclass
class Artifact:
    key: str
    owner: str
    kind: str  # json | markdown | text
    content: Any
    version: int = 1
    updated: float = field(default_factory=time.time)


class ArtifactStore:
    def __init__(self, events: EventBus, persist_dir: Path | None = None):
        self._events = events
        self._items: dict[str, Artifact] = {}
        self._dir = persist_dir
        if persist_dir:
            persist_dir.mkdir(parents=True, exist_ok=True)

    def publish(self, owner: str, key: str, content: Any, kind: str = "json") -> Artifact:
        existing = self._items.get(key)
        if existing and existing.owner != owner:
            raise ArtifactOwnershipError(f"Artifact '{key}' is owned by {existing.owner}; {owner} cannot modify it")
        version = existing.version + 1 if existing else 1
        art = Artifact(key=key, owner=owner, kind=kind, content=content, version=version)
        self._items[key] = art
        if self._dir:
            suffix = ".json" if kind == "json" else ".md"
            data = json.dumps(content, indent=2, default=str) if kind == "json" else str(content)
            (self._dir / f"{key}{suffix}").write_text(data, encoding="utf-8")
        self._events.emit("artifact_published", agent=owner, key=key, kind=kind, version=version)
        return art

    def get(self, key: str, default: Any = None) -> Any:
        art = self._items.get(key)
        return art.content if art else default

    def has(self, key: str) -> bool:
        return key in self._items

    def keys(self) -> list[str]:
        return list(self._items)

    def owned_by(self, owner: str) -> list[str]:
        return [k for k, a in self._items.items() if a.owner == owner]

    def render(self, key: str, max_chars: int | None = None) -> str:
        art = self._items.get(key)
        if not art:
            return ""
        text = json.dumps(art.content, indent=1, default=str) if art.kind == "json" else str(art.content)
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return text
