"""Claude Code reader: discovery and scoping (§6.1, §6.1.1) and the JSONL ->
canonical conversion (§7). Each discovery case here was a real bug."""
import json
from pathlib import Path

from cadra import claude
from tests.conftest import write_jsonl


def _entry(cwd: str, sid: str = "s1", **over):
    entry = {"type": "user", "cwd": cwd, "sessionId": sid,
             "timestamp": "2026-08-11T09:00:00Z",
             "message": {"role": "user", "content": "hi"}}
    entry.update(over)
    return entry


def test_encoded_name_matches_claude_code(tmp_path: Path):
    assert claude.encode_dir_name(Path("C:/Dev/ws")) == "C--Dev-ws"


def test_session_in_the_workspace_is_found(transcripts: Path, workspace: Path):
    enc = claude.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s1.jsonl", [_entry(str(workspace))])
    sessions, _notes = claude.discover(transcripts, workspace)
    assert [s.session_id for s in sessions] == ["s1"]


def test_session_started_in_a_subdirectory_is_found(transcripts: Path, workspace: Path):
    """Claude Code puts a subdirectory cwd in a SEPARATE project folder."""
    sub = workspace / "backend"
    enc = claude.encode_dir_name(sub)
    write_jsonl(transcripts / enc / "s2.jsonl", [_entry(str(sub), "s2")])
    sessions, _notes = claude.discover(transcripts, workspace)
    assert [s.session_id for s in sessions] == ["s2"]


def test_sibling_directory_sharing_the_encoded_prefix_is_excluded(
    transcripts: Path, workspace: Path
):
    """C:/Dev/solution-other encodes to a prefix-matching name but is NOT in scope."""
    sibling = workspace.parent / (workspace.name + "-other")
    enc = claude.encode_dir_name(sibling)
    write_jsonl(transcripts / enc / "s3.jsonl", [_entry(str(sibling), "s3")])
    sessions, _notes = claude.discover(transcripts, workspace)
    assert sessions == []


def test_session_started_above_the_workspace_is_excluded_without_opening_its_folder(
    transcripts: Path, workspace: Path, monkeypatch
):
    """A session started one level up is out of scope (§6.1) and we do NOT reach
    up to find it: ancestor folders climb to the candidate's home directory."""
    parent = workspace.parent
    enc = claude.encode_dir_name(parent)
    write_jsonl(transcripts / enc / "s4.jsonl",
                [_entry(str(parent), "s4"), _entry(str(workspace), "s4")])

    opened: list[str] = []
    real_open = claude.open_text
    monkeypatch.setattr(
        claude, "open_text",
        lambda path, *a, **kw: (opened.append(str(path)), real_open(path, *a, **kw))[1],
    )
    sessions, _notes = claude.discover(transcripts, workspace)
    assert sessions == []
    assert not any("s4.jsonl" in path for path in opened)


def test_sibling_in_a_candidate_folder_is_excluded_and_reported(
    transcripts: Path, workspace: Path
):
    """Folders we do open but reject are reported, never silently dropped."""
    sibling = workspace.parent / (workspace.name + "-other")
    enc = claude.encode_dir_name(sibling)
    write_jsonl(transcripts / enc / "s4b.jsonl", [_entry(str(sibling), "s4b")])
    sessions, notes = claude.discover(transcripts, workspace)
    assert sessions == []
    assert any("started elsewhere" in note for note in notes)


def test_session_leaving_the_workspace_is_still_captured_whole(
    transcripts: Path, workspace: Path
):
    enc = claude.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s5.jsonl",
                [_entry(str(workspace), "s5"), _entry("C:/Elsewhere", "s5")])
    sessions, _notes = claude.discover(transcripts, workspace)
    assert [s.session_id for s in sessions] == ["s5"]


