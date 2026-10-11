"""Google Antigravity CLI (`agy`) chats -> canonical messages (same shape as adapt.py).
Store (read-only): `$ANTIGRAVITY_HOME` or `~/.gemini/antigravity-cli`.
- Chat: `conversations/<conversationId>.db`, a SQLite file. It is ALWAYS opened with
  `file:<path>?mode=ro&immutable=1`, so no `-wal`/`-shm` file is ever created beside it.
  Conversations stored as `<id>.pb` are not supported; they are counted in one NOTE.
- Not used: `brain/<id>/.system_generated/logs/transcript_full.jsonl` is an alternative
  transcript source (per third-party docs). It is deliberately not read here.
- UNVERIFIED: Antigravity publishes no schema. Each `steps.step_payload` is a protobuf
  that was decoded by observation (agy 1.3.1) and checked only against `history.jsonl`
  (typed prompts) and against itself. The field numbers below are that evidence:
    step_type 14  user input         19.2 = prompt text
    step_type 15  assistant turn     20.8 = text (equal to 20.1), repeated 20.7 =
                                     tool call {1 id, 2 name, 3 JSON arguments}
    step_type 8   view_file result   5.4.1 = id of the call it answers, 14.4 = file text
  Every other step type is NOT mapped and is counted in a NOTE. Tool calls of any tool
  come from the type-15 turn (each call id is repeated by exactly one later step), but
  only `view_file` output is mapped; other tools' results sit in per-tool layouts that
  were not validated, so those calls have no result message.
- Timestamps: `steps.metadata` field 1 = {1 unix seconds, 2 nanoseconds}. For user steps
  it equals the `history.jsonl` timestamp to the millisecond.
- Working directory: `trajectory_metadata_blob.data` field 1.1 (else field 7) is a
  `file://` URI of the workspace; `history.jsonl` (`workspace` by `conversationId`) is
  the fallback. The store holds no agent version.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from cadra import adapt, collect
HOST = "antigravity"
USER_INPUT, ASSISTANT_TURN, VIEW_FILE = 14, 15, 8
MAPPED_TYPES = (USER_INPUT, ASSISTANT_TURN, VIEW_FILE)
#: Antigravity's own argument names that hold file system paths.
PATH_KEYS = adapt.PATH_ARG_KEYS + (
    "AbsolutePath", "TargetFile", "DirectoryPath", "SearchPath", "SearchDirectory", "Cwd")
MAX_HISTORY_BYTES = 8 * 1024 * 1024
UNVERIFIED = ("the Antigravity chat format is reverse-engineered and unverified "
              "(no public schema); only user prompts, assistant text, tool calls and "
              "view_file results are read")
def default_root() -> Path:
    home = os.environ.get("ANTIGRAVITY_HOME")
    return Path(home) if home else Path.home() / ".gemini" / "antigravity-cli"
def _fields(buf: bytes) -> list[tuple[int, int | bytes]]:
    """Protobuf wire format -> [(field number, int | bytes)]. ValueError if malformed."""
    out: list[tuple[int, int | bytes]] = []
    i, end = 0, len(buf)
    def varint() -> int:
        nonlocal i
        shift = result = 0
        while True:
            if i >= end or shift > 63:
                raise ValueError("bad varint")
            byte = buf[i]
            i += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
    while i < end:
        key = varint()
        number, kind = key >> 3, key & 7
        if number == 0:
            raise ValueError("field 0")
        if kind == 0:
            out.append((number, varint()))
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            if i + size > end:
                raise ValueError("truncated")
            out.append((number, buf[i:i + size]))
            i += size
        elif kind == 2:
            size = varint()
            if i + size > end:
                raise ValueError("truncated")
            out.append((number, buf[i:i + size]))
            i += size
        else:
            raise ValueError("unsupported wire type")
    return out
def _get(buf: bytes | None, *path: int) -> int | bytes | None:
    """First value at a nested field path; None when absent. ValueError if malformed."""
    value: int | bytes | None = buf
    for number in path:
        if not isinstance(value, bytes):
            return None
        value = next((v for n, v in _fields(value) if n == number), None)
    return value
def _text(value: int | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else ""
def _stamp(metadata: bytes | None) -> str | None:
    """steps.metadata field 1 = {1 unix seconds, 2 nanoseconds} -> ISO-8601 UTC (ms)."""
    try:
        seconds, nanos = _get(metadata, 1, 1), _get(metadata, 1, 2) or 0
        moment = datetime.fromtimestamp(seconds + nanos / 1e9, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
def _connect(path: Path) -> sqlite3.Connection:
    """Read-only AND immutable: SQLite then never touches -wal/-shm sidecars."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
def _uri_to_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return ""
    path = unquote(urlparse(uri).path)
    return path[1:] if re.match(r"^/[A-Za-z]:", path) else path
