"""Antigravity CLI reader. Hermetic: synthetic SQLite + protobuf under tmp_path only."""
import json
from pathlib import Path
import cadra_submit
from cadra import antigravity, config
from tests.conftest import AGY_ID, agy_step, make_agy_db, pb


def _db(root: Path, name: str = AGY_ID) -> Path:
    return root / "conversations" / f"{name}.db"


def test_session_found_via_trajectory_cwd(agy_root: Path, workspace: Path):
    sessions, notes = antigravity.discover(agy_root, workspace)
    assert [(s.session_id, s.host, s.origin_cwd) for s in sessions] == [
        (AGY_ID, "antigravity", str(workspace))]
    assert any("reverse-engineered and unverified" in n for n in notes)


def test_sibling_folder_cwd_is_excluded(agy_root: Path, workspace: Path):
    sibling = workspace.parent / "solution-other"
    sibling.mkdir()
    sessions, notes = antigravity.discover(agy_root, sibling)
    assert sessions == []
    assert any("started elsewhere" in n for n in notes)


def test_no_cwd_is_skipped_with_note(agy_root: Path, workspace: Path):
    other = "00000000-0000-4000-8000-0000000000a2"
    make_agy_db(_db(agy_root, other), [agy_step(14, pb(19, pb(2, "x")))], None)
    sessions, notes = antigravity.discover(agy_root, workspace)
    assert [s.session_id for s in sessions] == [AGY_ID]
    assert any(n.startswith("antigravity 00000000") and "no working directory" in n
               for n in notes)


def test_history_jsonl_is_the_cwd_fallback(agy_root: Path, workspace: Path):
    other = "00000000-0000-4000-8000-0000000000a3"
    make_agy_db(_db(agy_root, other), [agy_step(14, pb(19, pb(2, "x")))], None)
    (agy_root / "history.jsonl").write_text(json.dumps(
        {"display": "x", "timestamp": 1, "workspace": str(workspace),
         "conversationId": other}) + "\n", encoding="utf-8")
    sessions, _notes = antigravity.discover(agy_root, workspace)
    assert {s.session_id for s in sessions} == {AGY_ID, other}


def test_messages_tool_calls_results_and_rebase(agy_root: Path, workspace: Path):
    session = antigravity.discover(agy_root, workspace)[0][0]
    messages, meta = antigravity.load_session(session, workspace)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[0]["content"] == "add a readme"
    assert messages[1]["content"] == "Creating it."
    write, view = messages[1]["tool_calls"]
    assert (write["id"], view["id"]) == ("antigravity-0", "antigravity-1")
    assert write["function"]["name"] == "write_to_file"
    assert write["function"]["arguments"] == {"TargetFile": "README.md"}
    assert view["function"]["arguments"] == {"AbsolutePath": "README.md"}
    assert messages[2]["tool_call_id"] == "antigravity-1"
    assert messages[2]["content"] == "# hi\n"
    assert messages[0]["client_ts"] == "2026-08-06T07:06:40.250Z"
    assert meta["cwds"] == [str(workspace)]
    assert meta["started_at"] == "2026-08-06T07:06:40.250Z"
    assert meta["ended_at"] == "2026-08-06T07:07:00.250Z"
    assert meta["agent_version"] == "" and meta["unreadable_lines"] == 0


def test_unmapped_steps_are_counted_in_a_note(agy_root: Path, workspace: Path):
    notes = antigravity.discover(agy_root, workspace)[1]
    assert any("2 of 5 step(s) not carried over (type 5 x1, type 98 x1)" in n for n in notes)


def test_pb_conversations_are_counted_not_read(agy_root: Path, workspace: Path):
    for name in ("a.pb", "b.pb"):
        (agy_root / "conversations" / name).write_bytes(b"\x08\x01")
    sessions, notes = antigravity.discover(agy_root, workspace)
    assert len(sessions) == 1
    assert [n for n in notes if ".pb" in n] == [
        "antigravity: 2 conversation(s) stored as .pb files are not supported — not read"]


def test_database_is_opened_read_only_without_sidecars(tmp_path: Path, workspace: Path):
    root = tmp_path / "agy-wal"
    make_agy_db(_db(root), [agy_step(14, pb(19, pb(2, "hi")))], workspace, wal=True)
    before = sorted(p.name for p in (root / "conversations").iterdir())
    session = antigravity.discover(root, workspace)[0][0]
    antigravity.load_session(session, workspace)
    assert sorted(p.name for p in (root / "conversations").iterdir()) == before
    assert not any(n.endswith(("-wal", "-shm")) for n in before)


def test_malformed_payload_is_counted_not_fatal(agy_root: Path, workspace: Path):
    make_agy_db(_db(agy_root), [agy_step(14, b"\x0a\xff"), agy_step(15, pb(20, b"\x0f"))],
                workspace)
    session = antigravity.discover(agy_root, workspace)[0][0]
    messages, meta = antigravity.load_session(session, workspace)
    assert messages == [] and meta["unreadable_lines"] == 2


def test_unreadable_database_is_skipped_with_note(agy_root: Path, workspace: Path):
    _db(agy_root, "00000000-0000-4000-8000-0000000000a4").write_bytes(b"not a database")
    sessions, notes = antigravity.discover(agy_root, workspace)
    assert len(sessions) == 1
    assert any("could not be read — skipped" in n for n in notes)


def test_default_root_honours_antigravity_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_HOME", str(tmp_path / "ah"))
    assert antigravity.default_root() == tmp_path / "ah"
    monkeypatch.delenv("ANTIGRAVITY_HOME")
    assert antigravity.default_root() == Path.home() / ".gemini" / "antigravity-cli"


def test_host_flag_is_wired(agy_root: Path, workspace: Path, capsys):
    config.save(workspace, {"token": "t", "workspace_root": str(workspace),
                            "proxy_base_url": "https://proxy.test",
                            "git_remote": "https://github.com/c/s.git"})
    code = cadra_submit.main(["--workspace", str(workspace), "--host", "antigravity",
                              "--antigravity-root", str(agy_root), "--dry-run"])
    assert code == 0
    assert "FOUND 1" in capsys.readouterr().out
    preview = json.loads((workspace / ".cadra" / "last-preview.json").read_text())
    assert preview["sessions"][0]["host"] == "antigravity"
    assert preview["sessions"][0]["message_count"] == 3
