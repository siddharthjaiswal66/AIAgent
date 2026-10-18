"""Project workspace with enforced path ownership.

The generated application lives in a single project directory. Every path
prefix is owned by exactly one agent role. An agent may READ any file, but may
only WRITE files inside the prefixes it owns. Attempts to write elsewhere raise
OwnershipViolation - the agent must instead send a change request to the owner.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from .events import EventBus

IGNORED_DIRS = {".agentteam", ".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build", "dist", ".idea", ".gradle", ".next"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".jar", ".class", ".zip", ".gz", ".pdf", ".exe", ".dll", ".so", ".pyc", ".woff", ".woff2", ".ttf"}


class OwnershipViolation(PermissionError):
    def __init__(self, agent: str, path: str, owner: str | None):
        self.agent, self.path, self.owner = agent, path, owner
        who = owner or "nobody (unassigned path)"
        super().__init__(f"{agent} may not write '{path}'; it is owned by {who}")


def normalize(rel_path: str) -> str:
    p = rel_path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    parts = PurePosixPath(p).parts
    if any(part == ".." for part in parts) or (parts and ":" in parts[0]):
        raise ValueError(f"Illegal path outside project: {rel_path}")
    return "/".join(parts)


class ProjectWorkspace:
    def __init__(self, root: Path, events: EventBus):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._events = events
        self._ownership: dict[str, str] = {}  # prefix -> role
        self.write_log: dict[str, str] = {}  # path -> last writer

    # ------------------------------------------------------------------ ownership
    def set_ownership(self, mapping: dict[str, str]) -> None:
        self._ownership = {normalize(k) + ("/" if k.endswith("/") else ""): v for k, v in mapping.items() if k}
        by_role: dict[str, list[str]] = {}
        for prefix, role in sorted(self._ownership.items()):
            by_role.setdefault(role, []).append(prefix)
        self._events.emit("ownership_assigned", ownership=by_role)

    def ownership(self) -> dict[str, str]:
        return dict(self._ownership)

    def owned_by(self, role: str) -> list[str]:
        return sorted(p for p, r in self._ownership.items() if r == role)

    def owner_of(self, rel_path: str) -> str | None:
        path = normalize(rel_path).lower()
        best, best_len = None, -1
        for prefix, role in self._ownership.items():
            pl = prefix.lower()
            if pl.endswith("/"):
                match = path.startswith(pl)
            else:
                match = path == pl or path.startswith(pl + "/")
            if match and len(pl) > best_len:
                best, best_len = role, len(pl)
        return best

    # ------------------------------------------------------------------ IO
    def _abs(self, rel_path: str) -> Path:
        target = (self.root / normalize(rel_path)).resolve()
        if os.path.commonpath([str(target), str(self.root)]) != str(self.root):
            raise ValueError(f"Path escapes project root: {rel_path}")
        return target

    def write(self, agent: str, rel_path: str, content: str) -> str:
        rel = normalize(rel_path)
        owner = self.owner_of(rel)
        if owner != agent:
            self._events.emit("ownership_violation", agent=agent, path=rel, owner=owner)
            raise OwnershipViolation(agent, rel, owner)
        target = self._abs(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8", newline="\n")
        self.write_log[rel] = agent
        self._events.emit("file_written", agent=agent, path=rel, bytes=len(content), action="updated" if existed else "created")
        return rel

    def read(self, rel_path: str) -> str | None:
        try:
            target = self._abs(rel_path)
        except ValueError:
            return None
        if not target.is_file() or target.suffix.lower() in BINARY_SUFFIXES:
            return None
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    def exists(self, rel_path: str) -> bool:
        try:
            return self._abs(rel_path).exists()
        except ValueError:
            return False

    def list_files(self, prefix: str = "") -> list[str]:
        base = self._abs(prefix) if prefix else self.root
        if not base.exists():
            return []
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for name in sorted(filenames):
                full = Path(dirpath) / name
                out.append(full.relative_to(self.root).as_posix())
        return out

    def tree(self, max_entries: int = 400) -> str:
        files = self.list_files()
        lines = []
        for f in files[:max_entries]:
            lines.append(f"{f}   [owner: {self.owner_of(f) or '-'}]")
        if len(files) > max_entries:
            lines.append(f"... and {len(files) - max_entries} more files")
        return "\n".join(lines) or "(empty project)"

    def bundle(self, paths: list[str], budget: int) -> str:
        """Concatenate file contents into a prompt-friendly block, within a char budget."""
        chunks, used = [], 0
        seen = set()
        for p in paths:
            try:
                rel = normalize(p)
            except ValueError:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            content = self.read(rel)
            if content is None:
                continue
            block = f"--- {rel} (owner: {self.owner_of(rel) or '-'}) ---\n{content}\n"
            if used + len(block) > budget:
                remaining = budget - used
                if remaining > 500:
                    chunks.append(block[:remaining] + "\n...[truncated]\n")
                chunks.append(f"[context budget reached; {len(paths) - len(seen)} more files omitted]")
                break
            chunks.append(block)
            used += len(block)
        return "".join(chunks)

    def view_for(self, agent: str) -> "AgentWorkspace":
        return AgentWorkspace(self, agent)


class AgentWorkspace:
    """An agent's restricted handle on the project: read anything, write only what it owns."""

    def __init__(self, project: ProjectWorkspace, agent: str):
        self._project = project
        self.agent = agent

    @property
    def root(self) -> Path:
        return self._project.root

    def owned_paths(self) -> list[str]:
        return self._project.owned_by(self.agent)

    def can_write(self, rel_path: str) -> bool:
        try:
            return self._project.owner_of(rel_path) == self.agent
        except ValueError:
            return False

    def owner_of(self, rel_path: str) -> str | None:
        return self._project.owner_of(rel_path)

    def write(self, rel_path: str, content: str) -> str:
        return self._project.write(self.agent, rel_path, content)

    def read(self, rel_path: str) -> str | None:
        return self._project.read(rel_path)

    def list_files(self, prefix: str = "") -> list[str]:
        return self._project.list_files(prefix)

    def my_files(self) -> list[str]:
        return [f for f in self._project.list_files() if self.can_write(f)]

    def tree(self) -> str:
        return self._project.tree()

    def bundle(self, paths: list[str], budget: int) -> str:
        return self._project.bundle(paths, budget)