def test_subagent_files_attach_to_the_parent_and_are_not_sessions(
    transcripts: Path, workspace: Path
):
    enc = claude.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s6.jsonl", [_entry(str(workspace), "s6")])
    write_jsonl(transcripts / enc / "s6" / "subagents" / "agent-a1.jsonl",
                [_entry(str(workspace), "s6")])
    sessions, _notes = claude.discover(transcripts, workspace)
    assert len(sessions) == 1
    assert len(sessions[0].subagent_files) == 1


def test_tool_results_directory_is_not_read(transcripts: Path, workspace: Path):
    enc = claude.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s7.jsonl", [_entry(str(workspace), "s7")])
    offload = transcripts / enc / "s7" / "tool-results"
    offload.mkdir(parents=True)
    (offload / "blob.txt").write_text("large output")
    sessions, _notes = claude.discover(transcripts, workspace)
    assert sessions[0].subagent_files == []


def test_unrelated_project_directories_are_never_opened(
    transcripts: Path, workspace: Path, monkeypatch
):
    """The privacy guarantee: personal transcripts are not read at all."""
    enc = claude.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s8.jsonl", [_entry(str(workspace), "s8")])
    personal = transcripts / "C--Users-me-personal-diary"
    write_jsonl(personal / "p1.jsonl", [_entry("C:/Users/me/personal/diary", "p1")])

    opened: list[str] = []
    real_open = claude.open_text

    def _spy(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(claude, "open_text", _spy)
    claude.discover(transcripts, workspace)
    assert not any("personal" in path for path in opened)


def test_file_without_a_cwd_is_reported_not_silently_dropped(
    transcripts: Path, workspace: Path
):
    enc = claude.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s9.jsonl",
                [{"type": "mode", "sessionId": "s9"}])
    sessions, notes = claude.discover(transcripts, workspace)
    assert sessions == []
    assert any("no working directory" in note for note in notes)


def test_set_aside_orphaned_transcripts_are_not_sessions(tmp_path, workspace):
    folder = tmp_path / claude.encode_dir_name(workspace)
    folder.mkdir()
    line = json.dumps({"type": "user", "cwd": str(workspace), "sessionId": "s1",
                       "timestamp": "2026-10-26T09:00:00Z",
                       "message": {"role": "user", "content": "hi"}}) + "\n"
    (folder / "s1.jsonl").write_text(line, encoding="utf-8")
    (folder / "s1.orphaned-1761400000-abcd.jsonl").write_text(line, encoding="utf-8")
    sessions, _notes = claude.discover(tmp_path, workspace)
    assert [s.main_file.name for s in sessions] == ["s1.jsonl"]


# ── conversion ───────────────────────────────────────────────────────────────


def _assistant(blocks, ts="2026-08-11T09:00:00Z", **over):
    entry = {"type": "assistant", "timestamp": ts, "cwd": "C:/Dev/ws",
             "message": {"role": "assistant", "content": blocks}}
    entry.update(over)
    return entry


def _tool_result(tid, content, ts="2026-08-11T09:00:01Z"):
    return {"type": "user", "timestamp": ts, "cwd": "C:/Dev/ws",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content}]}}


WS = Path("C:/Dev/ws")


def test_tool_use_becomes_a_tool_call():
    out = claude.to_messages([_assistant([
        {"type": "tool_use", "id": "t1", "name": "Write",
         "input": {"file_path": "C:/Dev/ws/src/a.py", "content": "x = 1\n"}}])], WS)
    call = out[0]["tool_calls"][0]
    assert call["id"] == "t1"
    assert call["function"]["name"] == "Write"
    assert call["function"]["arguments"]["file_path"] == "src/a.py"


def test_paths_outside_the_workspace_stay_absolute():
    """So the server's _GLOBAL_PATH_RE discards them and they are never attested."""
    out = claude.to_messages([_assistant([
        {"type": "tool_use", "id": "t1", "name": "Write",
         "input": {"file_path": "C:/Other/secret.py", "content": "x = 1\n"}}])], WS)
    assert out[0]["tool_calls"][0]["function"]["arguments"]["file_path"] == \
        "C:/Other/secret.py"


