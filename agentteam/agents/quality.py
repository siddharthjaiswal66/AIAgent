"""Quality roles: QA / Test Engineer and Security Engineer."""

from __future__ import annotations

import json

from ..core.runner import CommandResult
from ..render import findings_md
from .base import Agent

QA_REPORT_SCHEMA = """{
  "status": "pass|fail",
  "summary": "...",
  "quality_score": 0,
  "requirements_coverage": [{"requirement_id": "FR-001", "status": "covered|partial|missing", "evidence": "files/tests that prove it"}],
  "defects": [{"id": "BUG-001", "severity": "critical|high|medium|low", "owner_role": "<role owning the files to fix>",
               "files": ["path"], "title": "...", "description": "steps/evidence", "suggested_fix": "..."}]
}"""

THREAT_MODEL_SCHEMA = """{
  "summary": "...",
  "assets": ["..."],
  "trust_boundaries": ["..."],
  "threats": [{"id": "TM-001", "stride": "Spoofing|Tampering|Repudiation|Information disclosure|Denial of service|Elevation of privilege",
               "description": "...", "affected_components": ["..."], "mitigation": "...", "owner_role": "<role>"}],
  "security_requirements": [{"id": "SEC-001", "requirement": "...", "owner_role": "<role>"}]
}"""

SECURITY_REVIEW_SCHEMA = """{
  "status": "pass|fail",
  "summary": "...",
  "findings": [{"id": "SEC-F-001", "severity": "critical|high|medium|low", "category": "OWASP category / CWE",
                "owner_role": "<role owning the files to fix>", "files": ["path"], "title": "...",
                "description": "...", "suggested_fix": "..."}]
}"""


def _source_files(files: list[str]) -> list[str]:
    return [f for f in files if not f.startswith("docs/")]


