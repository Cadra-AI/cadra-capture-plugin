"""Codex CLI rollouts -> canonical messages (same shape as adapt.py).

Store (read-only): `$CODEX_HOME/sessions` or `~/.codex/sessions`, laid out as
`YYYY/MM/DD/rollout-*.jsonl`. Every line is `{"timestamp", "type", "payload"}`.

- Line 1 is `session_meta`: `payload.cwd` scopes the session, `payload.session_id`
  (older rollouts: `payload.id`) names it, `cli_version` is the agent version. It
  also carries the full system prompt (`base_instructions`) and account ids
  (`creator_user_id`, `creator_account_id`); none of that is ever sent.
- `response_item` payloads are the conversation: `message` (user | assistant |
  developer), `function_call {name, arguments: JSON string, call_id}`,
  `function_call_output {call_id, output}`, `custom_tool_call {name, input,
  call_id}` (apply_patch), `custom_tool_call_output`, `reasoning`. Only user and
  assistant messages and the four tool kinds are kept.
- `event_msg` duplicates response items and `turn_context` is settings; both are
  dropped, except that `turn_context.cwd` is recorded in `meta.cwds`.
- Codex injects host context as user messages (`<environment_context>` etc.);
  those are not the candidate's words and are dropped.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cadra import adapt, collect

MAX_META_BYTES = 1024 * 1024
HOST = "codex"
#: User-role texts that Codex writes itself rather than the candidate.
_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>",
                      "# AGENTS.md instructions", "<turn_aborted>")
_KEPT_ITEMS = ("message", "function_call", "function_call_output",
               "custom_tool_call", "custom_tool_call_output")

open_text = open  # indirection so tests can observe which files are opened


def default_root() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "sessions"


def _read_meta(path: Path) -> tuple[dict | None, str]:
    """-> (session_meta payload, reason it is unusable). Line 1 only, capped."""
    try:
        with open_text(path, encoding="utf-8", errors="ignore") as handle:
            line = handle.readline(MAX_META_BYTES + 1)
    except OSError:
        return None, "could not be read"
    if len(line) > MAX_META_BYTES:
        return None, "first line is larger than 1 MB"
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None, "first line is not readable"
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        return None, "does not start with session metadata"
    payload = entry.get("payload")
    return (payload, "") if isinstance(payload, dict) else (None, "has no session metadata")


def discover(root: Path, workspace: Path) -> tuple[list[collect.Session], list[str]]:
    sessions: list[collect.Session] = []
    notes: list[str] = []
    if not root.is_dir():
        return sessions, notes
    for path in sorted(root.glob("*/*/*/rollout-*.jsonl")):
        meta, reason = _read_meta(path)
        if meta is None:
            notes.append(f"codex {path.name}: {reason} — skipped")
            continue
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            notes.append(f"codex {path.name}: no working directory recorded — skipped")
            continue
        session_id = meta.get("session_id") or meta.get("id") or path.stem
        if not collect.in_scope(cwd, workspace):
            notes.append(f"{str(session_id)[:8]}: started elsewhere ({cwd}) — not included")
            continue
        sessions.append(collect.Session(session_id=str(session_id), main_file=path,
                                        origin_cwd=cwd, host=HOST))
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


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b["text"] for b in content
             if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(parts)


def _arguments(raw: object, workspace: Path) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"input": raw}
    args = dict(raw) if isinstance(raw, dict) else {}
    for key in adapt.PATH_ARG_KEYS:
        if isinstance(args.get(key), str):
            args[key] = adapt.rebase_path(args[key], workspace)
    return args


def _to_message(item: dict, workspace: Path) -> dict | None:
    kind = item.get("type")
    if kind == "message":
        role = item.get("role")
        text = _text(item.get("content"))
        if role not in ("user", "assistant") or not text:
            return None  # developer messages are host instructions
        if role == "user" and text.lstrip().startswith(_INJECTED_PREFIXES):
            return None
        return {"role": role, "content": text}
    if kind == "function_call":
        args = _arguments(item.get("arguments"), workspace)
    elif kind == "custom_tool_call":
        # apply_patch input is a raw patch; its paths are not rebased (plan D6).
        raw = item.get("input")
        args = {"input": raw if isinstance(raw, str) else ""}
    elif kind in ("function_call_output", "custom_tool_call_output"):
        output = item.get("output")
        if not isinstance(output, str):
            output = json.dumps(output) if output is not None else ""
        return {"role": "tool", "tool_call_id": item.get("call_id"), "content": output}
    else:
        return None
    return {"role": "assistant", "tool_calls": [{
        "id": item.get("call_id"), "type": "function",
        "function": {"name": item.get("name"), "arguments": args},
    }]}


def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    entries, unreadable = _read_lines(session.main_file)
    messages: list[dict] = []
    cwds: list[str] = [session.origin_cwd]
    version = ""
    stamps: list[str] = []
    for entry in entries:
        kind = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = entry.get("timestamp")
        if isinstance(ts, str) and ts:
            stamps.append(ts)
        if kind == "session_meta":
            # Only the version is taken; prompt and account ids stay behind.
            if not version and isinstance(payload.get("cli_version"), str):
                version = payload["cli_version"]
            continue
        if kind == "turn_context":
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd and cwd not in cwds:
                cwds.append(cwd)
            continue
        if kind != "response_item" or payload.get("type") not in _KEPT_ITEMS:
            continue
        message = _to_message(payload, workspace)
        if message is None:
            continue
        if isinstance(ts, str) and ts:
            message["client_ts"] = ts
        messages.append(message)
    meta = {"cwds": cwds, "started_at": stamps[0] if stamps else None,
            "ended_at": stamps[-1] if stamps else None, "agent_version": version,
            "unreadable_lines": unreadable}
    return messages, meta
