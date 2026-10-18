"""DevOps Engineer: containerisation, CI/CD, IaC, verification and deployment."""

from __future__ import annotations

import json

from .base import Agent


class DevOpsEngineer(Agent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meta: dict = {}

    async def build_delivery_pipeline(self) -> dict:
        budget = self.settings.max_context_chars
        files = self.ws.list_files()
        build_files = [f for f in files if f.rsplit("/", 1)[-1].lower() in {
            "pom.xml", "build.gradle", "build.gradle.kts", "package.json", "requirements.txt", "pyproject.toml",
            "go.mod", "cargo.toml", "settings.gradle", "vite.config.ts", "vite.config.js", "angular.json", "next.config.js",
            "application.yml", "application.properties", "alembic.ini", ".csproj"} or f.endswith(".csproj")]
        sections = [
            self.artifact_section("architecture", "Architecture", max_chars=budget // 3),
            self.artifact_section("requirements", "Requirements (NFRs matter most)", max_chars=budget // 8),
            self.section("Repository", self.ws.tree()),
            self.section("Build & configuration files", self.ws.bundle(build_files, budget=budget // 3)),
        ]
        prompt = """Make the system deployable and operable:
- Production-grade multi-stage Dockerfiles for every deployable component (non-root user, healthchecks, small images).
- docker-compose.yml that runs the full stack locally (including database/broker) with healthchecks and env vars.
- CI/CD pipeline (GitHub Actions) that builds, runs all tests, scans, builds images and deploys.
- Kubernetes manifests (or Helm chart) under deploy/ with config maps, secrets references, probes, resources, HPA.
- .env.example with every environment variable from the architecture; scripts for local start/stop.
- Provide commands (from repo root) to verify the build ("verify_commands", e.g. 'docker compose config' and
  'docker compose build') and to deploy locally ("deploy_commands", e.g. 'docker compose up -d --build'),
  plus "access_urls" where the running system can be reached."""
        meta, written = await self.work(
            "build_deployment", prompt, mode="files", sections=sections,
            extra_meta=',\n  "verify_commands": ["..."],\n  "deploy_commands": ["..."],\n  "access_urls": ["..."]',
        )
        self.meta = meta
        return {"summary": meta.get("summary", ""), "files": written}

    async def verify_and_deploy(self, deploy: bool) -> dict:
        report = {"verified": None, "deployed": None, "runs": [], "access_urls": self.meta.get("access_urls") or []}
        runner = self.ctx.runner
        if not runner.enabled:
            report["note"] = "Execution disabled - deployment artifacts generated but not executed."
        else:
            for attempt in range(2):
                results = [await runner.run(c, self.role) for c in (self.meta.get("verify_commands") or [])[:5]]
                report["runs"] = [r.summary(3000) for r in results]
                report["verified"] = all(r.ok for r in results)
                if report["verified"] or attempt == 1:
                    break
                failures = [{"id": f"BUILD-{i}", "severity": "high", "title": f"Command failed: {r.command}",
                             "description": r.summary(4000), "files": []} for i, r in enumerate(results) if not r.ok]
                await self.fix_issues(failures, reporter=self.role, label="build failures")
            if deploy and report["verified"]:
                results = [await runner.run(c, self.role) for c in (self.meta.get("deploy_commands") or [])[:5]]
                report["runs"] += [r.summary(3000) for r in results]
                report["deployed"] = all(r.ok for r in results)

        lines = ["# Deployment Report", "", self.meta.get("summary", ""), "",
                 f"- Build verified: {report['verified']}", f"- Deployed: {report['deployed']}",
                 f"- Access URLs: {', '.join(report['access_urls']) or 'n/a'}", "",
                 "## Verify commands", *[f"- `{c}`" for c in self.meta.get("verify_commands") or []],
                 "", "## Deploy commands", *[f"- `{c}`" for c in self.meta.get("deploy_commands") or []]]
        if report.get("note"):
            lines += ["", f"> {report['note']}"]
        if report["runs"]:
            lines += ["", "## Execution log", *[f"```\n{r}\n```" for r in report["runs"]]]
        self.ws.write("deploy/DEPLOYMENT_REPORT.md", "\n".join(lines) + "\n")
        self.ctx.artifacts.publish(self.role, "deployment", {**report, "verify_commands": self.meta.get("verify_commands"),
                                                             "deploy_commands": self.meta.get("deploy_commands")})
        failed = [r for r in report["runs"] if "exit code: 0" not in r]
        if failed:
            self.log("Some deployment commands failed:\n" + json.dumps(failed)[:2000])
        return report
