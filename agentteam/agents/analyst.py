"""Business Analyst: deep requirement analysis."""

from __future__ import annotations

import json
import re

from ..render import requirements_md
from .base import Agent

REQUIREMENTS_SCHEMA = """{
  "project_name": "short-kebab-case-name",
  "title": "Human readable product name",
  "summary": "2-4 paragraph description of the product and its value",
  "business_goals": ["..."],
  "stakeholders": [{"name": "...", "interest": "..."}],
  "user_roles": [{"name": "...", "description": "...", "permissions": ["..."]}],
  "functional_requirements": [{
    "id": "FR-001", "title": "...", "description": "...", "priority": "must|should|could",
    "user_stories": ["As a <role> I want <capability> so that <benefit>"],
    "acceptance_criteria": ["Given ... When ... Then ..."]
  }],
  "non_functional_requirements": [{"id": "NFR-001", "category": "performance|security|availability|scalability|usability|compliance|maintainability|observability|accessibility", "requirement": "...", "metric": "measurable target"}],
  "domain_entities": [{"name": "...", "description": "...", "key_attributes": ["..."]}],
  "integrations": [{"system": "...", "purpose": "...", "protocol": "..."}],
  "application_types": ["web_frontend | rest_api | mobile_app | batch_jobs | data_pipeline | ml_feature | messaging | cli"],
  "technology_constraints": ["ONLY technology/platform/hosting constraints the customer explicitly stated, e.g. 'Backend must be Java 21 / Spring Boot', 'Deploy on Azure'. Empty list if none."],
  "constraints": ["..."],
  "assumptions": ["..."],
  "open_questions": ["..."],
  "risks": [{"risk": "...", "impact": "high|medium|low", "mitigation": "..."}],
  "out_of_scope": ["..."],
  "complexity": "small|medium|large"
}"""


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower()
    return value[:50] or "application"


class BusinessAnalyst(Agent):
    async def analyze(self, requirement: str) -> dict:
        prefs = self.settings.tech_preferences.strip()
        prefs_text = (
            f"\nCustomer technology preferences (mandatory - record each one in technology_constraints):\n{prefs}\n"
            if prefs else ""
        )
        prompt = f"""Analyse this business requirement in depth and produce the requirements specification.

Requirement from the customer:
\"\"\"
{requirement.strip()}
\"\"\"
{prefs_text}
Guidance:
- Identify explicit AND implicit needs an enterprise product requires (authentication, role-based authorisation,
  audit trail, validation, admin capabilities, search/pagination, notifications, reporting, error handling, i18n if relevant).
- Every functional requirement must have testable Given/When/Then acceptance criteria.
- Non-functional requirements must have measurable metrics.
- Keep the scope realistic for a first production release (MVP+), and list the rest under out_of_scope.
- Record assumptions for anything ambiguous instead of leaving gaps.
- technology_constraints: capture only technologies the customer explicitly demanded. Do NOT choose technologies
  yourself - the Solution Architect decides everything that is not constrained.
- complexity: small = a few entities/screens, no integrations; medium = typical line-of-business app;
  large = many modules, integrations, strict NFRs or compliance.

JSON shape:
{REQUIREMENTS_SCHEMA}"""
        req = await self.work("analyze_requirements", prompt, mode="json", allow_questions=False)

        if self.settings.interactive and self.ctx.human_input and req.get("open_questions"):
            answers = []
            for q in req["open_questions"][:8]:
                ans = (await self.ctx.human_input(f"[Business Analyst asks] {q}")).strip()
                if ans:
                    answers.append(f"Q: {q}\nA: {ans}")
            if answers:
                refine = (
                    f"{prompt}\n\n## Your previous specification\n{json.dumps(req, indent=1)}\n\n"
                    "## Answers from the customer\n" + "\n\n".join(answers) +
                    "\n\nUpdate the specification accordingly (same JSON shape). Remove answered open questions."
                )
                req = await self.work("refine_requirements", refine, mode="json", allow_questions=False)

        req["project_name"] = _slug(req.get("project_name") or req.get("title", ""))
        for key in ("functional_requirements", "non_functional_requirements", "user_roles", "domain_entities",
                    "integrations", "constraints", "assumptions", "open_questions", "risks", "out_of_scope", "business_goals",
                    "technology_constraints"):
            if not isinstance(req.get(key), list):
                req[key] = []
        if prefs and not req["technology_constraints"]:
            req["technology_constraints"] = [prefs]
        if str(req.get("complexity", "")).lower() not in {"small", "medium", "large"}:
            req["complexity"] = "medium"
        req["original_requirement"] = requirement.strip()

        self.ctx.artifacts.publish(self.role, "requirements", req)
        self.ws.write("docs/requirements/requirements.md", requirements_md(req))
        self.ws.write("docs/requirements/requirements.json", json.dumps(req, indent=2))
        return req
