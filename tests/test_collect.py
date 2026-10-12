"""Discovery and scoping (§6.1, §6.1.1). Each case here was a real bug."""
from pathlib import Path

from cadra import collect
from tests.conftest import write_jsonl


def _entry(cwd: str, sid: str = "s1", **over):
    entry = {"type": "user", "cwd": cwd, "sessionId": sid,
             "timestamp": "2026-08-11T09:00:00Z",
             "message": {"role": "user", "content": "hi"}}
    entry.update(over)
    return entry


def test_encoded_name_matches_claude_code(tmp_path: Path):
    assert collect.encode_dir_name(Path("C:/Dev/ws")) == "C--Dev-ws"


def test_session_in_the_workspace_is_found(transcripts: Path, workspace: Path):
    enc = collect.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s1.jsonl", [_entry(str(workspace))])
    sessions, _notes = collect.discover(transcripts, workspace)
    assert [s.session_id for s in sessions] == ["s1"]


def test_session_started_in_a_subdirectory_is_found(transcripts: Path, workspace: Path):
    """Claude Code puts a subdirectory cwd in a SEPARATE project folder."""
    sub = workspace / "backend"
    enc = collect.encode_dir_name(sub)
    write_jsonl(transcripts / enc / "s2.jsonl", [_entry(str(sub), "s2")])
    sessions, _notes = collect.discover(transcripts, workspace)
    assert [s.session_id for s in sessions] == ["s2"]


def test_sibling_directory_sharing_the_encoded_prefix_is_excluded(
    transcripts: Path, workspace: Path
):
    """C:/Dev/solution-other encodes to a prefix-matching name but is NOT in scope."""
    sibling = workspace.parent / (workspace.name + "-other")
    enc = collect.encode_dir_name(sibling)
    write_jsonl(transcripts / enc / "s3.jsonl", [_entry(str(sibling), "s3")])
    sessions, _notes = collect.discover(transcripts, workspace)
    assert sessions == []


def test_session_started_above_the_workspace_is_excluded_without_opening_its_folder(
    transcripts: Path, workspace: Path, monkeypatch
):
    """A session started one level up is out of scope (§6.1) and we do NOT reach
    up to find it: ancestor folders climb to the candidate's home directory."""
    parent = workspace.parent
    enc = collect.encode_dir_name(parent)
    write_jsonl(transcripts / enc / "s4.jsonl",
                [_entry(str(parent), "s4"), _entry(str(workspace), "s4")])

    opened: list[str] = []
    real_open = collect.open_text
    monkeypatch.setattr(
        collect, "open_text",
        lambda path, *a, **kw: (opened.append(str(path)), real_open(path, *a, **kw))[1],
    )
    sessions, _notes = collect.discover(transcripts, workspace)
    assert sessions == []
    assert not any("s4.jsonl" in path for path in opened)


def test_sibling_in_a_candidate_folder_is_excluded_and_reported(
    transcripts: Path, workspace: Path
):
    """Folders we do open but reject are reported, never silently dropped."""
    sibling = workspace.parent / (workspace.name + "-other")
    enc = collect.encode_dir_name(sibling)
    write_jsonl(transcripts / enc / "s4b.jsonl", [_entry(str(sibling), "s4b")])
    sessions, notes = collect.discover(transcripts, workspace)
    assert sessions == []
    assert any("started elsewhere" in note for note in notes)


def test_session_leaving_the_workspace_is_still_captured_whole(
    transcripts: Path, workspace: Path
):
    enc = collect.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s5.jsonl",
                [_entry(str(workspace), "s5"), _entry("C:/Elsewhere", "s5")])
    sessions, _notes = collect.discover(transcripts, workspace)
    assert [s.session_id for s in sessions] == ["s5"]


def test_subagent_files_attach_to_the_parent_and_are_not_sessions(
    transcripts: Path, workspace: Path
):
    enc = collect.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s6.jsonl", [_entry(str(workspace), "s6")])
    write_jsonl(transcripts / enc / "s6" / "subagents" / "agent-a1.jsonl",
                [_entry(str(workspace), "s6")])
    sessions, _notes = collect.discover(transcripts, workspace)
    assert len(sessions) == 1
    assert len(sessions[0].subagent_files) == 1


def test_tool_results_directory_is_not_read(transcripts: Path, workspace: Path):
    enc = collect.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s7.jsonl", [_entry(str(workspace), "s7")])
    offload = transcripts / enc / "s7" / "tool-results"
    offload.mkdir(parents=True)
    (offload / "blob.txt").write_text("large output")
    sessions, _notes = collect.discover(transcripts, workspace)
    assert sessions[0].subagent_files == []


def test_unrelated_project_directories_are_never_opened(
    transcripts: Path, workspace: Path, monkeypatch
):
    """The privacy guarantee: personal transcripts are not read at all."""
    enc = collect.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s8.jsonl", [_entry(str(workspace), "s8")])
    personal = transcripts / "C--Users-me-personal-diary"
    write_jsonl(personal / "p1.jsonl", [_entry("C:/Users/me/personal/diary", "p1")])

    opened: list[str] = []
    real_open = collect.open_text

    def _spy(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(collect, "open_text", _spy)
    collect.discover(transcripts, workspace)
    assert not any("personal" in path for path in opened)


def test_file_without_a_cwd_is_reported_not_silently_dropped(
    transcripts: Path, workspace: Path
):
    enc = collect.encode_dir_name(workspace)
    write_jsonl(transcripts / enc / "s9.jsonl",
                [{"type": "mode", "sessionId": "s9"}])
    sessions, notes = collect.discover(transcripts, workspace)
    assert sessions == []
    assert any("no working directory" in note for note in notes)
