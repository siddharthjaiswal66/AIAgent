"""Technical Writer: product, developer, API and operations documentation."""

from __future__ import annotations

from .base import Agent

DOC_SETS = [
    (
        "README & developer docs",
        "README.md (product overview, features, architecture summary with mermaid diagram, tech stack, prerequisites, "
        "quick start, configuration table, running tests, deployment, project structure, links to docs), "
        "docs/developer-guide.md (local setup, codebase tour per component, coding standards, how to add features, "
        "testing, troubleshooting) and CHANGELOG.md (initial release).",
    ),
    (
        "User, API & operations docs",
        "docs/user-guide.md (per user role: workflows step by step mapped to features), docs/api-reference.md "
        "(every endpoint: method, path, auth, request/response examples, error codes) and docs/operations/runbook.md "
        "(deployment, configuration, monitoring, logging, backup/restore, scaling, incident handling, security operations).",
    ),
]


class TechnicalWriter(Agent):
    async def write_docs(self) -> dict:
        budget = self.settings.max_context_chars
        files = self.ws.list_files()
        key_files = [f for f in files if not f.startswith("docs/") and not f.startswith("tests/")]
        written_all: list[str] = []
        summaries: list[str] = []
        for title, scope in DOC_SETS:
            sections = [
                self.artifact_section("requirements", "Requirements", max_chars=budget // 6),
                self.artifact_section("architecture", "Architecture", max_chars=budget // 5),
                self.artifact_section("deployment", "Deployment", max_chars=budget // 20),
                self.artifact_section("qa_report", "Latest QA report", max_chars=budget // 20),
                self.section("Repository", self.ws.tree()),
                self.section("Source code (for accuracy)", self.ws.bundle(key_files, budget=budget // 3)),
            ]
            prompt = (
                f"Write the {title}: {scope}\n"
                "Documentation must be accurate to the actual code and configuration (real paths, commands, env vars, "
                "endpoints). Ask the owning teammate if something is unclear."
            )
            meta, written = await self.work("write_docs", prompt, mode="files", sections=sections)
            written_all += written
            summaries.append(meta.get("summary", ""))
        return {"summary": " ".join(summaries), "files": written_all}
