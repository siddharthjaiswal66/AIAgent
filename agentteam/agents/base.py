"""Base agent: persona, isolated workspace, LLM reasoning and team communication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..config import Settings
from ..core.artifacts import ArtifactStore
from ..core.events import EventBus
from ..core.jsonutil import extract_json, parse_files_response
from ..core.messages import BROADCAST, Message, MessageBus
from ..core.runner import CommandRunner
from ..core.workspace import OwnershipViolation, ProjectWorkspace
from ..llm.router import ModelRouter
from .catalog import RoleSpec

HumanInput = Callable[[str], Awaitable[str]]

FILES_FORMAT = """\
Respond in EXACTLY this format:

1) A metadata block fenced as ```json ... ``` with this shape:
{
  "summary": "what you did and why",
  "needs_info": false,
  "questions": [{"to": "<teammate role>", "question": "..."}],
  "notes_for_team": [{"to": "<teammate role or 'all'>", "message": "contract/interface info teammates must know"}]%EXTRA%
}
2) Then EVERY file you create or modify, each one COMPLETE (no placeholders, no '...', no 'rest unchanged'):
=== FILE: relative/path/from/project/root ===
<entire file content>
=== END FILE ===

Rules:
- Only write files inside YOUR owned paths. Writes elsewhere are rejected and turned into change requests to the owner.
- If you are truly blocked by missing information that a teammate owns, set "needs_info": true, list your
  questions and output NO files. You will get the answers and be asked again.
