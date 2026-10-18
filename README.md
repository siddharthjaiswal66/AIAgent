# AgentTeam: an autonomous software delivery team

You give it a business requirement. A team of AI agents analyses it, decides which roles to staff, designs the architecture, plans the work, writes the code, reviews and tests it, adds deployment files, writes documentation and produces a delivery report.

## The team

| Role | When | Owns (write access) | Responsibilities |
|---|---|---|---|
| Business Analyst | always | `docs/requirements/` | Requirement analysis, user stories, acceptance criteria, NFRs, risks |
| Project Manager | always | `docs/plan/` | Staffs the team, writes the work breakdown and dependencies, delivery report |
| Solution Architect | always | `docs/architecture/` | Tech stack, components, API contracts, data model, ADRs, code ownership, integration review |
| Backend / Frontend / Database / Mobile / Integration / Data / ML engineers | as required | component dirs assigned by the architect | Implementation and unit tests |
| Security Engineer | as required | `docs/security/`, `security/` | STRIDE threat model, secure code review |
| QA / Test Engineer | always | `tests/`, `docs/qa/` | Test plan, automated tests, test execution, defect reports, sign-off |
| DevOps Engineer | always | `deploy/`, `infra/`, `.github/`, `docker-compose.yml`, Dockerfiles | Containers, CI/CD, Kubernetes, verification and deployment |
| Technical Writer | always | `docs/`, `README.md`, `CHANGELOG.md` | README, user guide, developer guide, API reference, runbook |

## How agents stay separate but still communicate

- **Separate work.** The `ProjectWorkspace` enforces path ownership. Agents can read any file, but they can only write to paths they own (the longest matching prefix wins). If an agent tries to write somewhere else, the write is blocked and turned into a change request to the owner.
- **Communication.** Agents never call each other directly. Everything goes through the `MessageBus`:
  - `question` / `answer`: an agent that is blocked asks the role that owns the information, for example a developer asking the architect about an API contract.
  - `notification`: notes such as "endpoint X is ready", or the security requirements sent to each owner.
  - `change_request`: a request to change a file the sender doesn't own.
  - `bug_report`: QA, security and architecture findings, routed to whoever owns the affected files.
  - `handoff`: a message confirming that a fix is done.
- **Shared artifacts.** Requirements, architecture, plan, threat model, QA report and deployment report are published to an `ArtifactStore`. Each artifact has one owner, and everyone else can only read it.

## Delivery flow

```
Requirement -> Analysis -> Team formation -> Architecture (+ threat model) -> Planning
 -> Development (parallel, dependency-ordered) -> Integration review -> Security review
 -> QA loop (write tests -> run -> evaluate -> route defects -> fix, up to N iterations)
 -> Build & deploy -> Documentation -> Delivery report
```

## Who decides the technology?

By default the **Solution Architect agent** chooses the whole stack. For each major layer it compares at least two options against the NFRs, the staffed roles, ecosystem maturity, licence cost, operability and the hiring market, and records each decision as an ADR that lists the alternatives it considered.

You can constrain the stack in any of these ways. Constraints are mandatory; the architect decides everything you don't constrain.
- Name the technology in the requirement text, for example "...built with .NET 8 on Azure". The analyst records it under `technology_constraints`.
- Pass `--tech "Java 21 Spring Boot, Angular, PostgreSQL"`, set `TECH_PREFERENCES` in `.env`, or use the field in the dashboard.
- Use `--interactive`. The architect then shows you its proposed stack, and you approve it or describe the changes you want (up to 3 revisions).

## Smart model routing (token efficiency)

With `LLM_ROUTING=auto` (the default) and `LLM_MODEL` left empty, each LLM call goes to a **small**, **medium** or **large** model:

| Signal | Effect |
|---|---|
| Task type | Requirements analysis and architecture use **large**. Team selection, Q&A, docs and reports use **small**. Coding, tests, reviews and DevOps use **medium**. |
| Project complexity (set by the analyst) | Small projects move adaptive tasks one tier down; large projects move them one tier up. |
| Task complexity (set by the PM for each task) | `low` tasks such as scaffolding or CRUD go one tier down; `high` tasks such as auth, payments or complex integrations go one tier up. |
| Defect severity | Critical findings are fixed on a stronger tier; low-severity findings on a cheaper one. |
| Feedback | A tier is escalated when the reply is unusable, when a task fails, or when defects survive a QA round. |
| `TOKEN_BUDGET` | At 80% of the budget, calls are capped at medium; at 95%, at small. |
| Model failure | The call falls back to another tier automatically. |

