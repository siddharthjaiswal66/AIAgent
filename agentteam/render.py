"""Deterministic Markdown rendering of structured artifacts."""

from __future__ import annotations


def _list(items, fmt=lambda x: str(x)) -> str:
    return "\n".join(f"- {fmt(i)}" for i in (items or [])) or "- (none)"


def requirements_md(r: dict) -> str:
    out = [f"# {r.get('title') or r.get('project_name', 'Project')} - Requirements Specification", "",
           "## Summary", r.get("summary", ""), "",
           "## Business Goals", _list(r.get("business_goals")), "",
           "## Stakeholders", _list(r.get("stakeholders"), lambda s: f"**{s.get('name')}** - {s.get('interest', '')}" if isinstance(s, dict) else s), "",
           "## User Roles"]
    for u in r.get("user_roles") or []:
        if isinstance(u, dict):
            out.append(f"- **{u.get('name')}**: {u.get('description', '')} (permissions: {', '.join(u.get('permissions') or [])})")
    out += ["", "## Functional Requirements"]
    for fr in r.get("functional_requirements") or []:
        if not isinstance(fr, dict):
            continue
        out += [f"### {fr.get('id')} - {fr.get('title')} ({fr.get('priority', '')})", fr.get("description", ""), ""]
        if fr.get("user_stories"):
            out += ["**User stories**", _list(fr["user_stories"]), ""]
        if fr.get("acceptance_criteria"):
            out += ["**Acceptance criteria**", _list(fr["acceptance_criteria"]), ""]
    out += ["## Non-Functional Requirements", "| ID | Category | Requirement | Metric |", "|---|---|---|---|"]
    for n in r.get("non_functional_requirements") or []:
        if isinstance(n, dict):
            out.append(f"| {n.get('id')} | {n.get('category')} | {n.get('requirement')} | {n.get('metric', '')} |")
    out += ["", "## Domain Entities", _list(r.get("domain_entities"), lambda e: f"**{e.get('name')}** - {e.get('description', '')} ({', '.join(e.get('key_attributes') or [])})" if isinstance(e, dict) else e),
            "", "## Integrations", _list(r.get("integrations"), lambda i: f"{i.get('system')}: {i.get('purpose')} via {i.get('protocol', '')}" if isinstance(i, dict) else i),
            "", "## Technology Constraints (customer-mandated)", _list(r.get("technology_constraints")),
            "", "## Constraints", _list(r.get("constraints")),
            "", "## Assumptions", _list(r.get("assumptions")),
            "", "## Open Questions", _list(r.get("open_questions")),
            "", "## Risks", _list(r.get("risks"), lambda k: f"{k.get('risk')} (impact: {k.get('impact')}) - mitigation: {k.get('mitigation')}" if isinstance(k, dict) else k),
            "", "## Out of Scope", _list(r.get("out_of_scope")), ""]
    return "\n".join(out)


def team_md(team: dict) -> str:
    out = ["# Delivery Team", "", "| Role | Title | Why |", "|---|---|---|"]
    for m in team.get("members", []):
        out.append(f"| {m['role']} | {m.get('title', '')} | {m.get('justification', '')} |")
    if team.get("excluded"):
        out += ["", "## Roles not staffed", _list(team["excluded"], lambda e: f"{e.get('role')}: {e.get('reason')}" if isinstance(e, dict) else e)]
    return "\n".join(out) + "\n"