def _history_workspaces(root: Path) -> dict[str, str]:
    """conversationId -> workspace from history.jsonl (typed prompts only). Bounded."""
    found: dict[str, str] = {}
    try:
        with open(root / "history.jsonl", encoding="utf-8", errors="ignore") as handle:
            lines = handle.read(MAX_HISTORY_BYTES).splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("conversationId"), str) \
                and isinstance(entry.get("workspace"), str) and entry["workspace"]:
            found.setdefault(entry["conversationId"], entry["workspace"])
    return found
def _origin_cwd(conn: sqlite3.Connection) -> str:
    row = conn.execute("select data from trajectory_metadata_blob limit 1").fetchone()
    try:
        for field in ((1, 1), (7,)):
            path = _uri_to_path(_text(_get(row[0] if row else None, *field)))
            if path:
                return path
    except ValueError:
        pass
    return ""
def discover(root: Path, workspace: Path) -> tuple[list[collect.Session], list[str]]:
    sessions: list[collect.Session] = []
    notes: list[str] = []
    folder = root / "conversations"
    if not folder.is_dir():
        return sessions, notes
    unsupported = len(list(folder.glob("*.pb")))
    if unsupported:
        notes.append(f"antigravity: {unsupported} conversation(s) stored as .pb files "
                     "are not supported — not read")
    history: dict[str, str] | None = None
    for path in sorted(folder.glob("*.db")):
        short = f"antigravity {path.stem[:8]}"
        try:
            conn = _connect(path)
            try:
                cwd = _origin_cwd(conn)
                counts = conn.execute(
                    "select step_type, count(*) from steps group by step_type").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            notes.append(f"{short}: conversation could not be read — skipped")
            continue
        if not cwd:
            history = _history_workspaces(root) if history is None else history
            cwd = history.get(path.stem, "")
        if not cwd:
            notes.append(f"{short}: no working directory recorded — skipped")
            continue
        if not collect.in_scope(cwd, workspace):
            notes.append(f"{path.stem[:8]}: started elsewhere ({cwd}) — not included")
            continue
        skipped = {t: n for t, n in counts if t not in MAPPED_TYPES}
        detail = ", ".join(f"type {t} x{n}" for t, n in sorted(skipped.items()))
        notes.append(f"{path.stem[:8]}: {sum(skipped.values())} of "
                     f"{sum(n for _, n in counts)} step(s) not carried over"
                     + (f" ({detail})" if detail else ""))
        sessions.append(collect.Session(session_id=path.stem, main_file=path,
                                        origin_cwd=cwd, host=HOST))
    if sessions:
        notes.append(f"antigravity: {UNVERIFIED}")
    return sessions, notes
def _arguments(raw: bytes, workspace: Path) -> dict:
    args = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(args, dict):
        raise ValueError("arguments are not an object")
    for key in PATH_KEYS:
        if isinstance(args.get(key), str):
            args[key] = adapt.rebase_path(args[key], workspace)
    return args
def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    messages: list[dict] = []
    stamps: list[str] = []
    ids: dict[str, str] = {}  # id written by Antigravity -> generated id
    unreadable = issued = 0
    try:
        conn = _connect(session.main_file)
        try:
            rows = conn.execute("select step_type, metadata, step_payload from steps "
                                "order by idx").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        rows, unreadable = [], 1
    for step_type, metadata, payload in rows:
        if step_type not in MAPPED_TYPES:
            continue
        stamp = _stamp(metadata)
        if stamp:
            stamps.append(stamp)
        stamped = {"client_ts": stamp} if stamp else {}
        try:
            if step_type == USER_INPUT:
                text = _text(_get(payload, 19, 2))
                if text:
                    messages.append({"role": "user", "content": text, **stamped})
            elif step_type == ASSISTANT_TURN:
                body = _get(payload, 20)
                text = _text(_get(body, 8) or _get(body, 1))
                calls = []
                for number, raw in _fields(body) if isinstance(body, bytes) else []:
                    if number != 7 or not isinstance(raw, bytes):
                        continue
                    generated = f"antigravity-{issued}"
                    issued += 1
                    ids[_text(_get(raw, 1))] = generated
                    calls.append({"id": generated, "type": "function", "function": {
                        "name": _text(_get(raw, 2)),
                        "arguments": _arguments(_get(raw, 3) or b"", workspace)}})
                message: dict = {"role": "assistant"}
                if text:
                    message["content"] = text
                if calls:
                    message["tool_calls"] = calls
                if len(message) > 1:
                    messages.append({**message, **stamped})
            else:  # VIEW_FILE: the file text answers the call named in 5.4.1
                call = ids.get(_text(_get(payload, 5, 4, 1)))
                text = _text(_get(payload, 14, 4))
                if call and text:
                    messages.append({"role": "tool", "tool_call_id": call,
                                     "content": text, **stamped})
        except ValueError:  # malformed protobuf or arguments JSON
            unreadable += 1
    meta = {"cwds": [session.origin_cwd],
            "started_at": min(stamps) if stamps else None,
            "ended_at": max(stamps) if stamps else None,
            "agent_version": "", "unreadable_lines": unreadable}
    return messages, meta
