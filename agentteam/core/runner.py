"""Sandboxed-ish command execution inside the generated project directory."""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .events import EventBus

# Commands that should never be executed regardless of what an agent proposes.
_BLOCKED = ("rm -rf /", "format c:", "del /s /q c:\\", "shutdown", "mkfs", ":(){", "reg delete", "diskpart")


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def summary(self, limit: int = 6000) -> str:
        out = self.output
        if len(out) > limit:
            out = out[: limit // 3] + "\n...[output truncated]...\n" + out[-(2 * limit // 3) :]
        return f"$ {self.command}\nexit code: {self.exit_code} ({self.duration:.1f}s)\n{out}"


class CommandRunner:
    def __init__(self, root: Path, events: EventBus, timeout: int = 900, enabled: bool = False):
        self.root = root
        self.events = events
        self.timeout = timeout
        self.enabled = enabled

    async def run(self, command: str, agent: str, cwd: str | None = None, timeout: int | None = None) -> CommandResult:
        if not self.enabled:
            return CommandResult(command, -1, "Command execution is disabled (set ALLOW_EXECUTION=true or use --execute).", 0)
        lowered = command.lower()
        if any(b in lowered for b in _BLOCKED):
            return CommandResult(command, -1, "Command blocked by safety policy.", 0)
        workdir = (self.root / cwd).resolve() if cwd else self.root
        if not str(workdir).startswith(str(self.root)):
            return CommandResult(command, -1, "Working directory outside project is not allowed.", 0)
        self.events.emit("command_started", agent=agent, command=command, cwd=str(workdir))
        started = time.time()

        def _run() -> CommandResult:
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout or self.timeout,
                )
                output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
                return CommandResult(command, proc.returncode, output, time.time() - started)
            except subprocess.TimeoutExpired as exc:
                out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
                return CommandResult(command, 124, out + "\n[timed out]", time.time() - started)
            except OSError as exc:
                return CommandResult(command, 127, f"Failed to start: {exc}", time.time() - started)

        result = await asyncio.to_thread(_run)
        self.events.emit(
            "command_finished", agent=agent, command=command, exit_code=result.exit_code,
            duration=round(result.duration, 1), output_tail=result.output[-2000:],
        )
        return result
