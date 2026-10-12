"""Cursor CLI reader. Hermetic: synthetic trees under tmp_path only."""
import json
from pathlib import Path
import cadra_submit
from cadra import collect, cursor
from tests.conftest import CURSOR_UUID, write_jsonl
def test_session_found_via_meta_cwd(cursor_root: Path, workspace: Path):
    sessions, notes = cursor.discover(cursor_root, workspace)
    assert [(s.session_id, s.host, s.origin_cwd) for s in sessions] == [
        (CURSOR_UUID, "cursor", str(workspace))]
    assert any("does not record tool outputs" in n for n in notes)
def test_sibling_folder_cwd_is_excluded(cursor_root: Path, workspace: Path):
    sibling = workspace.parent / "solution-other"
    sibling.mkdir()
    sessions, notes = cursor.discover(cursor_root, sibling)
    assert sessions == []
    assert any("started elsewhere" in n for n in notes)
def test_transcript_without_meta_is_skipped_with_note(cursor_root: Path, workspace: Path):
    other = "00000000-0000-4000-8000-0000000000c2"
    write_jsonl(cursor_root / "projects" / "slug-a" / "agent-transcripts" / f"{other}.jsonl",
                [{"role": "user", "message": {"content": [{"type": "text", "text": "x"}]}}])
    sessions, notes = cursor.discover(cursor_root, workspace)
    assert [s.session_id for s in sessions] == [CURSOR_UUID]
    assert any(n.startswith("cursor 00000000") and "skipped" in n for n in notes)
def test_messages_tool_calls_and_rebase(cursor_root: Path, workspace: Path):
    session = cursor.discover(cursor_root, workspace)[0][0]
    messages, meta = cursor.load_session(session, workspace)
    assert messages[0] == {"role": "user", "content": "add a readme"}
    assert messages[1] == {"role": "assistant", "content": "Creating it."}
    write, shell = (m["tool_calls"][0] for m in messages[2:])
    assert (write["id"], shell["id"]) == ("cursor-0", "cursor-1")
    assert write["function"]["name"] == "Write"
    assert write["function"]["arguments"]["path"] == "README.md"
    assert shell["function"]["arguments"]["command"] == "ls"
    assert len(messages) == 4  # turn_ended control line skipped
    assert not any("client_ts" in m for m in messages)
    assert meta["cwds"] == [str(workspace)]
    assert meta["started_at"] == "2026-08-11T09:10:00.000Z"
    assert meta["ended_at"] == "2026-08-11T09:11:00.000Z"
    assert meta["agent_version"] == "" and meta["unreadable_lines"] == 0
def test_store_db_and_prompt_history_never_opened(cursor_root, workspace, monkeypatch):
    chat = cursor_root / "chats" / "h1" / CURSOR_UUID
    (chat / "prompt_history.json").write_text("[]", encoding="utf-8")
    opened: list[str] = []
    real_open = cursor.open_text
    def _spy(path, *a, **kw):
        opened.append(Path(path).name)
        return real_open(path, *a, **kw)
    monkeypatch.setattr(cursor, "open_text", _spy)
    session = cursor.discover(cursor_root, workspace)[0][0]
    cursor.load_session(session, workspace)
    assert opened and set(opened) <= {"meta.json", f"{CURSOR_UUID}.jsonl"}
def test_unparseable_lines_are_counted(cursor_root: Path, workspace: Path):
    session = cursor.discover(cursor_root, workspace)[0][0]
    with open(session.main_file, "a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    assert cursor.load_session(session, workspace)[1]["unreadable_lines"] == 1
def test_default_root_honours_cursor_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path / "ch"))
    assert cursor.default_root() == tmp_path / "ch"
    monkeypatch.delenv("CURSOR_HOME")
    assert cursor.default_root() == Path.home() / ".cursor"
def test_host_flag_is_wired(cursor_root: Path, workspace: Path, capsys):
    from cadra import config
    config.save(workspace, {"token": "t", "workspace_root": str(workspace),
                            "proxy_base_url": "https://proxy.test",
                            "git_remote": "https://github.com/c/s.git"})
    code = cadra_submit.main(["--workspace", str(workspace), "--host", "cursor",
                              "--cursor-root", str(cursor_root), "--dry-run"])
    assert code == 0
    assert "FOUND 1" in capsys.readouterr().out
    preview = json.loads((workspace / ".cadra" / "last-preview.json").read_text())
    assert preview["sessions"][0]["host"] == "cursor"
