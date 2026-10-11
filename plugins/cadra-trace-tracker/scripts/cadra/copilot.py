"""GitHub Copilot CLI sessions -> canonical messages (same shape as adapt.py).

Store (read-only): `$COPILOT_HOME` or `~/.copilot`, then
`session-state/<uuid>/events.jsonl`. Dot dirs in
the root (`.session-operation-locks/`) are not sessions and are never opened.
Every line is `{type, id, parentId, timestamp, data}`.

- Line 1 must be `session.start`: `data.context.cwd` scopes the session,
  `data.sessionId` names it, `data.copilotVersion` is the agent version.
- Kept: `user.message` (`data.content` only; `transformedContent` carries host
  reminders), `assistant.message` (`content` + `toolRequests`; reasoning fields
  dropped), `tool.execution_complete` (`result.content`, else `error.message`).
  `session.context_changed.cwd` feeds `meta.cwds`. Everything else
  (`system.message`, `session.model_change`, `session.shutdown`,
  `tool.execution_start`, ...) is dropped.

Only `session.start`, `session.model_change` and `session.shutdown` were seen in a
real store. The conversation shapes come from the published Copilot SDK docs and
are unverified, so discovery says so once per in-scope session.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cadra import adapt, collect

MAX_META_BYTES = 1024 * 1024
HOST = "github-copilot-cli"
UNVERIFIED_NOTE = ("Copilot CLI message format is taken from published docs and was not "
                   "verified against a real transcript; please report mismatches.")

open_text = open  # indirection so tests can observe which files are opened


def default_root() -> Path:
    home = os.environ.get("COPILOT_HOME")
    return (Path(home) if home else Path.home() / ".copilot") / "session-state"


def _read_start(path: Path) -> dict | None:
    """The `session.start` data from line 1, or None. Bounded read."""
    try:
        with open_text(path, encoding="utf-8", errors="ignore") as handle:
            line = handle.readline(MAX_META_BYTES + 1)
    except OSError:
        return None
    if len(line) > MAX_META_BYTES:
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "session.start":
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def discover(root: Path, workspace: Path) -> tuple[list[collect.Session], list[str]]:
    sessions: list[collect.Session] = []
    notes: list[str] = []
    if not root.is_dir():
        return sessions, notes
    for directory in sorted(root.iterdir()):
        if directory.name.startswith(".") or not directory.is_dir():
            continue
        path = directory / "events.jsonl"
        if not path.is_file():
            continue
        start = _read_start(path)
        if start is None:
            notes.append(f"copilot {directory.name[:8]}: no session.start — skipped")
            continue
        context = start.get("context")
        cwd = context.get("cwd") if isinstance(context, dict) else None
        if not isinstance(cwd, str) or not cwd:
            notes.append(f"copilot {directory.name[:8]}: no working directory recorded"
                         " — skipped")
            continue
        session_id = str(start.get("sessionId") or directory.name)
        if not collect.in_scope(cwd, workspace):
            notes.append(f"{session_id[:8]}: started elsewhere ({cwd}) — not included")
            continue
        sessions.append(collect.Session(session_id=session_id, main_file=path,
                                        origin_cwd=cwd, host=HOST))
        notes.append(f"{session_id[:8]}: {UNVERIFIED_NOTE}")
    return sessions, notes


def _read_lines(path: Path) -> tuple[list[dict], int]:
    entries: list[dict] = []
    bad = 0
    try:
        with open_text(path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
                else:
                    bad += 1
    except OSError:
        bad += 1
    return entries, bad


def _tool_call(request: dict, workspace: Path) -> dict:
    raw = request.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {"input": raw}
    args = dict(raw) if isinstance(raw, dict) else {}
    for key in adapt.PATH_ARG_KEYS:
        if isinstance(args.get(key), str):
            args[key] = adapt.rebase_path(args[key], workspace)
    return {"id": request.get("toolCallId"), "type": "function",
            "function": {"name": request.get("name"), "arguments": args}}


def _to_message(kind: str, data: dict, workspace: Path) -> dict | None:
    if kind == "user.message":
        content = data.get("content")
        if not isinstance(content, str) or not content:
            return None
        return {"role": "user", "content": content}
    if kind == "assistant.message":
        out: dict = {"role": "assistant"}
        if isinstance(data.get("content"), str) and data["content"]:
            out["content"] = data["content"]
        requests = [r for r in data.get("toolRequests") or [] if isinstance(r, dict)]
        if requests:
            out["tool_calls"] = [_tool_call(r, workspace) for r in requests]
        return out if len(out) > 1 else None
    if kind == "tool.execution_complete":
        result = data.get("result")
        error = data.get("error")
        content = result.get("content") if isinstance(result, dict) else None
        if not isinstance(content, str) and isinstance(error, dict):
            content = error.get("message")
        return {"role": "tool", "tool_call_id": data.get("toolCallId"),
                "content": content if isinstance(content, str) else ""}
    return None


def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    entries, unreadable = _read_lines(session.main_file)
    messages: list[dict] = []
    cwds: list[str] = [session.origin_cwd]
    version = ""
    stamps: list[str] = []
    for entry in entries:
        kind = entry.get("type")
        data = entry.get("data")
        if not isinstance(kind, str) or not isinstance(data, dict):
            continue
        ts = entry.get("timestamp")
        if isinstance(ts, str) and ts:
            stamps.append(ts)
        if kind == "session.start":
            if not version and isinstance(data.get("copilotVersion"), str):
                version = data["copilotVersion"]
            continue
        if kind == "session.context_changed":
            cwd = data.get("cwd")
            if isinstance(cwd, str) and cwd and cwd not in cwds:
                cwds.append(cwd)
            continue
        message = _to_message(kind, data, workspace)
        if message is None:
            continue
        if isinstance(ts, str) and ts:
            message["client_ts"] = ts
        messages.append(message)
    meta = {"cwds": cwds, "started_at": stamps[0] if stamps else None,
            "ended_at": stamps[-1] if stamps else None, "agent_version": version,
            "unreadable_lines": unreadable}
    return messages, meta
