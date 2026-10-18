"""Orchestrator ("delivery lead"): runs the SDLC phases and coordinates the team.

It never does an agent's work itself; it staffs the team, hands out assignments,
enforces ownership boundaries and routes findings to the agents that own the code.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from .agents import BUILDER_ROLES, ROLE_CATALOG, Agent, TeamContext, create_agent
from .agents.catalog import PROTECTED_PREFIXES
from .config import Settings
from .core import ArtifactStore, CommandRunner, EventBus, MessageBus, ProjectWorkspace, RunState
from .core.workspace import normalize
from .llm.router import ModelRouter

BLOCKING = {"critical", "high", "medium"}

PHASES = {
    "requirements": "Requirements analysis",
    "team": "Team formation",
    "architecture": "Architecture & system design",
    "planning": "Delivery planning",
    "development": "Development",
    "integration_review": "Architecture & integration review",
    "security_review": "Security review",
    "testing": "Testing & quality assurance",
    "deployment": "Build & deployment",
    "documentation": "Documentation",
    "delivery": "Delivery report",
}


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()[:limit].strip("-") or "project"


class Orchestrator:
    def __init__(self, settings: Settings, events: EventBus | None = None, human_input=None,
                 state: RunState | None = None):
        self.settings = settings
        self.events = events or EventBus()
        self.state = state or RunState(self.events)
        self.human_input = human_input
        self.agents: dict[str, Agent] = {}
        self.task_results: dict[str, dict] = {}
        self.ctx: TeamContext | None = None

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def phase(self, name: str):
        self.events.emit("phase_started", phase=name, title=PHASES.get(name, name))
        try:
            yield
        except Exception:
            self.events.emit("phase_completed", phase=name, status="failed")
            raise
        self.events.emit("phase_completed", phase=name, status="done")

    def hire(self, role: str, justification: str = "") -> Agent:
        if role in self.agents:
            return self.agents[role]
        agent = create_agent(role, self.ctx)
        self.agents[role] = agent
        self.events.emit("agent_joined", agent=role, title=ROLE_CATALOG[role].title, justification=justification,
                         owned_paths=self.ctx.project.owned_by(role))
        return agent

    def _setup(self, requirement: str, project_dir: Path | None) -> Path:
        root = project_dir or Path(self.settings.output_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(requirement)}"
        root = root.resolve()
        meta = root / ".agentteam"
        meta.mkdir(parents=True, exist_ok=True)
        self.events.set_log_file(meta / "events.jsonl")
        llm = ModelRouter(self.settings, self.events)
        self.ctx = TeamContext(
            settings=self.settings,
            llm=llm,
            bus=MessageBus(self.events, meta / "messages.jsonl"),
            artifacts=ArtifactStore(self.events, meta / "artifacts"),
            project=ProjectWorkspace(root, self.events),
            events=self.events,
            runner=CommandRunner(root, self.events, timeout=self.settings.command_timeout, enabled=self.settings.allow_execution),
            human_input=self.human_input,
        )
        (meta / "requirement.txt").write_text(requirement, encoding="utf-8")
        return root

    # ------------------------------------------------------------------ ownership
    def _default_ownership(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for role in self.agents:
            for prefix in ROLE_CATALOG[role].default_paths:
                mapping[prefix] = role
        return mapping

    def _build_ownership(self, arch: dict) -> dict[str, str]:
        mapping = self._default_ownership()
        proposals = dict(arch.get("ownership") or {})
        for comp in arch.get("components") or []:
            if isinstance(comp, dict) and comp.get("directory") and comp.get("owner_role"):
                proposals.setdefault(str(comp["directory"]), str(comp["owner_role"]))
        for raw, role in proposals.items():
            role = str(role).strip()
            try:
                key = normalize(str(raw))
            except ValueError:
                continue
            if not key or role not in self.agents:
                if role not in self.agents:
                    self.events.emit("log", agent="orchestrator", message=f"Ignoring ownership of '{raw}' for unstaffed role '{role}'")
                continue
            if key.lower().startswith(PROTECTED_PREFIXES):
                continue
            mapping[key + ("/" if str(raw).replace("\\", "/").endswith("/") else "")] = role
        qa_code_paths = [p for p, r in mapping.items() if r == "qa_engineer" and not p.startswith("docs/")]
        if not qa_code_paths:
            mapping["tests/"] = "qa_engineer"
        return mapping

    # ------------------------------------------------------------------ main flow
    async def run(self, requirement: str, project_dir: Path | None = None) -> dict:
        root = self._setup(requirement, project_dir)
        ctx = self.ctx
        self.events.emit("run_started", requirement=requirement, project_dir=str(root),
                         provider=self.settings.provider, model=self.settings.resolved_model(),
                         routing=ctx.llm.describe(), tech_preferences=self.settings.tech_preferences)
        started = time.time()
        try:
            for role in ("business_analyst", "project_manager", "solution_architect"):
                self.hire(role, "Core leadership role")
            ctx.project.set_ownership(self._default_ownership())

            analyst = self.agents["business_analyst"]
            pm = self.agents["project_manager"]
            architect = self.agents["solution_architect"]

            with self.phase("requirements"):
                req = await analyst.analyze(requirement)
                ctx.llm.set_project_complexity(req.get("complexity"))

            with self.phase("team"):
                team = await pm.select_team()
                for m in team["members"]:
                    self.hire(m["role"], m.get("justification", ""))
                ctx.project.set_ownership(self._default_ownership())

            with self.phase("architecture"):
                arch = await architect.design()
                arch = await self._approve_architecture(architect, arch)
                ctx.project.set_ownership(self._build_ownership(arch))
                architect.notify(
                    "all", "Architecture baseline published",
                    f"Style: {arch.get('architecture_style')}\nStack: {json.dumps(arch.get('tech_stack'))}\n"
                    f"Code ownership:\n" + "\n".join(f"  {p} -> {r}" for p, r in sorted(ctx.project.ownership().items())),
                )
                if "security_engineer" in self.agents:
                    await self.agents["security_engineer"].threat_model()

            builders = [r for r in self.agents if r in BUILDER_ROLES]
            with self.phase("planning"):
                plan = await pm.plan(builders)
                for task in plan["tasks"]:
                    self.events.emit("task_created", agent="project_manager", task=task)

            with self.phase("development"):
                await self._execute_plan(plan["tasks"])
                await self._process_change_requests()

            with self.phase("integration_review"):
                review = await architect.review_integration()
                await self._dispatch_fixes(review.get("findings") or [], "solution_architect", "architecture findings")
                await self._process_change_requests()

            security_report = None
            if "security_engineer" in self.agents:
                with self.phase("security_review"):
                    security_report = await self.agents["security_engineer"].review()
                    await self._dispatch_fixes(security_report.get("findings") or [], "security_engineer", "security findings")
                    await self._process_change_requests()

            with self.phase("testing"):
                qa_report = await self._quality_loop()

            with self.phase("deployment"):
                devops = self.agents["devops_engineer"]
                await devops.build_delivery_pipeline()
                await self._process_change_requests()
                deployment = await devops.verify_and_deploy(self.settings.allow_deploy)

            with self.phase("documentation"):
                await self.agents["technical_writer"].write_docs()

            open_defects = [d for d in qa_report.get("defects") or [] if isinstance(d, dict)]
            facts = {
                "project": req.get("title"),
                "team": [m["role"] for m in team["members"]],
                "tasks_total": len(plan["tasks"]),
                "tasks_done": sum(1 for t in self.state.tasks.values() if t.get("status") == "done"),
                "tasks_failed": [t["id"] for t in self.state.tasks.values() if t.get("status") == "failed"],
                "qa_status": qa_report.get("status"),
                "qa_iterations": qa_report.get("iteration"),
                "tests_executed": qa_report.get("executed"),
                "open_defects": open_defects,
                "security_status": (security_report or {}).get("status", "not staffed"),
                "deployment": {k: deployment.get(k) for k in ("verified", "deployed", "access_urls", "note")},
                "files_delivered": len(ctx.project.list_files()),
                "agent_messages": len(ctx.bus.transcript),
                "tech_stack": arch.get("tech_stack"),
                "ai_usage": {k: ctx.llm.usage.as_dict().get(k) for k in
                             ("calls", "total_tokens", "est_cost_usd", "est_savings_pct", "by_tier")},
                "duration_minutes": round((time.time() - started) / 60, 1),
            }
            with self.phase("delivery"):
                await pm.delivery_report(facts)

            result = {"status": "completed", "project_dir": str(root), **facts, "usage": ctx.llm.usage.as_dict()}
            (root / ".agentteam" / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
            self.events.emit("run_completed", project_dir=str(root), usage=ctx.llm.usage.as_dict(), result=result)
            return result
        except Exception as exc:
            self.events.emit("run_failed", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            raise
        finally:
            await ctx.llm.aclose()

    # ------------------------------------------------------------------ development scheduling
    async def _execute_plan(self, tasks: list[dict]) -> None:
        status = {t["id"]: "pending" for t in tasks}
        busy: set[str] = set()
        running: dict[asyncio.Task, dict] = {}
        while True:
            for t in tasks:
                if status[t["id"]] != "pending" or t["assignee"] in busy:
                    continue
                if all(status.get(d) in ("done", "failed") for d in t["depends_on"]):
                    status[t["id"]] = "in_progress"
                    busy.add(t["assignee"])
                    self.events.emit("task_status", agent=t["assignee"], task_id=t["id"], status="in_progress")
                    related: list[str] = []
                    for dep in t["depends_on"]:
                        related += (self.task_results.get(dep) or {}).get("files", [])
                    running[asyncio.create_task(self._run_task(t, related))] = t
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                t = running.pop(fut)
                busy.discard(t["assignee"])
                exc = fut.exception()
                if exc:
                    status[t["id"]] = "failed"
                    self.events.emit("task_status", agent=t["assignee"], task_id=t["id"], status="failed", summary=str(exc))
                    self.events.emit("error", agent=t["assignee"], message=f"Task {t['id']} failed: {exc}")
                else:
                    status[t["id"]] = "done"
                    self.task_results[t["id"]] = fut.result()
                    self.events.emit("task_status", agent=t["assignee"], task_id=t["id"], status="done",
                                     summary=fut.result().get("summary", ""))

    async def _run_task(self, task: dict, related: list[str]) -> dict:
        agent = self.agents[task["assignee"]]
        try:
            return await agent.implement(task, related)
        except Exception as exc:
            # One retry on a stronger model tier before giving up on the task.
            self.events.emit("log", agent=agent.role, message=f"Task {task['id']} failed ({exc}); retrying on a stronger model")
            return await agent.implement(task, related, escalation=1)

    async def _approve_architecture(self, architect: Agent, arch: dict) -> dict:
        """In interactive mode the customer can approve or redirect the architect's technology decisions."""
        if not (self.settings.interactive and self.human_input):
            return arch
        for _ in range(3):
            stack = "\n".join(f"  {k}: {v}" for k, v in (arch.get("tech_stack") or {}).items())
            answer = (await self.human_input(
                f"[Solution Architect proposes]\nStyle: {arch.get('architecture_style')}\n{stack}\n"
                "Approve this architecture? Press Enter to approve, or describe what to change"
            )).strip()
            if not answer or answer.lower() in {"y", "yes", "ok", "approve", "approved"}:
                self.events.emit("log", agent="solution_architect", message="Architecture approved by customer")
                break
            self.events.emit("log", agent="solution_architect", message=f"Customer feedback on architecture: {answer}")
            arch = await architect.design(feedback=answer)
        return arch

    # ------------------------------------------------------------------ routing of findings
    def _owner_for(self, item: dict, reporter: str) -> str | None:
        owner = str(item.get("owner_role") or "").strip()
        if owner in self.agents:
            return owner
        for f in item.get("files") or []:
            try:
                o = self.ctx.project.owner_of(f)
            except ValueError:
                continue
            if o in self.agents:
                return o
        return None

    async def _dispatch_fixes(self, items: list, reporter: str, label: str, escalation: int = 0) -> None:
        groups: dict[str, list[dict]] = {}
        for item in items:
            if not isinstance(item, dict) or str(item.get("severity", "medium")).lower() not in BLOCKING:
                continue
            owner = self._owner_for(item, reporter)
            if owner:
                groups.setdefault(owner, []).append(item)
            else:
                self.events.emit("log", agent="orchestrator", message=f"Unroutable finding: {item.get('title')}")
        if not groups:
            return
        for owner, its in groups.items():
            self.ctx.bus.send(reporter, owner, "bug_report", f"{len(its)} {label} assigned to you",
                              json.dumps(its, indent=1))
        results = await asyncio.gather(
            *(self.agents[o].fix_issues(its, reporter, label, escalation=escalation) for o, its in groups.items()),
            return_exceptions=True,
        )
        for owner, res in zip(groups, results):
            if isinstance(res, Exception):
                self.events.emit("error", agent=owner, message=f"Fixing {label} failed: {res}")

    async def _process_change_requests(self, rounds: int = 2) -> None:
        for _ in range(rounds):
            pending = [a for a in self.agents.values() if a.pending_change_requests()]
            if not pending:
                return
            results = await asyncio.gather(*(a.handle_change_requests() for a in pending), return_exceptions=True)
            for agent, res in zip(pending, results):
                if isinstance(res, Exception):
                    self.events.emit("error", agent=agent.role, message=f"Change requests failed: {res}")

    async def _quality_loop(self) -> dict:
        qa = self.agents["qa_engineer"]
        await qa.write_tests()
        await self._process_change_requests()
        report: dict = {}
        for iteration in range(1, self.settings.max_qa_iterations + 1):
            results = await qa.run_tests()
            report = await qa.evaluate(iteration, results)
            blocking = [d for d in report.get("defects") or [] if isinstance(d, dict)
                        and str(d.get("severity", "")).lower() in BLOCKING]
            self.events.emit("log", agent="qa_engineer",
                             message=f"QA iteration {iteration}: status={report.get('status')} blocking defects={len(blocking)}")
            if report.get("status") == "pass" and not blocking:
                break
            if not blocking:
                self.events.emit("log", agent="qa_engineer",
                                 message="No actionable blocking defects identified; ending QA loop.")
                break
            if iteration == self.settings.max_qa_iterations:
                break
            # Defects that survive a fix round are retried on progressively stronger models.
            await self._dispatch_fixes(blocking, "qa_engineer", "defects", escalation=iteration - 1)
            await self._process_change_requests()
        return report