The complexity signals come from the agents' existing outputs, so routing makes no extra LLM calls.

The tiers can use different providers, for example a free local small tier:
```
LLM_PROVIDER=anthropic
LLM_TIER_SMALL=ollama:qwen2.5-coder:7b
```

Every routing decision and its reason are printed in the CLI and shown in the dashboard's **AI models** tab. The run summary reports tokens and calls per tier, plus an estimated cost compared with sending every call to the large tier. The estimate uses approximate list prices, which you can override with `LLM_PRICES`.

To use one model for everything, set `LLM_ROUTING=fixed` or set `LLM_MODEL`. To pin a model for one role, set for example `LLM_MODEL_SOLUTION_ARCHITECT=anthropic:claude-opus-4-20250514`.

## Setup

```powershell
cd c:\Trados\AIAgent
python -m venv .venv            # already created
.\.venv\Scripts\pip install -r requirements.txt
copy env.example .env           # then set LLM_PROVIDER and your API key
```

LLM providers:
- `openai`: also works with any OpenAI-compatible endpoint (Groq, OpenRouter, LM Studio, vLLM) via `OPENAI_BASE_URL`.
- `azure`: Azure OpenAI.
- `anthropic`: Anthropic Claude.
- `ollama`: free local models.
- `mock`: offline demo with no API key. It exercises the whole workflow but doesn't produce a real application.

You can use a different model for each role, for example `LLM_MODEL_SOLUTION_ARCHITECT=...`. See [Smart model routing](#smart-model-routing-token-efficiency).

## Usage

```powershell
# CLI
.\.venv\Scripts\python main.py "Build an employee leave management system with approval workflow, HR reports and email notifications"
.\.venv\Scripts\python main.py -f requirement.txt --provider anthropic --execute
.\.venv\Scripts\python main.py "..." --interactive          # answer the analyst's questions, approve the tech stack
.\.venv\Scripts\python main.py "..." --tech ".NET 8, React, SQL Server, Azure" --token-budget 2000000

# Web dashboard (team, task board, live agent conversation, file browser)
.\.venv\Scripts\python main.py --dashboard --port 8000      # open http://127.0.0.1:8000

# Offline demo
.\.venv\Scripts\python main.py "Build a task manager" --provider mock --execute
```

Flags:
- `--execute`: lets QA and DevOps run build and test commands. This needs the generated stack's toolchain (for example JDK, Node or Docker) on your PATH.
- `--deploy`: also runs deployment commands, for example `docker compose up -d`.

## Output

Each run creates `output/<timestamp>-<name>/` containing:
- the generated application source code
- `docs/`: requirements, architecture and ADRs, plan, threat model, security review, QA test plan and reports, user, developer and API guides, runbook and delivery report
- deployment files: Dockerfiles, `docker-compose.yml`, CI workflow, `deploy/` manifests and `deploy/DEPLOYMENT_REPORT.md`
- `.agentteam/`: `events.jsonl` (full audit trail), `messages.jsonl` (every message between agents), `artifacts/` and `result.json`

## Project structure

```
agentteam/
  config.py              settings from .env
  llm/                   provider clients (OpenAI, Azure, Anthropic, Ollama) + mock
    router.py            smart model routing, escalation, budget, cost tracking
  core/
    messages.py          MessageBus: questions, notifications, change requests, bug reports
    workspace.py         ProjectWorkspace: enforces file ownership
    artifacts.py         ArtifactStore: shared documents, each with a single owner
    events.py            EventBus + RunState for the console and dashboard
    runner.py            runs build/test/deploy commands
  agents/                one class per role + role catalog
  orchestrator.py        SDLC phases, scheduling, routing of findings to owners
  dashboard/             FastAPI + WebSocket live dashboard
main.py                  CLI
```

## Tips for good results

- Use a strong model with a large context window. Raise `MAX_CONTEXT_CHARS` (for example to 150000) and `LLM_MAX_TOKENS` (for example to 16000) if your model supports it.
- Write a more detailed requirement (users, workflows, integrations, scale, compliance) to get a more accurate specification.
- Use `--execute` so test results come from real runs, not only from static review.