def test_tab_numbered_read_results_are_stripped():
    out = claude.to_messages([_tool_result("t1", "     1\timport os\n     2\tx = 1")], WS)
    assert out[0]["content"] == "import os\nx = 1"


def test_prose_that_merely_starts_with_a_number_is_not_stripped():
    out = claude.to_messages([_tool_result("t1", "1\tfirst\nplain line\nanother line")], WS)
    assert "1\tfirst" in out[0]["content"]


def test_thinking_blocks_are_dropped_including_signature():
    out = claude.to_messages([_assistant([
        {"type": "thinking", "thinking": "", "signature": "A" * 2000},
        {"type": "text", "text": "done"}])], WS)
    assert "A" * 100 not in str(out)
    assert out[0]["content"] == "done"


def test_images_become_placeholders_inside_tool_results():
    out = claude.to_messages([_tool_result("t1", [
        {"type": "image", "source": {"data": "B" * 5000}},
        {"type": "text", "text": "ok"}])], WS)
    assert "B" * 100 not in str(out)
    assert "[image removed before upload]" in out[0]["content"]


def test_every_message_carries_client_ts():
    """Without this the server's velocity clock flags the candidate (§7.3)."""
    out = claude.to_messages([_assistant([{"type": "text", "text": "hi"}],
                                        ts="2026-08-11T10:11:12Z")], WS)
    assert out[0]["client_ts"] == "2026-08-11T10:11:12Z"


def test_entry_without_a_timestamp_omits_client_ts_rather_than_guessing():
    entry = _assistant([{"type": "text", "text": "hi"}])
    del entry["timestamp"]
    out = claude.to_messages([entry], WS)
    assert "client_ts" not in out[0]


def test_non_conversation_entry_types_are_dropped():
    entries = [{"type": t, "cwd": "C:/Dev/ws"} for t in
               ("mode", "permission-mode", "ai-title", "file-history-snapshot",
                "attachment", "queue-operation", "agent-name", "last-prompt")]
    assert claude.to_messages(entries, WS) == []


def test_api_request_blobs_are_not_conversation_even_with_a_message_dict():
    # Claude Code >= 2.1.285 mirrors each API request body into the transcript
    # as `api-request-blob` entries carrying a full `message` and no timestamp.
    blobs = [{"type": "api-request-blob", "cwd": "C:/Dev/ws",
              "message": {"role": role, "content": [{"type": "text", "text": "mirror"}]}}
             for role in ("user", "assistant", "system")]
    blobs.append({"type": "api-request-blob", "cwd": "C:/Dev/ws",
                  "message": {"role": "assistant", "content": [
                      {"type": "tool_use", "id": "t1", "name": "Read",
                       "input": {"file_path": "C:/Dev/ws/a.py"}}]}})
    real = [_assistant([{"type": "text", "text": "real"}])]
    assert claude.to_messages(blobs + real, WS) == claude.to_messages(real, WS)


def _sub_session(transcripts: Path, workspace: Path, meta: dict | None):
    """A one-session tree with a single subagent, optionally with a sidecar."""
    enc = claude.encode_dir_name(workspace)
    base = {"cwd": str(workspace), "sessionId": "s1",
            "timestamp": "2026-08-11T09:00:00Z"}
    write_jsonl(transcripts / enc / "s1.jsonl",
                [{**base, "type": "user",
                  "message": {"role": "user", "content": "main"}}])
    write_jsonl(transcripts / enc / "s1" / "subagents" / "agent-a1.jsonl",
                [{**base, "type": "assistant", "timestamp": "2026-08-11T09:00:01Z",
                  "message": {"role": "assistant",
                              "content": [{"type": "text", "text": "sub"}]}}])
    if meta is not None:
        sidecar = transcripts / enc / "s1" / "subagents" / "agent-a1.meta.json"
        sidecar.write_text(json.dumps(meta), encoding="utf-8")
    sessions, _notes = claude.discover(transcripts, workspace)
    return sessions[0]


