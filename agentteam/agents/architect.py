"""Solution Architect: system design, contracts, ownership and integration review."""

from __future__ import annotations

import json
import re

from ..render import architecture_md
from .base import Agent
from .catalog import ROLE_CATALOG

ARCHITECTURE_SCHEMA = """{
  "project_name": "...",
  "architecture_style": "e.g. modular monolith / microservices / event-driven / serverless - with justification",
  "overview": "narrative of the solution",
  "tech_stack": {"backend": "...", "frontend": "...", "database": "...", "messaging": "...", "infrastructure": "...", "testing": "...", "other": ["..."]},
  "components": [{"name": "...", "type": "service|web-app|database|library|worker|mobile-app|pipeline", "responsibility": "...",
                  "owner_role": "<team role>", "directory": "path/", "interfaces": ["..."], "depends_on": ["..."]}],
  "data_model": [{"entity": "...", "fields": [{"name": "...", "type": "...", "constraints": "..."}], "relationships": ["..."]}],
  "api_contracts": [{"method": "GET|POST|PUT|PATCH|DELETE", "path": "/api/v1/...", "description": "...", "auth": "role(s) or public",
                     "request": "schema or example", "response": "schema or example", "errors": ["400 ...", "404 ..."]}],
  "events": [{"name": "...", "producer": "...", "consumers": ["..."], "payload": "..."}],
  "directory_layout": "tree of the whole repository",
  "ownership": {"path/prefix/": "<team role>", "exact/file.ext": "<team role>"},
  "environment_variables": [{"name": "...", "description": "...", "component": "...", "example": "..."}],
  "cross_cutting": {"security": "...", "authentication": "...", "logging": "...", "observability": "...", "error_handling": "...",
                    "configuration": "...", "validation": "...", "scalability": "...", "performance": "..."},
  "coding_standards": ["..."],
  "build_commands": ["commands run from repo root, e.g. 'cd backend && mvn -q -DskipTests package'"],
  "test_commands": ["commands run from repo root that execute ALL automated tests"],
  "run_commands": ["commands to run the system locally"],
  "adrs": [{"id": "ADR-001", "title": "...", "context": "...", "decision": "...", "consequences": "..."}],
  "diagrams": {"system_context": "mermaid flowchart source", "containers": "mermaid flowchart source", "domain_model": "mermaid classDiagram/erDiagram source"}
}"""

FINDINGS_SCHEMA = """{
  "status": "pass|fail",
  "summary": "...",
  "findings": [{"id": "ARCH-001", "severity": "critical|high|medium|low", "owner_role": "<role that must fix it>",
                "files": ["path"], "title": "...", "description": "...", "suggested_fix": "..."}]
}"""


class SolutionArchitect(Agent):
    async def design(self, feedback: str | None = None) -> dict:
        team = self.ctx.artifacts.get("team") or {}
        req = self.ctx.artifacts.get("requirements") or {}
        roles = [m["role"] for m in team.get("members", [])]
        default_paths = "\n".join(
            f"- {r}: {', '.join(ROLE_CATALOG[r].default_paths)}" for r in roles if r in ROLE_CATALOG
        )
        constraints = [str(c) for c in req.get("technology_constraints") or [] if str(c).strip()]
        if constraints:
            tech_policy = (
                "MANDATORY customer technology constraints (non-negotiable, follow exactly):\n"
                + "\n".join(f"- {c}" for c in constraints)
                + "\nFor every layer NOT covered by these constraints, you decide."
            )
        else:
            tech_policy = "The customer imposed no technology constraints - you own every technology decision."
        prompt = f"""Design the complete solution architecture for the requirements.

Technology decision policy:
{tech_policy}
When you decide, evaluate at least two credible options per major layer against the NFRs (performance, security,
scalability, availability), team skills implied by the staffed roles, ecosystem maturity, licence cost (prefer free /
open-source), operability and hiring market. Record each major choice as an ADR listing the alternatives considered.

The staffed team and their default owned paths:
{default_paths}

Design rules:
- Choose a modern, well-supported, free/open-source technology stack appropriate to the requirements and NFRs.
- Define clear component boundaries, owner roles, API contracts (every endpoint the frontend/clients need), data model
  and events. Contracts are binding for all developers.
- "ownership" must map EVERY source directory/file of the repository to exactly ONE role from the team.
  Use path prefixes ending with '/' for directories. Component unit tests live inside the component and are owned by its
  developer; cross-component/integration/E2E tests live under tests/ (qa_engineer). Container/CI files (e.g.
  'backend/Dockerfile') belong to devops_engineer. Do not assign anything under docs/.
- build/test commands must work on a developer machine with the standard toolchain for the chosen stack.
- Provide mermaid diagrams (C4-style context and container views, domain model).

JSON shape:
{ARCHITECTURE_SCHEMA}"""
        if feedback:
            previous = self.ctx.artifacts.render("architecture", max_chars=self.settings.max_context_chars // 2)
            prompt += (
                f"\n\n## Your previous architecture\n{previous}\n\n## Customer feedback (must be addressed)\n{feedback}\n"
                "Revise the architecture to address the feedback; keep everything else consistent. Return the full JSON."
            )
        sections = [self.artifact_section("requirements", "Requirements specification")]
        arch = await self.work("design_architecture", prompt, mode="json", sections=sections, allow_questions=not feedback)

        if not isinstance(arch.get("ownership"), dict):
            arch["ownership"] = {}
        for key in ("build_commands", "test_commands", "run_commands", "components", "api_contracts", "adrs"):
            value = arch.get(key)
            if isinstance(value, str) and value.strip():
                arch[key] = [value]
            elif not isinstance(value, list):
                arch[key] = []

        self.ctx.artifacts.publish(self.role, "architecture", arch)
        self.ws.write("docs/architecture/architecture.md", architecture_md(arch))
        self.ws.write("docs/architecture/architecture.json", json.dumps(arch, indent=2))
        for adr in arch.get("adrs") or []:
            if isinstance(adr, dict) and adr.get("id"):
                name = re.sub(r"[^a-zA-Z0-9]+", "-", f"{adr.get('id')}-{adr.get('title', '')}").strip("-").lower()
                self.ws.write(
                    f"docs/architecture/adr/{name}.md",
                    f"# {adr.get('id')}: {adr.get('title')}\n\n**Status:** Accepted\n\n## Context\n{adr.get('context')}\n\n"
                    f"## Decision\n{adr.get('decision')}\n\n## Consequences\n{adr.get('consequences')}\n",
                )
        return arch

    async def review_integration(self) -> dict:
        budget = self.settings.max_context_chars
        files = self.ws.list_files()
        source = [f for f in files if not f.startswith("docs/")]
        sections = [
            self.artifact_section("architecture", "Architecture (binding contracts)", max_chars=budget // 3),
            self.section("Repository", self.ws.tree()),
            self.section("Source code", self.ws.bundle(source, budget=budget // 2)),
        ]
        prompt = f"""Perform an architecture & integration review of the implementation.
Check: components follow the architecture; API contracts match between providers and consumers (paths, payloads,
auth); data model consistency between DB schema and code; configuration/env var names consistent; missing modules,
broken imports or references to files that do not exist; build files referencing correct entry points.
Report only real, actionable issues and assign each to the role that owns the files to change.

JSON shape:
{FINDINGS_SCHEMA}"""
        report = await self.work("architecture_review", prompt, mode="json", sections=sections, allow_questions=False)
        self.ctx.artifacts.publish(self.role, "architecture_review", report)
        from ..render import findings_md

        self.ws.write("docs/architecture/integration-review.md", findings_md("Architecture & Integration Review", report))
        return report