"""

JSON_FORMAT = "Respond with ONE valid JSON object only - no prose before or after it."


@dataclass
class TeamContext:
    settings: Settings
    llm: ModelRouter
    bus: MessageBus
    artifacts: ArtifactStore
    project: ProjectWorkspace
    events: EventBus
    runner: CommandRunner
    human_input: HumanInput | None = None


class Agent:
    """A team member. Holds only its own private memory; reads shared artifacts;
    writes only to its owned paths; talks to others only via the MessageBus."""

    def __init__(self, spec: RoleSpec, ctx: TeamContext):
        self.spec = spec
        self.role = spec.role
        self.title = spec.title
        self.ctx = ctx
        self.ws = ctx.project.view_for(self.role)
        self.memory: list[str] = []  # private work log
        self.change_requests_sent: list[dict] = []
        ctx.bus.register(self.role, self.answer_question)

    # ------------------------------------------------------------------ helpers
    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    def status(self, status: str, activity: str = "") -> None:
        self.ctx.events.emit("agent_status", agent=self.role, status=status, activity=activity)

    def log(self, message: str) -> None:
        self.ctx.events.emit("log", agent=self.role, message=message)

    def team_roster(self) -> str:
        team = self.ctx.artifacts.get("team") or {}
        members = team.get("members") or [{"role": r} for r in self.ctx.bus.members()]
        return "\n".join(f"- {m['role']}: {m.get('title', '')}" for m in members)

    def system_prompt(self) -> str:
        owned = ", ".join(self.ws.owned_paths()) or "(none yet)"
        return (
            f"You are the {self.title} ({self.role}) in an autonomous enterprise software delivery team.\n"
            f"Mission: {self.spec.mission}\n"
            f"Expertise: {self.spec.expertise}\n\n"
            "Operating principles:\n"
            "- Stay strictly within your role. Never do another role's job; rely on teammates for their areas.\n"
            f"- You have WRITE access only to these project paths: {owned}. Everything else is read-only to you.\n"
            "- Coordinate through questions and notes to teammates; respect contracts published by the architect.\n"
            "- Produce production-grade, enterprise-quality, complete and consistent work: security, validation, error\n"
            "  handling, logging, configuration via environment variables, and tests where applicable.\n"
            "- Never emit placeholders, TODOs, pseudo-code or truncated files.\n"
            "- Be precise: file paths are relative to the project root and use forward slashes."
        )

    def section(self, title: str, body: str | None) -> str:
        return f"## {title}\n{body.strip()}\n" if body and body.strip() else ""

    def artifact_section(self, key: str, title: str, max_chars: int = 30000) -> str:
        return self.section(title, self.ctx.artifacts.render(key, max_chars=max_chars))

    def _inbox_text(self) -> str:
        msgs = self.ctx.bus.read_inbox(self.role)
        if not msgs:
            return ""
        lines = [f"[{m.kind}] from {m.sender} - {m.subject}:\n{m.content}" for m in msgs]
        return "\n\n".join(lines)

    # ------------------------------------------------------------------ LLM calls
    async def _call(self, task: str, prompt: str, *, mode: str, sections: list[str], inbox: str,
                    extra_meta: str = "", max_tokens: int | None = None,
                    complexity: str | None = None, escalation: int = 0):
        parts = [
            f"[[TASK:{task}]]",
            self.section("Team roster (roles you can talk to)", self.team_roster()),
            self.section("Your owned paths (write access)", "\n".join(self.ws.owned_paths())),
            self.section("Unread messages from teammates", inbox),
            *sections,
            self.section("Your assignment", prompt),
        ]
        if mode == "json":
            parts.append(self.section("Response format", JSON_FORMAT))
        elif mode == "files":
            parts.append(self.section("Response format", FILES_FORMAT.replace("%EXTRA%", extra_meta)))
        user = "\n".join(p for p in parts if p)
        messages = [{"role": "user", "content": user}]

        for attempt in range(3):
            # An unusable reply escalates the next attempt to a stronger model tier.
            raw = await self.ctx.llm.complete(
                self.system_prompt(), messages, role=self.role, task=task, complexity=complexity,
                escalation=escalation + attempt, max_tokens=max_tokens,
            )
            if mode == "text":
                return raw.strip()
            if mode == "json":
                try:
                    data = extract_json(raw)
                    if isinstance(data, dict):
                        return data
                except ValueError:
                    pass
                fix = "Your reply was not a single valid JSON object. Reply again with ONLY the corrected JSON object."
            else:
                meta, files = parse_files_response(raw)
                if files or meta.get("needs_info") or meta.get("summary"):
                    return meta, files
                fix = ("Your reply did not follow the required format. Reply again with the ```json metadata block "
                       "followed by === FILE: ... === / === END FILE === blocks.")
            messages = [*messages, {"role": "assistant", "content": raw[:20000]}, {"role": "user", "content": fix}]
        raise RuntimeError(f"{self.role}: model failed to produce a valid '{mode}' response for task '{task}'")

    async def work(self, task: str, prompt: str, *, mode: str = "json", sections: list[str] | None = None,
                   allow_questions: bool = True, extra_meta: str = "", max_tokens: int | None = None,
                   complexity: str | None = None, escalation: int = 0):
        """Run an assignment. Handles teammate consultation, file writing and notes.

        `complexity` and `escalation` are routing hints used to pick a cost-appropriate model.
        Returns: json -> dict ; text -> str ; files -> (meta, written_paths)
        """
        sections = sections or []
        route_hints = {"complexity": complexity, "escalation": escalation}
        self._current_task = task
        self.status("working", task)
        inbox = self._inbox_text()
        try:
            result = await self._call(task, prompt, mode=mode, sections=sections, inbox=inbox,
                                      extra_meta=extra_meta, max_tokens=max_tokens, **route_hints)
            meta = result[0] if mode == "files" else result
            if allow_questions and isinstance(meta, dict) and meta.get("needs_info") and meta.get("questions"):
                answers = await self.consult(meta["questions"])
                follow_up = (
                    f"{prompt}\n\n## Answers from your teammates\n{answers}\n\n"
                    "You now have the information you need. Complete the assignment fully. Do not ask more questions; "
                    "where something is still unclear, make and document a reasonable assumption."
                )
                result = await self._call(task, follow_up, mode=mode, sections=sections, inbox=inbox,
                                          extra_meta=extra_meta, max_tokens=max_tokens, **route_hints)
                meta = result[0] if mode == "files" else result

            if mode == "files":
                meta, files = result
                written = self.apply_files(files)
                self.dispatch_notes(meta)
                self.memory.append(f"[{task}] {meta.get('summary', '')} -> {len(written)} files")
                return meta, written
            if isinstance(meta, dict):
                self.dispatch_notes(meta)
            return result
        finally:
            self.status("idle", "")

    # ------------------------------------------------------------------ communication
    async def consult(self, questions: list[dict]) -> str:
        answers = []
        for q in questions[: self.settings.max_questions_per_task]:
            to = str(q.get("to", "")).strip()
            text = str(q.get("question", "")).strip()
            if not to or not text:
                continue
            self.status("waiting", f"asking {to}")
            answer = await self.ctx.bus.ask(self.role, to, text, subject=text[:80])
            answers.append(f"Q to {to}: {text}\nA: {answer}")
        self.status("working", getattr(self, "_current_task", ""))
        return "\n\n".join(answers) or "(no answers)"

    async def answer_question(self, msg: Message) -> str:
        """Handler invoked by the MessageBus when a teammate asks this agent something."""
        own_artifacts = "\n\n".join(
            self.ctx.artifacts.render(k, max_chars=15000) for k in self.ctx.artifacts.owned_by(self.role)
        )
        my_files = self.ws.my_files()
        sections = [
            self.section("Artifacts you own", own_artifacts),
            self.section("Your recent work log", "\n".join(self.memory[-15:])),
            self.section("Files you own", "\n".join(my_files[:200])),
            self.section("Relevant file contents", self.ws.bundle(my_files, budget=min(20000, self.settings.max_context_chars // 3))),
        ]
        prompt = (
            f"Your teammate '{msg.sender}' asks you:\n\"{msg.content}\"\n\n"
            "Answer precisely and authoritatively from your area of responsibility (exact names, paths, endpoints, "
            "schemas, decisions). If it is outside your responsibility, say who owns it. Keep it concise."
        )
        self.status("answering", f"question from {msg.sender}")
        try:
            return await self._call("answer_question", prompt, mode="text", sections=sections, inbox="")
        finally:
            self.status("idle", "")

    def notify(self, to: str, subject: str, content: str, kind: str = "notification") -> None:
        self.ctx.bus.send(self.role, to, kind, subject, content)

    def dispatch_notes(self, meta: dict) -> None:
        for note in meta.get("notes_for_team") or []:
            if not isinstance(note, dict):
                continue
            to = str(note.get("to") or BROADCAST)
            message = str(note.get("message") or "").strip()
            if message and (to == BROADCAST or self.ctx.bus.is_member(to)):
                self.notify(to, f"Note from {self.title}", message)

    # ------------------------------------------------------------------ files
    def apply_files(self, files: list[dict]) -> list[str]:
        written: list[str] = []
        for f in files:
            path, content = f.get("path", ""), f.get("content", "")
            try:
                written.append(self.ws.write(path, content))
            except OwnershipViolation as v:
                self._request_change(v, content)
            except ValueError as exc:
                self.ctx.events.emit("error", agent=self.role, message=f"Rejected path '{path}': {exc}")
        return written

    def _request_change(self, violation: OwnershipViolation, content: str) -> None:
        owner = violation.owner
        record = {"path": violation.path, "owner": owner}
        self.change_requests_sent.append(record)
        if owner and self.ctx.bus.is_member(owner):
            preview = content if len(content) < 4000 else content[:4000] + "\n...[truncated]"
            self.notify(
                owner,
                f"Change request: {violation.path}",
                f"{self.title} needs '{violation.path}' (owned by you) to be created/changed as follows. "
                f"Please review and apply what is appropriate during your next task.\n\n{preview}",
                kind="change_request",
            )
        else:
            self.log(f"Dropped write to unowned path {violation.path}")

    # ------------------------------------------------------------------ shared capabilities
    async def fix_issues(self, issues: list[dict], reporter: str, label: str = "defects", escalation: int = 0) -> dict:
        """Fix defects/findings assigned to this agent. Available to every file-owning role.

        Model selection: severity drives complexity (critical -> high, all low -> low) and `escalation`
        (raised by the orchestrator when defects survive earlier fix rounds) moves to stronger tiers.
        """
        if not issues:
            return {"summary": "nothing to fix"}
        severities = {str(i.get("severity", "medium")).lower() for i in issues}
        complexity = "high" if "critical" in severities else "low" if severities <= {"low"} else "medium"
        paths: list[str] = []
        for issue in issues:
            paths.extend(issue.get("files") or [])
        paths += self.ws.my_files()
        budget = self.settings.max_context_chars
        sections = [
            self.artifact_section("architecture", "Architecture", max_chars=budget // 4),
            self.section("Project files", self.ws.tree()),
            self.section("Current code (read-only unless you own it)", self.ws.bundle(paths, budget=budget // 2)),
            self.section(f"{label.title()} reported by {reporter}", json.dumps(issues, indent=1)),
        ]
        prompt = (
            f"Fix every {label[:-1] if label.endswith('s') else label} listed above that falls within your owned paths. "
            "Return complete updated files. If an issue actually belongs to another role, explain in notes_for_team "
            "addressed to that role instead of editing their files."
        )
        meta, written = await self.work(f"fix_{label.replace(' ', '_')}", prompt, mode="files", sections=sections,
                                        complexity=complexity, escalation=escalation)
        if reporter != self.role:
            self.notify(reporter, f"Fixed {len(issues)} {label}", f"{meta.get('summary', '')}\nFiles: {', '.join(written)}", kind="handoff")
        return {"summary": meta.get("summary", ""), "files": written}

    def pending_change_requests(self) -> int:
        return sum(1 for m in self.ctx.bus.read_inbox(self.role, mark_read=False) if m.kind == "change_request")

    async def handle_change_requests(self) -> dict | None:
        """Apply change requests teammates sent because they may not write our files."""
        pending = [m for m in self.ctx.bus.read_inbox(self.role, mark_read=False) if m.kind == "change_request"]
        if not pending:
            return None
        for m in pending:
            m.read = True
        issues = [{"id": f"CR-{m.id}", "severity": "medium", "title": m.subject, "description": m.content,
                   "files": [m.subject.split(":", 1)[-1].strip()], "requested_by": m.sender} for m in pending]
        result = await self.fix_issues(issues, reporter=pending[0].sender, label="change requests")
        for sender in {m.sender for m in pending[1:]} - {pending[0].sender}:
            self.notify(sender, "Change requests processed", result.get("summary", ""), kind="handoff")
        return result
