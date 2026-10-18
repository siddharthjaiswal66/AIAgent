"""FastAPI web dashboard: submit requirements and watch the team work in real time."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from ..config import Settings
from ..core.events import EventBus, RunState
from ..orchestrator import Orchestrator

STATIC = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    requirement: str
    provider: str | None = None
    model: str | None = None
    execute: bool = False
    deploy: bool = False
    tech: str | None = None
    routing: str | None = None
    token_budget: int | None = None


def create_app(settings: Settings, initial_requirement: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if initial_requirement and initial_requirement.strip():
            start_run(RunRequest(requirement=initial_requirement))
        yield

    app = FastAPI(title="AgentTeam Dashboard", lifespan=lifespan)
    events = EventBus()
    state = RunState(events)
    clients: set[asyncio.Queue] = set()
    current: dict = {"task": None}

    def fan_out(event: dict) -> None:
        for q in list(clients):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    events.subscribe(fan_out)

    def start_run(req: RunRequest) -> None:
        task = current["task"]
        if task is not None and not task.done():
            raise HTTPException(status_code=409, detail="A delivery run is already in progress")
        run_settings = settings.with_overrides(
            provider=req.provider.lower() if req.provider else None,
            model=req.model or None,
            allow_execution=True if (req.execute or req.deploy) else None,
            allow_deploy=True if req.deploy else None,
            interactive=False,
            tech_preferences=req.tech.strip() if req.tech and req.tech.strip() else None,
            routing=req.routing if req.routing in ("auto", "fixed") else None,
            token_budget=req.token_budget if req.token_budget and req.token_budget > 0 else None,
        )
        events.reset()
        orchestrator = Orchestrator(run_settings, events, state=state)

        async def _run():
            try:
                await orchestrator.run(req.requirement)
            except Exception:
                pass  # reported via run_failed event

        current["task"] = asyncio.create_task(_run())

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    async def get_state() -> dict:
        snap = state.snapshot()
        snap["config"] = {
            "provider": settings.provider,
            "model": settings.resolved_model(),
            "routing": settings.routing,
            "execute": settings.allow_execution,
            "deploy": settings.allow_deploy,
        }
        return snap

    @app.post("/api/runs")
    async def create_run(req: RunRequest) -> dict:
        if not req.requirement.strip():
            raise HTTPException(status_code=400, detail="Requirement is empty")
        start_run(req)
        return {"started": True}

    @app.get("/api/file", response_class=PlainTextResponse)
    async def get_file(path: str) -> str:
        if not state.project_dir:
            raise HTTPException(status_code=404, detail="No project")
        root = Path(state.project_dir).resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return target.read_text(encoding="utf-8", errors="replace")

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        clients.add(queue)
        try:
            for event in events.history[-300:]:
                await socket.send_text(json.dumps(event, default=str))
            while True:
                event = await queue.get()
                await socket.send_text(json.dumps(event, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.discard(queue)

    return app


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8000, initial_requirement: str | None = None) -> None:
    import uvicorn

    print(f"AgentTeam dashboard: http://{host}:{port}")
    uvicorn.run(create_app(settings, initial_requirement), host=host, port=port, log_level="warning")
