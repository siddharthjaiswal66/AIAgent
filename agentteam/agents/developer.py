"""Implementation roles (backend, frontend, database, mobile, integration, data, ML)."""

from __future__ import annotations

import json

from .base import Agent


class Developer(Agent):
    async def implement(self, task: dict, related_files: list[str], escalation: int = 0) -> dict:
        budget = self.settings.max_context_chars
        deliverables = task.get("deliverables") or []
        ordered = [*deliverables, *related_files, *self.ws.my_files()]
        sections = [
            self.artifact_section("requirements", "Requirements specification", max_chars=budget // 6),
            self.artifact_section("architecture", "Architecture (binding contracts & standards)", max_chars=budget // 3),
            self.artifact_section("threat_model", "Security requirements", max_chars=budget // 12),
            self.section("Repository files", self.ws.tree()),
            self.section("Existing code relevant to this task", self.ws.bundle(ordered, budget=budget // 2)),
        ]
        prompt = f"""Implement the following task.

Task {task['id']}: {task['title']}
{task.get('description', '')}

Deliverables: {', '.join(deliverables) or '(choose appropriate paths inside your owned paths)'}
Acceptance criteria:
{json.dumps(task.get('acceptance_criteria') or [], indent=1)}

Engineering expectations:
- Follow the architecture's tech stack, directory layout, API contracts, naming and coding standards exactly.
- Integrate with the existing code shown above (correct imports, package names, configuration keys).
- Include input validation, error handling, logging, security controls and unit tests for your code.
- If you change an existing file, output the whole updated file.
- If teammates depend on what you built (endpoints, schemas, env vars, component props), tell them in notes_for_team."""
        meta, written = await self.work(
            "implement_task", prompt, mode="files", sections=sections,
            extra_meta=',\n  "interfaces": "public endpoints/classes/contracts you exposed"',
            complexity=task.get("complexity"), escalation=escalation,
        )
        return {"summary": meta.get("summary", ""), "files": written, "interfaces": meta.get("interfaces", "")}
