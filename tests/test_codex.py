"""Codex reader: host-injected user text and the shell / web-search item kinds.
Hermetic: a synthetic rollout with the real line shape (codex rust-v0.164.0)."""
import json
from pathlib import Path

from cadra import codex


def _rollout(root: Path, workspace: Path, items: list[dict]) -> None:
    path = root / "2026" / "10" / "26" / "rollout-2026-10-26T09-00-00-abc.jsonl"
    path.parent.mkdir(parents=True)
    meta = {"timestamp": "2026-10-26T09:00:00Z", "type": "session_meta", "payload": {
        "id": "00000000-0000-4000-8000-0000000000c1", "cwd": str(workspace),
        "cli_version": "0.164.0", "originator": "codex_cli"}}
    lines = [meta] + [{"timestamp": f"2026-10-26T09:00:{i + 1:02d}Z", "type": "response_item",
                       "payload": item} for i, item in enumerate(items)]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _load(root: Path, workspace: Path) -> list[dict]:
    sessions, _notes = codex.discover(root, workspace)
    assert len(sessions) == 1
    return codex.load_session(sessions[0], workspace)[0]


def _user(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def test_injected_skill_and_notice_text_is_not_the_candidate(tmp_path: Path, workspace: Path):
    # Codex writes the whole SKILL.md into the transcript as a *user* message
    # wrapped in <skill>; the candidate's own words are the separate line before it.
    _rollout(tmp_path, workspace, [
        _user("$cadra-trace-tracker:cadra-connect"),
        _user("<skill>\n<name>cadra-trace-tracker:cadra-connect</name>\n<path>/x</path>"
              "\n# Skill: cadra-connect ...\n</skill>"),
        _user("<user_shell_command>ls</user_shell_command>"),
        _user("<current_time_reminder>now</current_time_reminder>"),
        _user("<plugin>\n<name>x</name>\n</plugin>"),
        _user("<subagent_notification>done</subagent_notification>"),
        _user("real follow-up"),
    ])
    messages = _load(tmp_path, workspace)
    assert [m["content"] for m in messages] == ["$cadra-trace-tracker:cadra-connect",
                                                "real follow-up"]


def test_local_shell_and_web_search_calls_are_kept(tmp_path: Path, workspace: Path):
    _rollout(tmp_path, workspace, [
        {"type": "local_shell_call", "id": "lsh_1", "call_id": "call_1", "status": "completed",
         "action": {"type": "exec", "command": ["ls", "-la"],
                    "working_directory": str(workspace / "src"), "timeout_ms": 1000}},
        {"type": "local_shell_call_output", "call_id": "call_1", "output": "a.py\n"},
        {"type": "web_search_call", "id": "ws_1", "status": "completed",
         "action": {"type": "search", "query": "pandas merge"}},
    ])
    messages = _load(tmp_path, workspace)
    assert messages[0]["tool_calls"] == [{"id": "call_1", "type": "function", "function": {
        "name": "local_shell", "arguments": {"command": ["ls", "-la"],
                                             "working_directory": "src"}}}]
    assert messages[1] == {"role": "tool", "tool_call_id": "call_1", "content": "a.py\n",
                           "client_ts": "2026-10-26T09:00:02Z"}
    assert messages[2]["tool_calls"] == [{"id": "ws_1", "type": "function", "function": {
        "name": "web_search", "arguments": {"type": "search", "query": "pandas merge"}}}]
    assert len(messages) == 3


def test_archived_threads_are_still_collected(tmp_path: Path, workspace: Path):
    # `codex` moves an archived thread's rollout to ~/.codex/archived_sessions/.
    (tmp_path / "sessions").mkdir()
    _rollout(tmp_path / "archived_sessions", workspace, [_user("archived work")])
    assert [m["content"] for m in _load(tmp_path / "sessions", workspace)] == ["archived work"]


def test_sub_agent_rollouts_keep_their_own_thread_id(tmp_path: Path, workspace: Path):
    # Sub-agent rollouts carry the ROOT thread in `session_id`; keying on it would
    # make two files collide on one state entry.
    root = tmp_path / "sessions"
    for i, (tid, sid) in enumerate([("t-root", "t-root"), ("t-child", "t-root")]):
        path = root / "2026" / "10" / "26" / f"rollout-{i}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"timestamp": "2026-10-26T09:00:00Z", "type": "session_meta",
                                    "payload": {"id": tid, "session_id": sid,
                                                "cwd": str(workspace)}}) + "\n")
    sessions, _ = codex.discover(root, workspace)
    assert sorted(s.session_id for s in sessions) == ["t-child", "t-root"]


def test_archives_are_only_looked_up_beside_the_real_store(tmp_path: Path, workspace: Path):
    (tmp_path / "custom").mkdir()
    _rollout(tmp_path / "archived_sessions", workspace, [_user("archived")])
    assert codex.discover(tmp_path / "custom", workspace) == ([], [])
