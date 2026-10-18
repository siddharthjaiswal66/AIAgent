"""Command line entry point.

Examples:
  python main.py "Build an employee leave management system for a 500-person company"
  python main.py -f requirement.txt --provider ollama --model qwen2.5-coder:14b --execute
  python main.py --dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from agentteam.config import Settings
from agentteam.core.events import EventBus
from agentteam.orchestrator import Orchestrator


def console_printer(event: dict) -> None:
    t, agent = event["type"], event.get("agent") or "system"
    stamp = time.strftime("%H:%M:%S", time.localtime(event["ts"]))
    line = None
    if t == "run_started":
        line = f"RUN started -> {event['project_dir']}\nModel routing: {json.dumps(event.get('routing'))}"
        if event.get("tech_preferences"):
            line += f"\nTechnology constraints: {event['tech_preferences']}"
    elif t == "llm_call":
        line = (f"  [{agent}] model {event.get('tier')}:{event.get('model')} for {event.get('task')} "
                f"({event.get('prompt_tokens')}+{event.get('completion_tokens')} tok) - {event.get('reason')}")
    elif t == "phase_started":
        line = f"\n=== PHASE: {event.get('title')} ==="
    elif t == "phase_completed":
        line = f"=== {event['phase']} {event.get('status')} ==="
    elif t == "agent_joined":
        line = f"[team] + {event.get('title')} ({agent}) - {event.get('justification', '')}"
    elif t == "agent_status" and event.get("status") not in ("idle",):
        line = f"[{agent}] {event.get('status')} {event.get('activity', '')}"
    elif t == "task_status":
        line = f"[task {event['task_id']}] {event['status']} ({agent}) {(event.get('summary') or '')[:120]}"
    elif t == "message":
        content = (event.get("content") or "").replace("\n", " ")[:160]
        line = f"  <msg {event.get('kind')}> {event.get('sender')} -> {event.get('recipient')}: {event.get('subject')} | {content}"
    elif t == "file_written":
        line = f"  [{agent}] {event.get('action')} {event['path']}"
    elif t == "ownership_violation":
        line = f"  [{agent}] BLOCKED write to {event['path']} (owner: {event.get('owner')}) -> change request sent"
    elif t == "command_started":
        line = f"  [{agent}] $ {event['command']}"
    elif t == "command_finished":
        line = f"  [{agent}] exit {event['exit_code']} in {event['duration']}s"
    elif t in ("log", "error"):
        line = f"  [{agent}] {'ERROR: ' if t == 'error' else ''}{event.get('message')}"
    elif t == "run_completed":
        line = f"\nRUN COMPLETED -> {event['project_dir']}\nLLM usage: {json.dumps(event.get('usage'))}"
    elif t == "run_failed":
        line = f"\nRUN FAILED: {event.get('error')}"
    if line:
        print(f"{stamp} {line}" if not line.startswith("\n") else line, flush=True)


async def ask_human(question: str) -> str:
    return await asyncio.to_thread(input, f"\n{question}\n(press Enter to accept / let the team decide) > ")


async def run_cli(settings: Settings, requirement: str, project_dir: Path | None) -> int:
    events = EventBus()
    events.subscribe(console_printer)
    orchestrator = Orchestrator(settings, events, human_input=ask_human if settings.interactive else None)
    try:
        result = await orchestrator.run(requirement, project_dir)
    except Exception as exc:  # already reported through events
        print(f"Delivery failed: {exc}", file=sys.stderr)
        return 1
    print("\nSummary:")
    for key in ("project_dir", "qa_status", "tasks_total", "tasks_done", "files_delivered", "agent_messages", "duration_minutes"):
        print(f"  {key}: {result.get(key)}")
    usage = result.get("usage") or {}
    print(f"  llm_calls: {usage.get('calls')}  tokens: {usage.get('total_tokens')}")
    print(f"  calls by tier: {json.dumps({k: v['calls'] for k, v in (usage.get('by_tier') or {}).items()})}")
    if usage.get("est_cost_usd") is not None:
        print(f"  est. cost: ${usage['est_cost_usd']:.4f}  (all-large-model: ${usage['est_cost_if_all_large_usd']:.4f}, "
              f"saved ~{usage.get('est_savings_pct')}%)")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Autonomous multi-agent software delivery team")
    parser.add_argument("requirement", nargs="?", help="Business requirement text")
    parser.add_argument("-f", "--file", help="Read the requirement from a file")
    parser.add_argument("--provider", help="openai | azure | anthropic | ollama | mock")
    parser.add_argument("--model", help="Model / deployment name")
    parser.add_argument("--output", help="Base output directory (default: ./output)")
    parser.add_argument("--project-dir", help="Exact directory for the generated project")
    parser.add_argument("--execute", action="store_true", help="Allow agents to run build/test commands")
    parser.add_argument("--deploy", action="store_true", help="Allow DevOps to run deployment commands (implies --execute)")
    parser.add_argument("--interactive", action="store_true",
                        help="Analyst asks you clarifying questions and you approve the architecture/tech stack")
    parser.add_argument("--tech", help="Mandatory technology constraints, e.g. \"Java 21 Spring Boot, Angular, PostgreSQL, Azure\". "
                                       "Omit to let the architect decide.")
    parser.add_argument("--routing", choices=["auto", "fixed"], help="auto = smart per-task model selection; fixed = one model")
    parser.add_argument("--token-budget", type=int, help="Total token budget; routing downgrades tiers as it is consumed")
    parser.add_argument("--max-qa-iterations", type=int)
    parser.add_argument("--dashboard", action="store_true", help="Start the web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    requirement = args.requirement
    if args.file:
        requirement = Path(args.file).read_text(encoding="utf-8")

    settings = Settings.from_env().with_overrides(
        provider=args.provider.lower() if args.provider else None,
        model=args.model,
        output_dir=args.output,
        allow_execution=True if (args.execute or args.deploy) else None,
        allow_deploy=True if args.deploy else None,
        interactive=True if args.interactive else None,
        max_qa_iterations=args.max_qa_iterations,
        tech_preferences=args.tech,
        routing=args.routing,
        token_budget=args.token_budget,
    )

    if args.dashboard:
        from agentteam.dashboard.server import serve

        serve(settings, host=args.host, port=args.port, initial_requirement=requirement)
        return 0

    if not requirement or not requirement.strip():
        parser.error("Provide a requirement (text argument or --file), or use --dashboard")
    project_dir = Path(args.project_dir) if args.project_dir else None
    return asyncio.run(run_cli(settings, requirement, project_dir))


if __name__ == "__main__":
    sys.exit(main())
