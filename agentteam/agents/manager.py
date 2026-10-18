"""Project Manager: team staffing, delivery planning and status reporting."""

from __future__ import annotations

import json

from ..render import plan_md, team_md
from .base import Agent
from .catalog import BUILDER_ROLES, CORE_ROLES, ROLE_CATALOG, catalog_for_prompt

PLAN_SCHEMA = """{
  "milestones": [{"id": "M1", "name": "...", "goal": "..."}],
  "tasks": [{
    "id": "T1",
    "title": "...",
    "description": "precise description of what to build, referencing requirement IDs, components, endpoints, entities",
    "assignee": "<role id>",
    "milestone": "M1",
    "complexity": "low|medium|high",
    "depends_on": ["T0"],
    "deliverables": ["exact/file/paths/the/task/creates.ext"],
    "acceptance_criteria": ["..."]
  }],
  "risks": [{"risk": "...", "mitigation": "..."}]
}"""


class ProjectManager(Agent):
    async def select_team(self) -> dict:
        req = self.ctx.artifacts.get("requirements") or {}
        prompt = f"""Decide which roles must be staffed to deliver this product. Staff only what the requirements
actually need (e.g. no mobile developer without a mobile app, no data engineer without pipelines/analytics), but make
sure the team can deliver a complete, production-ready enterprise product.

Available roles:
{catalog_for_prompt()}

JSON shape:
{{"team": [{{"role": "<role id>", "justification": "..."}}], "excluded": [{{"role": "<role id>", "reason": "..."}}]}}"""
        sections = [self.artifact_section("requirements", "Requirements specification")]
        result = await self.work("select_team", prompt, mode="json", sections=sections, allow_questions=False)

        chosen: dict[str, str] = {}
        for m in result.get("team") or []:
            role = str(m.get("role", "")).strip() if isinstance(m, dict) else str(m)
            if role in ROLE_CATALOG:
                chosen[role] = m.get("justification", "") if isinstance(m, dict) else ""
        for role in CORE_ROLES:
            chosen.setdefault(role, "Core delivery role")
        if not any(r in BUILDER_ROLES for r in chosen):
            chosen["backend_developer"] = "At least one implementation role is required"
        if "security" in json.dumps(req.get("non_functional_requirements", [])).lower() and "security_engineer" not in chosen:
            chosen["security_engineer"] = "Security NFRs present in requirements"

        team = {
            "members": [
                {"role": r, "title": ROLE_CATALOG[r].title, "justification": j}
                for r, j in sorted(chosen.items(), key=lambda kv: list(ROLE_CATALOG).index(kv[0]))
            ],
            "excluded": [e for e in (result.get("excluded") or []) if isinstance(e, dict) and e.get("role") not in chosen],
        }
        self.ctx.artifacts.publish(self.role, "team", team)
        self.ws.write("docs/plan/team.md", team_md(team))
        return team

    async def plan(self, assignable: list[str]) -> dict:
        ownership = "\n".join(
            f"- {role}: {', '.join(self.ctx.project.owned_by(role)) or '(no paths)'}" for role in assignable
        )
        budget = self.settings.max_context_chars
        sections = [
            self.artifact_section("requirements", "Requirements specification", max_chars=budget // 3),
            self.artifact_section("architecture", "Architecture", max_chars=budget // 2),
            self.artifact_section("threat_model", "Threat model / security requirements", max_chars=budget // 8),
        ]
        prompt = f"""Create the development plan (work breakdown) for the implementation phase.

Assignable roles and the paths they own (a task's deliverables MUST be inside its assignee's owned paths):
{ownership}

Planning rules:
- Break the work into small, concrete tasks; each task must be completable in ONE response (at most ~6 files).
- Start each component with a scaffolding task (build files, configuration, entry point, health endpoint).
- Order tasks with depends_on so contracts are produced before consumers (e.g. DB schema -> entities ->
  services -> API -> frontend API client -> pages).
- Cover EVERY 'must' functional requirement and the key NFRs (security, logging, validation, config).
- Each component owner also writes unit tests for their code inside their own paths.
- Do NOT plan QA test-suite, CI/CD, containerisation or documentation tasks; QA, DevOps and Technical Writer have dedicated phases.
- Use realistic file paths consistent with the architecture directory layout.
- Rate each task's "complexity" honestly - it decides which AI model tier builds it (cost control):
  low = scaffolding, configuration, boilerplate, simple CRUD/DTOs/static pages;
  medium = typical feature/business logic, API endpoints with validation, UI pages with state;
  high = intricate domain rules, security/auth, payments, concurrency, complex integrations or algorithms.

JSON shape:
{PLAN_SCHEMA}"""
        plan = await self.work("plan_project", prompt, mode="json", sections=sections, allow_questions=True)
        plan["tasks"] = self._validate_tasks(plan.get("tasks") or [], assignable)
        self.ctx.artifacts.publish(self.role, "plan", plan)
        self.ws.write("docs/plan/project-plan.md", plan_md(plan))
        return plan

    def _validate_tasks(self, raw_tasks: list, assignable: list[str]) -> list[dict]:
        tasks: list[dict] = []
        ids: set[str] = set()
        for i, t in enumerate(raw_tasks, 1):
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or f"T{i}").strip()
            if tid in ids:
                tid = f"{tid}-{i}"
            ids.add(tid)
            deliverables = [str(d) for d in (t.get("deliverables") or []) if d]
            assignee = str(t.get("assignee", "")).strip()
            if assignee not in assignable:
                owners = []
                for d in deliverables:
                    try:
                        owners.append(self.ctx.project.owner_of(d))
                    except ValueError:
                        pass
                assignee = next((o for o in owners if o in assignable), assignable[0])
            tasks.append({
                "id": tid,
                "title": str(t.get("title") or f"Task {i}"),
                "description": str(t.get("description") or ""),
                "assignee": assignee,
                "milestone": t.get("milestone"),
                "complexity": str(t.get("complexity") or "medium").strip().lower()
                if str(t.get("complexity") or "").strip().lower() in {"low", "medium", "high"} else "medium",
                "depends_on": [str(d) for d in (t.get("depends_on") or [])],
                "deliverables": deliverables,
                "acceptance_criteria": t.get("acceptance_criteria") or [],
            })
        known = {t["id"] for t in tasks}
        for t in tasks:
            t["depends_on"] = [d for d in t["depends_on"] if d in known and d != t["id"]]
        # Break dependency cycles (Kahn's algorithm; leftover nodes lose deps on each other).
        remaining = {t["id"]: set(t["depends_on"]) for t in tasks}
        resolved: set[str] = set()
        while True:
            ready = [tid for tid, deps in remaining.items() if deps <= resolved]
            if not ready:
                break
            for tid in ready:
                resolved.add(tid)
                remaining.pop(tid)
        if remaining:
            cyclic = set(remaining)
            for t in tasks:
                if t["id"] in cyclic:
                    t["depends_on"] = [d for d in t["depends_on"] if d not in cyclic]
        return tasks

    async def delivery_report(self, facts: dict) -> str:
        sections = [
            self.artifact_section("requirements", "Requirements", max_chars=12000),
            self.artifact_section("plan", "Plan", max_chars=12000),
            self.section("Delivery facts", json.dumps(facts, indent=1, default=str)),
        ]
        prompt = (
            "Write the final delivery report in Markdown for the customer and stakeholders: executive summary, "
            "scope delivered vs requirements (by FR id), team & process, quality status (tests, open defects), "
            "security status, deployment status and how to run it, known limitations, recommended next steps."
        )
        report = await self.work("delivery_report", prompt, mode="text", sections=sections, allow_questions=False)
        self.ws.write("docs/plan/delivery-report.md", report + "\n")
        return report