def architecture_md(a: dict) -> str:
    out = [f"# Architecture - {a.get('project_name', '')}", "", "## Overview", a.get("overview", ""), "",
           f"**Architecture style:** {a.get('architecture_style', '')}", "", "## Technology Stack"]
    for k, v in (a.get("tech_stack") or {}).items():
        out.append(f"- **{k}**: {v if not isinstance(v, list) else ', '.join(map(str, v))}")
    diagrams = a.get("diagrams") or {}
    for name, mermaid in diagrams.items():
        if mermaid:
            out += ["", f"## {name.replace('_', ' ').title()} Diagram", "```mermaid", str(mermaid).strip(), "```"]
    out += ["", "## Components", "| Component | Type | Owner | Directory | Responsibility |", "|---|---|---|---|---|"]
    for c in a.get("components") or []:
        if isinstance(c, dict):
            out.append(f"| {c.get('name')} | {c.get('type', '')} | {c.get('owner_role', '')} | `{c.get('directory', '')}` | {c.get('responsibility', '')} |")
    out += ["", "## Data Model"]
    for e in a.get("data_model") or []:
        if not isinstance(e, dict):
            continue
        out += [f"### {e.get('entity')}", "| Field | Type | Constraints |", "|---|---|---|"]
        for f in e.get("fields") or []:
            if isinstance(f, dict):
                out.append(f"| {f.get('name')} | {f.get('type')} | {f.get('constraints', '')} |")
        if e.get("relationships"):
            out += ["", "Relationships:", _list(e["relationships"])]
        out.append("")
    out += ["## API Contracts", "| Method | Path | Auth | Description |", "|---|---|---|---|"]
    for api in a.get("api_contracts") or []:
        if isinstance(api, dict):
            out.append(f"| {api.get('method')} | `{api.get('path')}` | {api.get('auth', '')} | {api.get('description', '')} |")
    out += ["", "## Directory Layout", "```", str(a.get("directory_layout", "")).strip(), "```", "", "## Code Ownership",
            "| Path | Owner |", "|---|---|"]
    for p, r in (a.get("ownership") or {}).items():
        out.append(f"| `{p}` | {r} |")
    out += ["", "## Cross-Cutting Concerns"]
    for k, v in (a.get("cross_cutting") or {}).items():
        out.append(f"- **{k}**: {v}")
    out += ["", "## Coding Standards", _list(a.get("coding_standards")),
            "", "## Commands", f"- Build: {a.get('build_commands')}", f"- Test: {a.get('test_commands')}", f"- Run: {a.get('run_commands')}",
            "", "## Architecture Decision Records"]
    for adr in a.get("adrs") or []:
        if isinstance(adr, dict):
            out += [f"### {adr.get('id')}: {adr.get('title')}", f"**Context:** {adr.get('context')}",
                    f"**Decision:** {adr.get('decision')}", f"**Consequences:** {adr.get('consequences')}", ""]
    return "\n".join(out) + "\n"


def plan_md(plan: dict) -> str:
    out = ["# Delivery Plan", "", "## Milestones"]
    for m in plan.get("milestones") or []:
        if isinstance(m, dict):
            out.append(f"- **{m.get('id')} {m.get('name')}**: {m.get('goal', '')}")
    out += ["", "## Tasks", "| ID | Task | Assignee | Depends on | Deliverables |", "|---|---|---|---|---|"]
    for t in plan.get("tasks") or []:
        out.append(f"| {t['id']} | {t['title']} | {t['assignee']} | {', '.join(t.get('depends_on') or []) or '-'} | {', '.join(t.get('deliverables') or [])} |")
    out += ["", "## Task Details"]
    for t in plan.get("tasks") or []:
        out += [f"### {t['id']} - {t['title']}", t.get("description", ""), "", "Acceptance criteria:", _list(t.get("acceptance_criteria")), ""]
    if plan.get("risks"):
        out += ["## Delivery Risks", _list(plan["risks"], lambda r: r if isinstance(r, str) else f"{r.get('risk')} - {r.get('mitigation', '')}")]
    return "\n".join(out) + "\n"


def findings_md(title: str, report: dict) -> str:
    out = [f"# {title}", "", f"**Status:** {report.get('status', 'n/a')}", ""]
    if report.get("quality_score") is not None:
        out.append(f"**Quality score:** {report.get('quality_score')}/100\n")
    out += ["## Summary", report.get("summary", ""), ""]
    if report.get("requirements_coverage"):
        out += ["## Requirements Coverage", "| Requirement | Status | Evidence |", "|---|---|---|"]
        for c in report["requirements_coverage"]:
            if isinstance(c, dict):
                out.append(f"| {c.get('requirement_id')} | {c.get('status')} | {c.get('evidence', '')} |")
        out.append("")
    items = report.get("defects") or report.get("findings") or []
    out += ["## Findings", "| ID | Severity | Owner | Title | Files |", "|---|---|---|---|---|"]
    for d in items:
        if isinstance(d, dict):
            out.append(f"| {d.get('id')} | {d.get('severity')} | {d.get('owner_role')} | {d.get('title')} | {', '.join(d.get('files') or [])} |")
    out.append("")
    for d in items:
        if isinstance(d, dict):
            out += [f"### {d.get('id')} - {d.get('title')}", d.get("description", ""), "",
                    f"**Fix:** {d.get('suggested_fix') or d.get('remediation', '')}", ""]
    if report.get("test_runs"):
        out += ["## Test Execution", *[f"```\n{r}\n```" for r in report["test_runs"]]]
    return "\n".join(out) + "\n"
