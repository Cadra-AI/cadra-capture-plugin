"""Cursor CLI (`agent`, formerly `cursor-agent`) transcripts -> canonical messages
(same shape as adapt.py).
Store (read-only): `$CURSOR_HOME` or `~/.cursor`.
- Transcript: `projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl` (a flat
  `<uuid>.jsonl` under `agent-transcripts/` is also accepted). Legacy `*.txt` files and
  `subagents/` folders are ignored. `<slug>` is a lossy path encoding and is never
  decoded. Lines are `{"role", "message": {"content": [blocks]}}`; blocks are
  `text` and `tool_use {name, input}`. A final `{"type": "turn_ended"}` control line
  has no role and is skipped.
- Cursor records NO timestamps, NO tool-use ids and NO tool results. Tool-call ids
  here are generated (`cursor-<n>`), and there are no `client_ts` values.
- Working directory: only `chats/<workspace-hash>/<uuid>/meta.json` (`cwd`,
  `createdAtMs`, `updatedAtMs`), found by the transcript's uuid. The same chat folder
  holds `store.db` and `prompt_history.json`; neither is ever opened
  (`prompt_history.json` is workspace-wide, not per session).
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from cadra import adapt, collect
MAX_META_BYTES = 256 * 1024
HOST = "cursor"
open_text = open  # indirection so tests can observe which files are opened
def default_root() -> Path:
    home = os.environ.get("CURSOR_HOME")
    return Path(home) if home else Path.home() / ".cursor"
def _read_meta(root: Path, uuid: str) -> tuple[dict | None, str]:
    """-> (meta.json contents, reason it is unusable). Bounded read."""
    found = sorted(root.glob(f"chats/*/{uuid}/meta.json"))
    if not found:
        return None, "no session metadata found"
    try:
        with open_text(found[0], encoding="utf-8", errors="ignore") as handle:
            raw = handle.read(MAX_META_BYTES + 1)
    except OSError:
        return None, "metadata could not be read"
    if len(raw) > MAX_META_BYTES:
        return None, "metadata is larger than 256 KB"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "metadata is not readable"
    return (data, "") if isinstance(data, dict) else (None, "metadata is not readable")
def _iso(ms: object) -> str | None:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
def _transcripts(root: Path) -> list[Path]:
    base = root.glob("projects/*/agent-transcripts")
    files: list[Path] = []
    for folder in sorted(base):
        files += folder.glob("*/*.jsonl")
        files += folder.glob("*.jsonl")
    return [p for p in files if "subagents" not in p.parts]
def discover(root: Path, workspace: Path) -> tuple[list[collect.Session], list[str]]:
    sessions: list[collect.Session] = []
    notes: list[str] = []
    if not root.is_dir():
        return sessions, notes
    for path in _transcripts(root):
        uuid = path.stem
        meta, reason = _read_meta(root, uuid)
        if meta is None:
            notes.append(f"cursor {uuid[:8]}: {reason} — skipped")
            continue
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            notes.append(f"cursor {uuid[:8]}: no working directory recorded — skipped")
            continue
        if not collect.in_scope(cwd, workspace):
            notes.append(f"{uuid[:8]}: started elsewhere ({cwd}) — not included")
            continue
        notes.append(f"{uuid[:8]}: Cursor does not record tool outputs, so file contents "
                     "read or produced by tools are not in this transcript")
        sessions.append(collect.Session(session_id=uuid, main_file=path,
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
def _arguments(raw: object, workspace: Path) -> dict:
    args = dict(raw) if isinstance(raw, dict) else {}
    for key in adapt.PATH_ARG_KEYS:
        if isinstance(args.get(key), str):
            args[key] = adapt.rebase_path(args[key], workspace)
    return args
def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    entries, unreadable = _read_lines(session.main_file)
    messages: list[dict] = []
    calls = 0
    for entry in entries:
        role = entry.get("role")
        message = entry.get("message")
        if role not in ("user", "assistant") or not isinstance(message, dict):
            continue  # control lines such as turn_ended carry no role
        content = message.get("content")
        blocks = ([{"type": "text", "text": content}] if isinstance(content, str)
                  else content if isinstance(content, list) else [])
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                if block["text"]:
                    messages.append({"role": role, "content": block["text"]})
            elif block.get("type") == "tool_use" and role == "assistant":
                messages.append({"role": "assistant", "tool_calls": [{
                    "id": f"cursor-{calls}", "type": "function",
                    "function": {"name": block.get("name"),
                                 "arguments": _arguments(block.get("input"), workspace)},
                }]})
                calls += 1
    meta_json, _reason = _read_meta(_store_root(session.main_file), session.session_id)
    meta_json = meta_json or {}
    meta = {"cwds": [session.origin_cwd],
            "started_at": _iso(meta_json.get("createdAtMs")),
            "ended_at": _iso(meta_json.get("updatedAtMs")),
            "agent_version": "", "unreadable_lines": unreadable}
    return messages, meta
def _store_root(transcript: Path) -> Path:
    """<root>/projects/<slug>/agent-transcripts[/<uuid>]/<uuid>.jsonl -> <root>."""
    folder = transcript.parent
    if folder.name != "agent-transcripts":
        folder = folder.parent
    return folder.parent.parent.parent
