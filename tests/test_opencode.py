"""OpenCode reader. Hermetic: a synthetic SQLite db with the real table layout."""
import json
import sqlite3
from pathlib import Path

import pytest

import cadra_submit
from cadra import opencode


def _db(root: Path, workspace: Path) -> None:
    root.mkdir(parents=True)
    conn = sqlite3.connect(root / "opencode.db")
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
            directory TEXT, version TEXT, time_created INT, time_updated INT);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INT,
            time_updated INT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INT, time_updated INT, data TEXT);
    """)
    ws = str(workspace)
    sess = [("ses_1", None, ws), ("ses_sub", "ses_1", ws),
            ("ses_out", None, str(workspace.parent / "elsewhere"))]
    for sid, parent, cwd in sess:
        conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                     (sid, "p", parent, cwd, "1.0.0", 1786439400000, 1786439460000))
    parts = [
        ("msg_1", "user", [{"type": "text", "text": "add a readme"},
                           {"type": "text", "text": "injected", "synthetic": True}]),
        ("msg_2", "assistant", [
            {"type": "step-start"}, {"type": "reasoning", "text": "secret thoughts"},
            {"type": "text", "text": "Creating it."},
            {"type": "tool", "callID": "c1", "tool": "write", "state": {
                "status": "completed", "input": {"filePath": f"{ws}/README.md"},
                "output": "ok"}},
            {"type": "tool", "callID": "c2", "tool": "bash", "state": {
                "status": "error", "input": {"command": "ls"}, "error": "boom"}},
            {"type": "patch"}, {"type": "patch"}]),
    ]
    n = 0
    for mid, role, items in parts:
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     (mid, "ses_1", n, n, json.dumps({"role": role})))
        for item in items:
            n += 1
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                         (f"prt_{n:03d}", mid, "ses_1", n, n, json.dumps(item)))
    conn.commit()
    conn.close()


def _db_v2(root: Path, workspace: Path) -> None:
    """OpenCode 2.x: sessions in `session_v2`, one `session_message` row per turn.
    An upgraded store keeps the empty 1.x `message`/`part` tables beside them."""
    root.mkdir(parents=True)
    conn = sqlite3.connect(root / "opencode.db")
    conn.executescript("""
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
            directory TEXT, version TEXT, time_created INT, time_updated INT);
        CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
            seq INT, time_created INT, time_updated INT, data TEXT);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, data TEXT);
    """)
    ws = str(workspace)
    for sid, parent, cwd in [("ses_1", None, ws), ("ses_sub", "ses_1", ws),
                             ("ses_out", None, str(workspace.parent / "elsewhere"))]:
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
                     (sid, "p", parent, cwd, "2.1.4", 1786439400000, 1786439460000))
    rows = [
        {"type": "user", "text": "add a readme"},
        {"type": "synthetic", "text": "injected"},
        {"type": "assistant", "content": [
            {"type": "reasoning", "text": "secret thoughts"},
            {"type": "text", "text": "Creating it."},
            {"type": "tool", "id": "c1", "name": "write", "state": {
                "status": "completed", "input": {"filePath": f"{ws}/README.md"},
                "content": [{"type": "text", "text": "ok"},
                            {"type": "file", "uri": "file:///x.png", "mime": "image/png"}]}},
            {"type": "tool", "id": "c2", "name": "bash", "state": {
                "status": "error", "input": {"command": "ls"},
                "error": {"name": "ToolError", "message": "boom"}}}]},
        {"type": "system", "text": "tools changed"},
        {"type": "idle"},
    ]
    for seq, data in enumerate(rows):
        conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                     (f"msg_{seq:03d}", "ses_1", data["type"], seq, seq, seq,
                      json.dumps(data)))
    conn.commit()
    conn.close()


@pytest.fixture
def oc_root_v2(tmp_path: Path, workspace: Path) -> Path:
    root = tmp_path / "opencode"
    _db_v2(root, workspace)
    return root


def test_v2_store_is_discovered_and_read(oc_root_v2: Path, workspace: Path):
    # OpenCode 2.x (what `brew install opencode` gives since 1.19): no `session`
    # table, so the 1.x query raised "no such table" and the submit said FOUND 0.
    sessions, notes = opencode.discover(oc_root_v2, workspace)
    assert [(s.session_id, s.origin_cwd) for s in sessions] == [("ses_1", str(workspace))]
    assert any("1 sub-agent" in n for n in notes) and not any("skipped" in n for n in notes)
    messages, meta = opencode.load_session(sessions[0], workspace)
    assert messages[0] == {"role": "user", "content": "add a readme"}
    assert messages[1] == {"role": "assistant", "content": "Creating it."}
    assert messages[2]["tool_calls"][0] == {"id": "c1", "type": "function", "function": {
        "name": "write", "arguments": {"filePath": "README.md"}}}
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "ok\n[file: x.png]"}
    assert messages[5] == {"role": "tool", "tool_call_id": "c2", "content": "boom"}
    assert len(messages) == 6
    assert "secret thoughts" not in json.dumps(messages)
    assert "injected" not in json.dumps(messages)
    assert meta["unmapped_parts"] == {"system": 1, "idle": 1}
    assert meta["agent_version"] == "2.1.4"
    assert meta["started_at"] == "2026-08-11T09:10:00.000Z"


@pytest.fixture
def oc_root(tmp_path: Path, workspace: Path) -> Path:
    root = tmp_path / "opencode"
    _db(root, workspace)
    return root


def test_discover_scopes_and_skips_subagents(oc_root: Path, workspace: Path):
    sessions, notes = opencode.discover(oc_root, workspace)
    assert [(s.session_id, s.host, s.origin_cwd) for s in sessions] == [
        ("ses_1", "opencode", str(workspace))]
    assert any("1 sub-agent" in n for n in notes)


def test_messages_tools_and_drops(oc_root: Path, workspace: Path):
    session = opencode.discover(oc_root, workspace)[0][0]
    messages, meta = opencode.load_session(session, workspace)
    assert messages[0] == {"role": "user", "content": "add a readme"}
    assert messages[1] == {"role": "assistant", "content": "Creating it."}
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == {"filePath": "README.md"}
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert messages[5] == {"role": "tool", "tool_call_id": "c2", "content": "boom"}
    assert len(messages) == 6
    assert "secret thoughts" not in json.dumps(messages)
    assert meta["unmapped_parts"] == {"patch": 2}
    assert meta["agent_version"] == "1.0.0"
    assert meta["started_at"] == "2026-08-11T09:10:00.000Z"


def test_db_opened_read_only(oc_root: Path, workspace: Path):
    session = opencode.discover(oc_root, workspace)[0][0]
    opencode.load_session(session, workspace)
    assert not list(oc_root.glob("*-wal")) and not list(oc_root.glob("*-shm"))


def test_rows_only_in_the_wal_are_read(tmp_path: Path, workspace: Path):
    # A fresh OpenCode install leaves opencode.db as a bare header and every row in
    # opencode.db-wal (no checkpoint on exit); immutable reads saw "no such table".
    _db(tmp_path / "src", workspace)
    root = tmp_path / "opencode"
    root.mkdir()
    writer = sqlite3.connect(root / "opencode.db")
    writer.execute("pragma journal_mode=wal")
    writer.execute("pragma wal_autocheckpoint=0")
    writer.execute("attach ? as src", (str(tmp_path / "src" / "opencode.db"),))
    for table in ("session", "message", "part"):
        writer.execute(f"create table main.{table} as select * from src.{table}")
    writer.commit()
    writer.execute("detach src")
    try:
        sessions, notes = opencode.discover(root, workspace)
        assert [s.session_id for s in sessions] == ["ses_1"], notes
        assert len(opencode.load_session(sessions[0], workspace)[0]) == 6
    finally:
        writer.close()


def test_missing_store_and_legacy_layout(tmp_path: Path, workspace: Path):
    assert opencode.discover(tmp_path / "none", workspace) == ([], [])
    (tmp_path / "old" / "storage" / "session").mkdir(parents=True)
    sessions, notes = opencode.discover(tmp_path / "old", workspace)
    assert sessions == [] and any("older JSON" in n for n in notes)


def test_default_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "oc"))
    assert opencode.default_root() == tmp_path / "oc"
    monkeypatch.delenv("OPENCODE_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "x"))
    assert opencode.default_root() == tmp_path / "x" / "opencode"


def test_registered_host():
    assert "opencode" in cadra_submit.HOSTS
def _add_db(root: Path, name: str, workspace: Path, sid: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / name)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
            directory TEXT, version TEXT, time_created INT, time_updated INT);
    """)
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                 (sid, "p", None, str(workspace), "1.0.0", 1, 2))
    conn.commit()
    conn.close()
    return root / name