class QAEngineer(Agent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_commands: list[str] = []

    async def write_tests(self) -> dict:
        budget = self.settings.max_context_chars
        files = self.ws.list_files()
        sections = [
            self.artifact_section("requirements", "Requirements (acceptance criteria)", max_chars=budget // 4),
            self.artifact_section("architecture", "Architecture", max_chars=budget // 4),
            self.section("Repository", self.ws.tree()),
            self.section("Source code", self.ws.bundle(_source_files(files), budget=budget // 2)),
        ]
        prompt = """Create the test strategy and the automated test suite for the delivered system.
- Write docs/qa/test-plan.md (strategy, scope, test levels, environments, entry/exit criteria, traceability matrix FR -> tests).
- Write automated integration / API / end-to-end tests inside your owned test paths, using the stack's standard test
  frameworks, mapped to the acceptance criteria. Tests must be runnable without external paid services
  (use in-memory/embedded DBs, test containers or mocks as appropriate).
- Provide the exact commands (run from the repo root) to execute your tests in "test_commands"."""
        meta, written = await self.work(
            "write_tests", prompt, mode="files", sections=sections,
            extra_meta=',\n  "test_commands": ["command run from repo root"]',
        )
        self.test_commands = [c for c in (meta.get("test_commands") or []) if isinstance(c, str)]
        return {"summary": meta.get("summary", ""), "files": written}

    async def run_tests(self) -> list[CommandResult]:
        if not self.ctx.runner.enabled:
            return []
        arch = self.ctx.artifacts.get("architecture") or {}
        commands: list[str] = []
        for c in [*(arch.get("build_commands") or []), *(arch.get("test_commands") or []), *self.test_commands]:
            if isinstance(c, str) and c.strip() and c not in commands:
                commands.append(c)
        self.status("testing", "running test suites")
        results = []
        for cmd in commands[:10]:
            results.append(await self.ctx.runner.run(cmd, self.role))
        self.status("idle")
        return results

    async def evaluate(self, iteration: int, results: list[CommandResult]) -> dict:
        budget = self.settings.max_context_chars
        files = self.ws.list_files()
        if results:
            execution = "\n\n".join(r.summary(limit=budget // (6 * max(1, len(results)))) for r in results)
        else:
            execution = ("Automated execution is disabled in this environment. Perform a rigorous static verification: "
                         "trace each acceptance criterion through the code and tests, and look for defects.")
        sections = [
            self.artifact_section("requirements", "Requirements (acceptance criteria)", max_chars=budget // 5),
            self.artifact_section("architecture", "Architecture", max_chars=budget // 6),
            self.section("Test execution results", execution),
            self.section("Repository", self.ws.tree()),
            self.section("Source code & tests", self.ws.bundle(_source_files(files), budget=budget // 2)),
        ]
        prompt = f"""QA iteration {iteration}: evaluate product quality and decide whether it can be released.
- Analyse test results (failures, compilation errors, stack traces) and identify root causes.
- Check every functional requirement's acceptance criteria against the implementation.
- Report each defect once, assigned to the role owning the files that must change (use the ownership shown in the
  repository listing). If a test itself is wrong, assign it to qa_engineer.
- status is "pass" only if there are no critical/high defects and all tests that ran succeeded.

JSON shape:
{QA_REPORT_SCHEMA}"""
        report = await self.work(f"evaluate_quality", prompt, mode="json", sections=sections, allow_questions=False)
        report.setdefault("defects", [])
        report["iteration"] = iteration
        report["executed"] = bool(results)
        report["test_runs"] = [r.summary(limit=3000) for r in results]
        if results and any(not r.ok for r in results) and report.get("status") == "pass":
            report["status"] = "fail"
        self.ctx.artifacts.publish(self.role, "qa_report", report)
        self.ws.write(f"docs/qa/test-report-iteration-{iteration}.md", findings_md(f"Test Report - Iteration {iteration}", report))
        return report


class SecurityEngineer(Agent):
    async def threat_model(self) -> dict:
        sections = [
            self.artifact_section("requirements", "Requirements"),
            self.artifact_section("architecture", "Architecture"),
        ]
        prompt = f"""Produce a STRIDE threat model for the architecture and derive concrete security requirements
that developers must implement (assign each to the owning role).

JSON shape:
{THREAT_MODEL_SCHEMA}"""
        tm = await self.work("threat_model", prompt, mode="json", sections=sections, allow_questions=True)
        self.ctx.artifacts.publish(self.role, "threat_model", tm)
        lines = ["# Threat Model", "", tm.get("summary", ""), "", "## Assets", *[f"- {a}" for a in tm.get("assets") or []],
                 "", "## Trust Boundaries", *[f"- {b}" for b in tm.get("trust_boundaries") or []],
                 "", "## Threats", "| ID | STRIDE | Description | Mitigation | Owner |", "|---|---|---|---|---|"]
        for t in tm.get("threats") or []:
            if isinstance(t, dict):
                lines.append(f"| {t.get('id')} | {t.get('stride')} | {t.get('description')} | {t.get('mitigation')} | {t.get('owner_role')} |")
        lines += ["", "## Security Requirements"]
        for s in tm.get("security_requirements") or []:
            if isinstance(s, dict):
                lines.append(f"- **{s.get('id')}** ({s.get('owner_role')}): {s.get('requirement')}")
        self.ws.write("docs/security/threat-model.md", "\n".join(lines) + "\n")
        for role in {s.get("owner_role") for s in tm.get("security_requirements") or [] if isinstance(s, dict)}:
            if role and self.ctx.bus.is_member(role):
                reqs = [s for s in tm["security_requirements"] if isinstance(s, dict) and s.get("owner_role") == role]
                self.notify(role, "Security requirements for your components", json.dumps(reqs, indent=1))
        return tm

    async def review(self) -> dict:
        budget = self.settings.max_context_chars
        files = self.ws.list_files()
        sections = [
            self.artifact_section("threat_model", "Threat model", max_chars=budget // 6),
            self.artifact_section("architecture", "Architecture", max_chars=budget // 6),
            self.section("Repository", self.ws.tree()),
            self.section("Source code", self.ws.bundle(_source_files(files), budget=budget // 2)),
        ]
        prompt = f"""Perform a secure code review (OWASP Top 10 / ASVS): injection, broken auth/access control,
sensitive data exposure, hard-coded secrets, insecure configuration, missing input validation, vulnerable patterns,
missing security headers, logging of secrets, insecure dependencies. Verify the threat-model mitigations exist.
Assign each finding to the role owning the files.

JSON shape:
{SECURITY_REVIEW_SCHEMA}"""
        report = await self.work("security_review", prompt, mode="json", sections=sections, allow_questions=False)
        report.setdefault("findings", [])
        self.ctx.artifacts.publish(self.role, "security_review", report)
        self.ws.write("docs/security/security-review.md", findings_md("Security Review", report))
        return report
