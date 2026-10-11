"""OpenCode (sst/opencode) sessions -> canonical messages (same shape as adapt.py).
Store (read-only): `$OPENCODE_HOME` or `$XDG_DATA_HOME/opencode` or
`~/.local/share/opencode`. Docs-inferred fallback (not run on Windows): if that
directory holds no db and `%LOCALAPPDATA%\\opencode` exists, that is used instead.
- Db file: `$OPENCODE_DB` when set (absolute = as is, relative = joined to the data
  dir); otherwise `opencode.db` first, then every `opencode-<channel>.db` (non-stable
  channels name the db after the channel). Duplicate session ids are read once.
  Desktop UI state (`ai.opencode.desktop/*.dat`) is not session data and is ignored.
- Format VERIFIED against opencode 1.18.x on a real machine (structure only): one
  SQLite file `opencode.db`, opened `mode=ro&immutable=1` so no `-wal`/`-shm` file is
  created. Tables used: `session` (id, parent_id, directory, time_created,
  time_updated, version), `message` (session_id, time_created, data JSON with
  `role`), `part` (message_id, data JSON with `type`).
- The older per-file layout (`storage/session|message|part/*.json`) is NOT read;
  a note says so when only that layout exists. That layout is unverified here.
- Parts mapped: `text` (user/assistant) and `tool` (call + result; `state.input`,
  `state.output` or `state.error`). Dropped by design: `reasoning`, synthetic text
  (injected prompts), and provider/account/model ids. Any other part type
  (`file`, `patch`, `compaction`, `step-*`...) is counted in a note, never silent.
- Sub-agent sessions (`parent_id` set) are skipped with a count. Working directory
  is `session.directory`. Message times are epoch ms in `time_created`.
"""
from __future__ import annotations
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from cadra import adapt, collect
HOST = "opencode"
DB_NAME = "opencode.db"
IGNORED_PARTS = {"reasoning", "step-start", "step-finish"}
def default_root() -> Path:
    home = os.environ.get("OPENCODE_HOME")
    if home:
        return Path(home)
    xdg = os.environ.get("XDG_DATA_HOME")
    default = (Path(xdg) if xdg else Path.home() / ".local" / "share") / "opencode"
    local = os.environ.get("LOCALAPPDATA")
    if local and not _db_files(default) and (Path(local) / "opencode").is_dir():
        return Path(local) / "opencode"
    return default
def _db_files(root: Path) -> list[Path]:
    """Session dbs in the data dir: `opencode.db`, then any channel db."""
    found = [root / DB_NAME] if (root / DB_NAME).is_file() else []
    return found + sorted(p for p in root.glob("opencode-*.db") if p.is_file())
def db_candidates(root: Path) -> list[Path]:
    override = os.environ.get("OPENCODE_DB")
    if override:
        path = Path(override)
        path = path if path.is_absolute() else root / path
        return [path] if path.is_file() else []
    return _db_files(root) if root.is_dir() else []
def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
def _iso(ms: object) -> str | None:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
def _json(raw: object) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
def discover(root: Path, workspace: Path) -> tuple[list[collect.Session], list[str]]:
    sessions: list[collect.Session] = []
    notes: list[str] = []
    dbs = db_candidates(root)
    if not dbs:
        if (root / "storage" / "session").is_dir():
            notes.append("opencode: only the older JSON storage layout was found; "
                         "it is not supported — nothing included")
        return sessions, notes
    seen: set[str] = set()
    subagents = 0
    for db in dbs:
        try:
            with _connect(db) as conn:
                rows = conn.execute(
                    "SELECT id, parent_id, directory FROM session ORDER BY time_created"
                ).fetchall()
        except sqlite3.Error:
            notes.append(f"opencode: {db.name} could not be read — skipped")
            continue
        for row in rows:
            cwd = row["directory"]
            if not isinstance(cwd, str) or not cwd or not collect.in_scope(cwd, workspace):
                continue
            if row["parent_id"]:
                subagents += 1
                continue
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            sessions.append(collect.Session(session_id=row["id"], main_file=db,
                                            origin_cwd=cwd, host=HOST))
    if subagents:
        notes.append(f"opencode: {subagents} sub-agent session(s) not included")
    return sessions, notes
def _arguments(raw: object, workspace: Path) -> dict:
    args = dict(raw) if isinstance(raw, dict) else {}
    for key in adapt.PATH_ARG_KEYS:
        if isinstance(args.get(key), str):
            args[key] = adapt.rebase_path(args[key], workspace)
    return args
def _output(state: dict) -> str:
    out = state.get("output") if state.get("status") != "error" else state.get("error")
    if isinstance(out, str):
        return out
    return json.dumps(out) if out is not None else ""
def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    messages: list[dict] = []
    unmapped: Counter = Counter()
    unreadable = 0
    started = ended = version = None
    try:
        with _connect(session.main_file) as conn:
            info = conn.execute("SELECT version, time_created, time_updated FROM session "
                                "WHERE id = ?", (session.session_id,)).fetchone()
            if info:
                version = info["version"] or None
                started, ended = _iso(info["time_created"]), _iso(info["time_updated"])
            rows = conn.execute(
                "SELECT m.data AS mdata, m.time_created AS ts, p.data AS pdata "
                "FROM message m JOIN part p ON p.message_id = m.id "
                "WHERE m.session_id = ? ORDER BY m.time_created, m.id, p.id",
                (session.session_id,)).fetchall()
    except sqlite3.Error:
        return [], {"cwds": [session.origin_cwd], "started_at": None, "ended_at": None,
                    "agent_version": "", "unreadable_lines": 1}
    for row in rows:
        role = _json(row["mdata"]).get("role")
        part = _json(row["pdata"])
        kind = part.get("type")
        if role not in ("user", "assistant") or not kind:
            unreadable += 1
            continue
        if kind in IGNORED_PARTS:
            continue
        if kind == "text":
            text = part.get("text")
            if isinstance(text, str) and text and not part.get("synthetic"):
                messages.append({"role": role, "content": text})
        elif kind == "tool" and role == "assistant":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            call_id = part.get("callID")
            messages.append({"role": "assistant", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": part.get("tool"),
                             "arguments": _arguments(state.get("input"), workspace)},
            }]})
            if state.get("status") in ("completed", "error"):
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": _output(state)})
        else:
            unmapped[kind] += 1
    meta = {"cwds": [session.origin_cwd], "started_at": started, "ended_at": ended,
            "agent_version": version or "", "unreadable_lines": unreadable}
    if unmapped:
        meta["unmapped_parts"] = dict(unmapped)
    return messages, meta
