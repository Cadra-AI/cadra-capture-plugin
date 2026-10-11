"""Kiro sessions -> canonical messages (same shape as adapt.py).

Store (read-only): `$KIRO_HOME` or `~/.kiro`, then
`sessions/<hash>/sess_<id>/` holding `session.json`,
`messages.jsonl`, optionally `sub-executions/<subSessionId>.jsonl`, plus
`tool-outputs/`, `snapshots/` and cursors, which are never opened.

- `session.json`: `workspacePaths[0]` scopes the session, `id` names it. There is
  no agent version field.
- `messages.jsonl` records are `{id, timestamp, payload{type, ...}}`. Kept:
  `user {content}`, `assistant {content}`, `tool_call {toolCallId, toolName,
  args}`, `tool_result {toolCallId, content}`. Everything else is dropped,
  including `session_start` (injected prompt), `steering_inclusion`,
  `agent_note`, `turn_start`/`turn_end` and context snapshots.
- `sub_agent_start {subSessionId, subAgentName}` links a sub-execution file; its
  records share the shape above and are tagged as subagent work. There is no
  parent tool-use id, so none is sent.
- When Kiro offloads a long tool result it keeps a truncated text that mentions
  `tool-outputs`. That text is what is sent; the count is reported as a NOTE.
- Second layout (v3 CLI, VERIFIED locally, structure only): flat files in
  `sessions/cli/`: `<uuid>.json` (`session_id`, `cwd`, `created_at`, `updated_at`,
  `title`, `session_state`...; the state, permissions and model ids are never sent),
  `<uuid>.jsonl` and `<uuid>.history` (prompt history, `.lock`: never opened).
  Records are `{version, kind, data}`: `Prompt` (text blocks, `meta.timestamp` epoch
  seconds), `AssistantMessage` (`text`, `toolUse {toolUseId, name, input}`, and
  `thinking`, dropped), `ToolResults` (`toolResult {toolUseId, content[], status}`
  with `text`/`json`/`image` blocks). `Clear` is dropped; any other kind or block is
  counted in a NOTE. Both layouts are read; a session id seen twice is read once.
  Documented, not run here: Windows `%USERPROFILE%\\.kiro\\sessions\\cli\\`.
- Older v1/v2 history (`data.sqlite3`, tables `conversations`/`conversations_v2`
  keyed by directory) is NOT read: its value JSON could not be verified (the local
  tables are empty). When rows exist a NOTE says so. Only row counts are taken;
  `auth_kv` is never queried. Locations: macOS `~/Library/Application
  Support/kiro-cli/`, Linux `~/.local/share/kiro-cli/`, Windows `%APPDATA%\\kiro-cli\\`.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from cadra import adapt, collect

MAX_SESSION_JSON_BYTES = 256 * 1024
HOST = "kiro"
OFFLOAD_MARKER = "tool-outputs"

open_text = open  # indirection so tests can observe which files are opened


def default_root() -> Path:
    home = os.environ.get("KIRO_HOME")
    return (Path(home) if home else Path.home() / ".kiro") / "sessions"


def legacy_db_candidates() -> list[Path]:
    """Where Kiro v1/v2 keeps `data.sqlite3` on this OS (docs-inferred off macOS)."""
    appdata = os.environ.get("APPDATA")
    paths = [Path.home() / "Library" / "Application Support" / "kiro-cli",
             Path.home() / ".local" / "share" / "kiro-cli"]
    if appdata:
        paths.append(Path(appdata) / "kiro-cli")
    return [p / "data.sqlite3" for p in paths]
def _legacy_rows(db: Path) -> int:
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        with conn:
            return sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                       for t in ("conversations", "conversations_v2"))
    except sqlite3.Error:
        return 0
def _read_session_json(path: Path) -> tuple[dict | None, str]:
    try:
        with open_text(path, encoding="utf-8", errors="ignore") as handle:
            raw = handle.read(MAX_SESSION_JSON_BYTES + 1)
    except OSError:
        return None, "could not be read"
    if len(raw) > MAX_SESSION_JSON_BYTES:
        return None, "session.json is larger than 256 KB"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "session.json is not readable"
    return (data, "") if isinstance(data, dict) else (None, "session.json is not readable")


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


def _payload(entry: dict) -> dict:
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else {}


def _is_offloaded(payload: dict) -> bool:
    content = payload.get("content")
    return (payload.get("type") == "tool_result" and isinstance(content, str)
            and OFFLOAD_MARKER in content)


def _legacy_note(root: Path) -> list[str]:
    if root != default_root():  # an explicit --kiro-root must not touch the real home
        return []
    for db in legacy_db_candidates():
        if db.is_file() and _legacy_rows(db):
            return ["kiro: older Kiro database found (v1/v2 data.sqlite3), not read"]
    return []
def _iso(seconds: object) -> str | None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    try:
        stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
def _discover_cli(root: Path, workspace: Path, seen: set[str]
                  ) -> tuple[list[collect.Session], list[str]]:
    sessions: list[collect.Session] = []
    notes: list[str] = []
    for path in sorted((root / "cli").glob("*.json")):
        data, reason = _read_session_json(path)
        if data is None:
            notes.append(f"kiro {path.stem[:8]}: {reason} — skipped")
            continue
        cwd = data.get("cwd")
        session_id = str(data.get("session_id") or path.stem)
        jsonl = path.with_suffix(".jsonl")
        if not isinstance(cwd, str) or not cwd:
            notes.append(f"kiro {session_id[:8]}: no working directory recorded — skipped")
        elif not collect.in_scope(cwd, workspace):
            notes.append(f"{session_id[:8]}: started elsewhere ({cwd}) — not included")
        elif session_id not in seen and jsonl.is_file():
            seen.add(session_id)
            sessions.append(collect.Session(session_id=session_id, main_file=jsonl,
                                            origin_cwd=cwd, host=HOST))
    return sessions, notes
def discover(root: Path, workspace: Path) -> tuple[list[collect.Session], list[str]]:
    sessions: list[collect.Session] = []
    notes: list[str] = _legacy_note(root)
    if not root.is_dir():
        return sessions, notes
    seen: set[str] = set()
    for path in sorted(root.glob("*/sess_*/session.json")):
        directory = path.parent
        data, reason = _read_session_json(path)
        if data is None:
            notes.append(f"kiro {directory.name}: {reason} — skipped")
            continue
        paths = data.get("workspacePaths")
        cwd = paths[0] if isinstance(paths, list) and paths else None
        if not isinstance(cwd, str) or not cwd:
            notes.append(f"kiro {directory.name}: no working directory recorded — skipped")
            continue
        session_id = str(data.get("id") or directory.name)
        if session_id in seen:
            continue
        if not collect.in_scope(cwd, workspace):
            notes.append(f"{session_id[:8]}: started elsewhere ({cwd}) — not included")
            continue
        main_file = directory / "messages.jsonl"
        subagents = sorted((directory / "sub-executions").glob("*.jsonl"))
        # One pass over the main transcript, only once the session is in scope:
        # sub-agent names for tagging, and how many results Kiro offloaded.
        sub_meta: dict[str, dict] = {}
        offloaded = 0
        entries, _bad = _read_lines(main_file)
        for sub_file in subagents:
            sub_entries, _sub_bad = _read_lines(sub_file)
            offloaded += sum(1 for e in sub_entries if _is_offloaded(_payload(e)))
        for entry in entries:
            payload = _payload(entry)
            if _is_offloaded(payload):
                offloaded += 1
            if payload.get("type") == "sub_agent_start":
                sub_id, name = payload.get("subSessionId"), payload.get("subAgentName")
                if isinstance(sub_id, str) and isinstance(name, str):
                    sub_meta[sub_id] = {"agentType": name}
        if offloaded:
            notes.append(f"{session_id[:8]}: {offloaded} tool result(s) were offloaded "
                         "by Kiro and are sent as the truncated text Kiro kept")
        sessions.append(collect.Session(
            session_id=session_id, main_file=main_file, origin_cwd=cwd,
            subagent_files=subagents, subagent_meta=sub_meta, host=HOST,
        ))
        seen.add(session_id)
    cli_sessions, cli_notes = _discover_cli(root, workspace, seen)
    return sessions + cli_sessions, notes + cli_notes


def _to_message(payload: dict, workspace: Path) -> dict | None:
    kind = payload.get("type")
    content = payload.get("content")
    if kind in ("user", "assistant"):
        if not isinstance(content, str) or not content:
            return None
        return {"role": kind, "content": content}
    if kind == "tool_call":
        raw = payload.get("args")
        args = dict(raw) if isinstance(raw, dict) else {}
        for key in adapt.PATH_ARG_KEYS:
            if isinstance(args.get(key), str):
                args[key] = adapt.rebase_path(args[key], workspace)
        return {"role": "assistant", "tool_calls": [{
            "id": payload.get("toolCallId"), "type": "function",
            "function": {"name": payload.get("toolName"), "arguments": args},
        }]}
    if kind == "tool_result":
        if not isinstance(content, str):
            content = json.dumps(content) if content is not None else ""
        return {"role": "tool", "tool_call_id": payload.get("toolCallId"),
                "content": content}
    return None


def _dicts(content: object) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
def _blocks(content: object, kind: str) -> list:
    return [b.get("data") for b in content if isinstance(b, dict) and b.get("kind") == kind
            ] if isinstance(content, list) else []
def _result_text(blocks: object) -> str:
    parts: list[str] = []
    for block in _dicts(blocks):
        data = block.get("data")
        if isinstance(data, str):
            parts.append(data)
        elif block.get("kind") == "image":
            parts.append("[image omitted]")
        elif data is not None:
            parts.append(json.dumps(data))
    return "\n".join(parts)
def _load_cli(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    entries, unreadable = _read_lines(session.main_file)
    info, _reason = _read_session_json(session.main_file.with_suffix(".json"))
    info = info or {}
    messages: list[dict] = []
    unmapped: Counter = Counter()
    cwds: list[str] = [session.origin_cwd]
    for entry in entries:
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        kind, content = entry.get("kind"), data.get("content")
        if kind == "Prompt":
            ts = _iso((data.get("meta") or {}).get("timestamp")
                      if isinstance(data.get("meta"), dict) else None)
            text = "\n".join(t for t in _blocks(content, "text") if isinstance(t, str))
            if text:
                messages.append({"role": "user", "content": text,
                                 **({"client_ts": ts} if ts else {})})
        elif kind == "AssistantMessage":
            for block in _dicts(content):
                bkind, bdata = block.get("kind"), block.get("data")
                if bkind == "text" and isinstance(bdata, str):
                    if bdata:  # Kiro writes empty text blocks beside tool calls
                        messages.append({"role": "assistant", "content": bdata})
                elif bkind == "toolUse" and isinstance(bdata, dict):
                    raw = bdata.get("input")
                    args = {k: v for k, v in raw.items() if k != "__tool_use_purpose"
                            } if isinstance(raw, dict) else {}
                    for key in adapt.PATH_ARG_KEYS:
                        if isinstance(args.get(key), str):
                            args[key] = adapt.rebase_path(args[key], workspace)
                    wd = args.get("working_dir") or args.get("cwd")
                    if isinstance(wd, str) and wd and wd not in cwds:
                        cwds.append(wd)
                    messages.append({"role": "assistant", "tool_calls": [{
                        "id": bdata.get("toolUseId"), "type": "function",
                        "function": {"name": bdata.get("name"), "arguments": args}}]})
                elif bkind != "thinking":
                    unmapped[f"AssistantMessage.{bkind}"] += 1
        elif kind == "ToolResults":
            for block in _dicts(content):
                bdata = block.get("data")
                if block.get("kind") == "toolResult" and isinstance(bdata, dict):
                    messages.append({"role": "tool", "tool_call_id": bdata.get("toolUseId"),
                                     "content": _result_text(bdata.get("content"))})
                else:
                    unmapped[f"ToolResults.{block.get('kind')}"] += 1
        elif kind != "Clear":
            unmapped[str(kind)] += 1
    meta = {"cwds": cwds, "started_at": info.get("created_at"),
            "ended_at": info.get("updated_at"), "agent_version": "",
            "unreadable_lines": unreadable}
    if unmapped:
        meta["unmapped_parts"] = dict(unmapped)
    return messages, meta
def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    """Main transcript plus sub-executions, interleaved by timestamp."""
    if session.main_file.name != "messages.jsonl":
        return _load_cli(session, workspace)
    main, unreadable = _read_lines(session.main_file)
    tagged: list[tuple[str, dict, dict | None]] = [
        (str(e.get("timestamp") or ""), e, None) for e in main
    ]
    for sub_file in session.subagent_files:
        tag = adapt.agent_tag(session.subagent_meta.get(sub_file.stem) or {})
        sub_entries, sub_bad = _read_lines(sub_file)
        unreadable += sub_bad
        tagged += [(str(e.get("timestamp") or ""), e, tag) for e in sub_entries]
    tagged.sort(key=lambda item: item[0])

    messages: list[dict] = []
    cwds: list[str] = [session.origin_cwd]
    for ts, entry, tag in tagged:
        payload = _payload(entry)
        args = payload.get("args")
        cwd = args.get("cwd") if isinstance(args, dict) else None
        is_call = payload.get("type") == "tool_call"
        if is_call and isinstance(cwd, str) and cwd and cwd not in cwds:
            cwds.append(cwd)
        message = _to_message(payload, workspace)
        if message is None:
            continue
        if ts:
            message["client_ts"] = ts
        if tag:
            message["cadra_agent"] = tag
        messages.append(message)
    stamps = [t for t, _e, _tag in tagged if t]
    meta = {"cwds": cwds, "started_at": stamps[0] if stamps else None,
            "ended_at": stamps[-1] if stamps else None, "agent_version": "",
            "unreadable_lines": unreadable}
    return messages, meta
