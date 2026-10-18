"""Helpers for parsing structured LLM output."""

from __future__ import annotations

import json
import re

FILE_BLOCK_RE = re.compile(
    r"^=== FILE:\s*(?P<path>[^\n=]+?)\s*===[ \t]*\r?\n(?P<body>.*?)\r?\n=== END FILE ===",
    re.DOTALL | re.MULTILINE,
)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n```", re.DOTALL)


def _try_load(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # tolerate trailing commas
    cleaned = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None


def _balanced_slice(text: str, open_ch: str, close_ch: str) -> str | None:
    start = text.find(open_ch)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json(text: str):
    """Extract the first JSON object/array from an LLM reply."""
    text = text.strip()
    value = _try_load(text)
    if value is not None:
        return value
    for match in _FENCE_RE.finditer(text):
        value = _try_load(match.group(1).strip())
        if value is not None:
            return value
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        candidate = _balanced_slice(text, open_ch, close_ch)
        if candidate:
            value = _try_load(candidate)
            if value is not None:
                return value
    raise ValueError("No valid JSON found in model response")


def _strip_outer_fence(body: str) -> str:
    stripped = body.strip("\n")
    lines = stripped.split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]) + "\n"
    return body if body.endswith("\n") else body + "\n"


def parse_files_response(text: str) -> tuple[dict, list[dict]]:
    """Parse a reply consisting of a JSON metadata block followed by file blocks."""
    first_file = text.find("=== FILE:")
    head = text if first_file < 0 else text[:first_file]
    try:
        meta = extract_json(head) if head.strip() else {}
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {"data": meta}
    files = []
    for m in FILE_BLOCK_RE.finditer(text):
        files.append({"path": m.group("path").strip().strip("`"), "content": _strip_outer_fence(m.group("body"))})
    return meta, files