def test_subagent_messages_carry_the_real_agent_type_and_parent_call(
    transcripts: Path, workspace: Path
):
    """The sidecar has both fields; emitting a literal 'subagent' would throw
    away which agent actually did the work (§7)."""
    session = _sub_session(transcripts, workspace,
                           {"agentType": "code-reviewer",
                            "toolUseId": "toolu_01Kai", "description": "review"})
    messages, _meta = claude.load_session(session, workspace)
    main = [m for m in messages if "cadra_agent" not in m]
    sub = [m for m in messages if "cadra_agent" in m]
    assert [m["content"] for m in main] == ["main"]
    assert sub[0]["cadra_agent"] == {"parent_tool_use_id": "toolu_01Kai",
                                     "agent_type": "code-reviewer"}


def test_missing_sidecar_still_tags_the_message_without_inventing_a_parent(
    transcripts: Path, workspace: Path
):
    session = _sub_session(transcripts, workspace, None)
    messages, _meta = claude.load_session(session, workspace)
    sub = [m for m in messages if "cadra_agent" in m]
    assert sub[0]["cadra_agent"] == {"agent_type": "subagent"}


def test_main_transcript_messages_are_never_tagged(
    transcripts: Path, workspace: Path
):
    session = _sub_session(transcripts, workspace, {"agentType": "general-purpose"})
    messages, _meta = claude.load_session(session, workspace)
    assert "cadra_agent" not in messages[0]


# ── agent-injected content must not be attributed to the candidate ───────────
# Claude Code injects skill text as a role=user entry carrying isMeta=True and a
# sourceToolUseID. Recorded as role=user it becomes candidate-authored prose to
# the grader: _user_rows_from_trace feeds it to paste_velocity_evidence,
# select_judge_candidates and tier0_hits. A 5k-char "prompt" that appears
# instantly is exactly the pasted-text signature — this already fired in
# production on a Claude Code system-reminder and banded a grade to `caution`.
#
# Measured on a real session (49ecd456): two injected blocks of 3,369 and 4,834
# chars were 66% of ALL user-role text, while the managed path kept its injected
# bulk in a role=system row the grader ignores. Re-roling to "system" matches
# managed: the content is preserved for context and excluded from authorship.

def _user_text(text, ts="2026-08-11T09:00:00Z", **over):
    entry = {"type": "user", "timestamp": ts, "cwd": "C:/Dev/ws",
             "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    entry.update(over)
    return entry


def test_injected_skill_text_is_not_recorded_as_a_candidate_message():
    out = claude.to_messages([_user_text(
        "Base directory for this skill: C:/plugins/x/skills/y",
        isMeta=True, sourceToolUseID="toolu_01",
    )], WS)
    assert [m["role"] for m in out] == ["system"]


def test_a_typed_message_is_still_the_candidate_speaking():
    out = claude.to_messages([_user_text("add a gitignore for token.txt")], WS)
    assert [m["role"] for m in out] == ["user"]
    assert out[0]["content"] == "add a gitignore for token.txt"


def test_injected_content_is_kept_not_dropped():
    """The grader still wants the context; it just must not score it as theirs."""
    out = claude.to_messages([_user_text("injected context", isMeta=True)], WS)
    assert out[0]["content"] == "injected context"
    assert out[0].get("client_ts") == "2026-08-11T09:00:00Z"


def test_a_plain_string_user_entry_is_re_roled_too():
    """Claude Code also emits content as a bare string, handled by a separate
    branch — the marker must be honoured there as well, not just for blocks."""
    out = claude.to_messages([{
        "type": "user", "timestamp": "2026-08-11T09:00:00Z", "cwd": "C:/Dev/ws",
        "isMeta": True, "message": {"role": "user", "content": "injected prose"},
    }], WS)
    assert [m["role"] for m in out] == ["system"]


def test_tool_results_are_unaffected_by_the_marker():
    """tool_result blocks already become role=tool; isMeta must not re-role them
    into system and lose the tool_call_id linkage."""
    entry = _tool_result("t1", "ok")
    entry["isMeta"] = True
    out = claude.to_messages([entry], WS)
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "t1"