def test_channel_dbs_read_and_deduped(tmp_path: Path, workspace: Path):
    root = tmp_path / "oc"
    _add_db(root, "opencode.db", workspace, "a")
    _add_db(root, "opencode-local.db", workspace, "b")
    _add_db(root, "opencode-beta.db", workspace, "a")  # same id: read once
    sessions, _ = opencode.discover(root, workspace)
    assert [s.session_id for s in sessions] == ["a", "b"]
    assert [s.main_file.name for s in sessions] == ["opencode.db", "opencode-local.db"]
def test_opencode_db_env_absolute_and_relative(tmp_path: Path, workspace: Path, monkeypatch):
    root = tmp_path / "oc"
    _add_db(root, "opencode.db", workspace, "default")
    other = _add_db(tmp_path / "elsewhere", "mine.db", workspace, "abs")
    _add_db(root, "custom.db", workspace, "rel")
    monkeypatch.setenv("OPENCODE_DB", str(other))
    assert [s.session_id for s in opencode.discover(root, workspace)[0]] == ["abs"]
    monkeypatch.setenv("OPENCODE_DB", "custom.db")
    assert [s.session_id for s in opencode.discover(root, workspace)[0]] == ["rel"]
    monkeypatch.setenv("OPENCODE_DB", "missing.db")
    assert opencode.discover(root, workspace) == ([], [])
def test_windows_localappdata_fallback_only_when_default_absent(
        tmp_path: Path, workspace: Path, monkeypatch):
    home = tmp_path / "winhome"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    default = home / ".local" / "share" / "opencode"
    assert opencode.default_root() == default  # neither exists
    _add_db(local / "opencode", "opencode.db", workspace, "w")
    assert opencode.default_root() == local / "opencode"
    _add_db(default, "opencode-dev.db", workspace, "d")  # default now has a db
    assert opencode.default_root() == default
