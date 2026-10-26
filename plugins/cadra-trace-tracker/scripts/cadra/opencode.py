"""OpenCode (sst/opencode) sessions -> canonical messages (same shape as claude.py).
Store (read-only): `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`
(`$OPENCODE_HOME` is this plugin's own override; OpenCode has no such variable).
Docs-inferred fallback (not run on Windows): if that directory holds no db and
`%LOCALAPPDATA%\\opencode` exists, that is used instead.
- Db file: `$OPENCODE_DB` when set (absolute = as is, relative = joined to the data
  dir); otherwise `opencode.db` first, then every `opencode-<channel>.db` (`latest`,
  `beta` and `prod` share `opencode.db`; dev/snapshot channels get their own).
  Duplicate session ids are read once.
  Desktop UI state (`ai.opencode.desktop/*.dat`) is not session data and is ignored.
- Two layouts, both read; one SQLite file `opencode.db`, opened via
  `collect.open_sqlite_ro` (read-only, reads the WAL, never creates `-wal`/`-shm`).
  1.x (VERIFIED against 1.18.x; unchanged through 1.19.4): `session` (id, parent_id,
  directory, time_created, time_updated, version), `message` (session_id, data JSON
  with `role`), `part` (message_id, data JSON with `type`).
  2.x (from source, `anomalyco/opencode` HEAD 2026-10; `brew install opencode` gives
  2.1): `session_v2` with the same columns, and `session_message` (session_id, type,
  seq, data) — one row per turn; `user.text`, `assistant.content[]` of `text` /
  `reasoning` / `tool {id, name, state {status, input, content[], error.message}}`.
  The 1.x tables are left in place by the upgrade, empty, so `session_v2` wins.
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
    conn = collect.open_sqlite_ro(db)
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
def _session_table(conn: sqlite3.Connection) -> str:
    """OpenCode 2.x renamed `session` to `session_v2` (and moved the conversation
    into `session_message`); 1.x stores have only `session`."""
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
        "('session', 'session_v2')")}
    return "session_v2" if "session_v2" in names else "session"


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
                    f"SELECT id, parent_id, directory FROM {_session_table(conn)} "
                    "ORDER BY time_created").fetchall()
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
    """1.x: `state.output` / `state.error` strings. 2.x: `state.content` items
    (`text`, or `file` which becomes a placeholder) / `state.error.message`."""
    if state.get("status") == "error" and isinstance(state.get("error"), dict):
        return str(state["error"].get("message") or "")
    out = state.get("output") if state.get("status") != "error" else state.get("error")
    if isinstance(state.get("content"), list):
        pieces = []
        for item in state["content"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                pieces.append(item["text"])
            elif item.get("type") == "file":
                name = item.get("name") or str(item.get("uri") or "").rsplit("/", 1)[-1]
                pieces.append(f"[file: {name}]")
        out = "\n".join(pieces)
    if isinstance(out, str):
        return out
    return json.dumps(out) if out is not None else ""


def _tool_messages(call_id: object, name: object, state: dict, workspace: Path) -> list[dict]:
    out = [{"role": "assistant", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": _arguments(state.get("input"), workspace)},
    }]}]
    if state.get("status") in ("completed", "error"):
        out.append({"role": "tool", "tool_call_id": call_id, "content": _output(state)})
    return out


def _load_v1(conn: sqlite3.Connection, session_id: str, workspace: Path,
             messages: list[dict], unmapped: Counter) -> int:
    """1.x: one `part` row per block, joined to its `message` for the role."""
    rows = conn.execute(
        "SELECT m.data AS mdata, p.data AS pdata "
        "FROM message m JOIN part p ON p.message_id = m.id "
        "WHERE m.session_id = ? ORDER BY m.time_created, m.id, p.id", (session_id,)).fetchall()
    unreadable = 0
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
            messages += _tool_messages(part.get("callID"), part.get("tool"), state, workspace)
        else:
            unmapped[kind] += 1
    return unreadable


def _load_v2(conn: sqlite3.Connection, session_id: str, workspace: Path,
             messages: list[dict], unmapped: Counter) -> int:
    """2.x: one `session_message` row per turn; the assistant turn carries its
    text, reasoning and tool blocks in `data.content`. `synthetic` is the injected
    prompt text (dropped by design, as in 1.x); every other kind is counted."""
    rows = conn.execute("SELECT type, data FROM session_message WHERE session_id = ? "
                        "ORDER BY seq", (session_id,)).fetchall()
    unreadable = 0
    for row in rows:
        data = _json(row["data"])
        kind = row["type"] or data.get("type")
        if not kind or not data:
            unreadable += 1
        elif kind == "synthetic":
            continue
        elif kind == "user":
            if isinstance(data.get("text"), str) and data["text"]:
                messages.append({"role": "user", "content": data["text"]})
        elif kind == "assistant":
            for item in data.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str) \
                        and item["text"]:
                    messages.append({"role": "assistant", "content": item["text"]})
                elif item.get("type") == "tool":
                    state = item.get("state") if isinstance(item.get("state"), dict) else {}
                    messages += _tool_messages(item.get("id"), item.get("name"), state,
                                               workspace)
                elif item.get("type") != "reasoning":
                    unmapped[str(item.get("type"))] += 1
        else:
            unmapped[kind] += 1
    return unreadable


def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    messages: list[dict] = []
    unmapped: Counter = Counter()
    started = ended = version = None
    try:
        with _connect(session.main_file) as conn:
            table = _session_table(conn)
            info = conn.execute(f"SELECT version, time_created, time_updated FROM {table} "
                                "WHERE id = ?", (session.session_id,)).fetchone()
            if info:
                version = info["version"] or None
                started, ended = _iso(info["time_created"]), _iso(info["time_updated"])
            load = _load_v2 if table == "session_v2" else _load_v1
            unreadable = load(conn, session.session_id, workspace, messages, unmapped)
    except sqlite3.Error:
        return [], {"cwds": [session.origin_cwd], "started_at": None, "ended_at": None,
                    "agent_version": "", "unreadable_lines": 1}
    meta = {"cwds": [session.origin_cwd], "started_at": started, "ended_at": ended,
            "agent_version": version or "", "unreadable_lines": unreadable}
    if unmapped:
        meta["unmapped_parts"] = dict(unmapped)
    return messages, meta
