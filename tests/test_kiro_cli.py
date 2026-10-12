"""Kiro v3 `sessions/cli/` layout and the v1/v2 database note. All content synthetic."""
import json
import sqlite3
from pathlib import Path
import pytest
from cadra import kiro
def _block(kind, data):
    return {"kind": kind, "data": data}
def _write(root: Path, sid: str, cwd: str) -> None:
    cli = root / "cli"
    cli.mkdir(parents=True, exist_ok=True)
    (cli / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "cwd": cwd, "created_at": "2026-08-11T09:00:00.000000Z",
        "updated_at": "2026-08-11T09:30:00.000000Z", "session_state": {"x": 1}}))
    (cli / f"{sid}.history").write_text("never read\n")
    records = [
        {"version": "v1", "kind": "Prompt", "data": {
            "content": [_block("text", "add a readme")], "meta": {"timestamp": 1786439400}}},
        {"version": "v1", "kind": "AssistantMessage", "data": {"content": [
            _block("thinking", {"text": "secret thoughts"}),
            _block("text", "Creating it."),
            _block("toolUse", {"toolUseId": "t1", "name": "write", "input": {
                "__tool_use_purpose": "why", "path": f"{cwd}/README.md",
                "working_dir": f"{cwd}/sub"}}),
            _block("text", ""), _block("mystery", {})]}},
        {"version": "v1", "kind": "ToolResults", "data": {"content": [
            _block("toolResult", {"toolUseId": "t1", "status": "success", "content": [
                _block("text", "ok"), _block("json", {"n": 1})]})]}},
        {"version": "v1", "kind": "Clear"},
        {"version": "v1", "kind": "Weird"},
    ]
    (cli / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
def test_cli_layout_scoped_and_mapped(tmp_path: Path, workspace: Path):
    root = tmp_path / "sessions"
    _write(root, "c1", str(workspace))
    _write(root, "c2", str(tmp_path / "elsewhere"))
    sessions, notes = kiro.discover(root, workspace)
    assert [(s.session_id, s.host) for s in sessions] == [("c1", "kiro")]
    assert any("started elsewhere" in n for n in notes)
    messages, meta = kiro.load_session(sessions[0], workspace)
    assert messages[0] == {"role": "user", "content": "add a readme",
                           "client_ts": "2026-08-11T09:10:00.000Z"}
    assert messages[1] == {"role": "assistant", "content": "Creating it."}
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == {
        "path": "README.md", "working_dir": f"{workspace}/sub"}
    assert messages[3] == {"role": "tool", "tool_call_id": "t1",
                           "content": 'ok\n{"n": 1}'}
    assert len(messages) == 4
    assert "secret thoughts" not in json.dumps(messages)
    assert meta["unmapped_parts"] == {"AssistantMessage.mystery": 1, "Weird": 1}
    assert meta["started_at"] == "2026-08-11T09:00:00.000000Z"
    assert str(workspace / "sub") in meta["cwds"]
def test_both_layouts_dedupe(kiro_root: Path, workspace: Path):
    _write(kiro_root, "s1", str(workspace))  # same id as the fixture's session
    _write(kiro_root, "c9", str(workspace))
    ids = sorted(s.session_id for s in kiro.discover(kiro_root, workspace)[0])
    assert ids.count("s1") == 1 and "c9" in ids
def test_history_and_lock_files_not_opened(tmp_path: Path, workspace: Path, monkeypatch):
    root = tmp_path / "sessions"
    _write(root, "c1", str(workspace))
    opened: list[str] = []
    real = open
    monkeypatch.setattr(kiro, "open_text", lambda p, *a, **k: (opened.append(str(p)),
                                                               real(p, *a, **k))[1])
    session = kiro.discover(root, workspace)[0][0]
    kiro.load_session(session, workspace)
    assert opened and not any(p.endswith((".history", ".lock")) for p in opened)
def test_kiro_home_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "k"))
    assert kiro.default_root() == tmp_path / "k" / "sessions"
def _legacy_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE conversations (key TEXT PRIMARY KEY, value TEXT);"
                       "CREATE TABLE conversations_v2 (key TEXT, conversation_id TEXT,"
                       " value TEXT, created_at INT, updated_at INT);"
                       "CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT);")
    for i in range(rows):
        conn.execute("INSERT INTO conversations VALUES (?, '{}')", (f"/d{i}",))
    conn.commit()
    conn.close()
@pytest.mark.parametrize("rel", ["Library/Application Support/kiro-cli",
                                 ".local/share/kiro-cli"])
def test_legacy_db_note_only_with_rows(tmp_path: Path, workspace: Path, monkeypatch, rel):
    home = tmp_path / "fakehome"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _legacy_db(home / rel / "data.sqlite3", rows=0)
    assert kiro.discover(kiro.default_root(), workspace)[1] == []
    conn = sqlite3.connect(home / rel / "data.sqlite3")
    conn.execute("INSERT INTO conversations VALUES ('/d', '{}')")
    conn.commit()
    conn.close()
    notes = kiro.discover(kiro.default_root(), workspace)[1]
    assert notes == ["kiro: older Kiro database found (v1/v2 data.sqlite3), not read"]
    # an explicit root never touches the home db
    assert kiro.discover(tmp_path / "other", workspace)[1] == []
def test_legacy_db_windows_appdata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert tmp_path / "Roaming" / "kiro-cli" / "data.sqlite3" in kiro.legacy_db_candidates()
