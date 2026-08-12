"""End-to-end submission against a stubbed proxy (§5.2, §10).

No test in this file may make a real network call: `client.post_chunk` is always
replaced. The proxy does not exist yet.
"""
import json
from pathlib import Path

import pytest

import cadra_submit
from cadra import config
from tests.conftest import write_jsonl


@pytest.fixture
def connected(workspace: Path, transcripts: Path):
    config.save(workspace, {"token": "tok", "workspace_root": str(workspace),
                            "proxy_base_url": "https://proxy.test",
                            "git_remote": "https://github.com/c/s.git"})
    from cadra import collect
    enc = collect.encode_dir_name(Path(str(workspace)).resolve())
    write_jsonl(transcripts / enc / "sess1.jsonl", [
        {"type": "user", "cwd": str(workspace), "sessionId": "sess1",
         "timestamp": "2026-08-11T09:00:00Z", "version": "2.0.14",
         "message": {"role": "user", "content": "build it"}},
        {"type": "assistant", "cwd": str(workspace), "timestamp": "2026-08-11T09:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Write",
              "input": {"file_path": str(workspace / "a.py"), "content": "x = 1\n"}}]}},
    ])
    return workspace


def test_submission_posts_the_expected_envelope(connected: Path, transcripts: Path,
                                                monkeypatch):
    sent: list[dict] = []

    def _post(*, base_url, token, body):
        sent.append(body)
        return 202, {"accepted": True, "session_id": body["session"]["session_id"],
                     "messages": len(body["messages"]), "received_at": "now"}

    monkeypatch.setattr(cadra_submit.client, "post_chunk", _post)
    monkeypatch.setattr(cadra_submit.repo, "git_remote",
                        lambda ws: "https://github.com/c/s.git")
    code = cadra_submit.main(["--workspace", str(connected),
                              "--projects-root", str(transcripts)])
    assert code == 0
    body = sent[0]
    assert body["session"]["session_id"] == "sess1"
    assert "assessment_id" not in body          # identity comes from the token
    assert body["binding"]["git_remote"] == "https://github.com/c/s.git"
    assert body["agent"]["name"] == "claude-code"
    assert body["agent"]["version"] == "2.0.14"
    assert body["messages"][0]["client_ts"] == "2026-08-11T09:00:00Z"
    args = body["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert args["file_path"] == "a.py"          # rebased


def test_nothing_new_is_reported_explicitly(connected: Path, transcripts: Path,
                                            monkeypatch, capsys):
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (202, {"accepted": True, "session_id": "sess1",
                                            "messages": 2, "received_at": "now"}))
    cadra_submit.main(["--workspace", str(connected), "--projects-root", str(transcripts)])
    capsys.readouterr()
    cadra_submit.main(["--workspace", str(connected), "--projects-root", str(transcripts)])
    assert "already submitted" in capsys.readouterr().out.lower()


def test_server_rejection_is_reported_and_state_not_advanced(
    connected: Path, transcripts: Path, monkeypatch, capsys
):
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (403, {"error": {"code": "rejected_revoked",
                                                      "message": "disconnected"}}))
    code = cadra_submit.main(["--workspace", str(connected),
                              "--projects-root", str(transcripts)])
    assert code == 1
    assert "rejected_revoked" in capsys.readouterr().out
    state = json.loads((connected / ".cadra" / "state.json").read_text()) \
        if (connected / ".cadra" / "state.json").exists() else {}
    assert "sess1" not in state


def test_preview_is_written_and_matches_what_was_sent(connected: Path,
                                                      transcripts: Path, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (sent.append(kw["body"]),
                                      (202, {"accepted": True, "session_id": "sess1",
                                             "messages": 2, "received_at": "n"}))[1])
    cadra_submit.main(["--workspace", str(connected), "--projects-root", str(transcripts)])
    preview = json.loads((connected / ".cadra" / "last-preview.json").read_text())
    assert preview["sessions"][0]["messages"] == sent[0]["messages"]


def test_unconnected_workspace_points_at_connect(workspace: Path, transcripts: Path,
                                                 capsys):
    code = cadra_submit.main(["--workspace", str(workspace),
                              "--projects-root", str(transcripts)])
    assert code == 1
    assert "cadra-connect" in capsys.readouterr().out


def test_envelope_matches_the_canonical_shape(connected: Path, transcripts: Path,
                                              monkeypatch):
    """§7.4 is the contract the unbuilt proxy will be written against, so a
    flat variant here would be a silent break nobody notices until integration."""
    sent: list[dict] = []
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda *, base_url, token, body: (sent.append(body),
                                                          (202, {"messages": 0}))[1])
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    body = sent[0]
    assert set(body) == {"capture_version", "agent", "session", "binding",
                         "chunk", "redaction", "truncated_paths", "messages"}
    assert set(body["session"]) == {"session_id", "started_at", "ended_at", "cwds"}
    assert set(body["binding"]) == {"workspace_root", "git_remote", "git_branch"}
    assert set(body["chunk"]) == {"index", "total", "prefix_hash", "chunk_hash"}
    assert set(body["redaction"]) == {"rules_version", "redacted_count"}


def test_dry_run_sends_nothing_and_advances_no_state(connected: Path,
                                                     transcripts: Path,
                                                     monkeypatch, capsys):
    """The preview is only a consent control if it happens BEFORE the send (§6.1)."""
    def _boom(**_kw):
        raise AssertionError("dry run must not reach the network")

    monkeypatch.setattr(cadra_submit.client, "post_chunk", _boom)
    code = cadra_submit.main(["--workspace", str(connected),
                              "--projects-root", str(transcripts), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY-RUN" in out
    assert "bytes" in out
    assert not (config.config_dir(connected) / "state.json").exists()
    preview = json.loads(
        (config.config_dir(connected) / "last-preview.json").read_text(encoding="utf-8"))
    assert preview["sessions"][0]["message_count"] == 2


def test_submit_does_not_fall_back_to_config_git_remote(
    connected: Path, transcripts: Path, monkeypatch
):
    """§5.1: when git cannot answer, send null — never the connect-time remote."""
    monkeypatch.setattr(cadra_submit.repo, "git_remote", lambda ws: None)
    sent: list[dict] = []
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda *, base_url, token, body: (sent.append(body),
                                                          (202, {"messages": 0}))[1])
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    assert sent[0]["binding"]["git_remote"] is None
    # Fixture config still holds the connect-time value — must not leak into envelope.
    assert config.load(connected)["git_remote"] == "https://github.com/c/s.git"


def test_dry_run_preview_is_what_a_real_submit_sends(connected: Path,
                                                     transcripts: Path, monkeypatch):
    """§11: the preview must be byte-identical to the payload, or it is theatre."""
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts), "--dry-run"])
    preview = json.loads(
        (config.config_dir(connected) / "last-preview.json").read_text(encoding="utf-8"))

    sent: list[dict] = []
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda *, base_url, token, body: (sent.append(body),
                                                          (202, {"messages": 0}))[1])
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    assert preview["sessions"][0]["messages"] == sent[0]["messages"]


def test_malformed_lines_are_counted_and_reported_not_fatal(
    connected: Path, transcripts: Path, monkeypatch, capsys
):
    """§10: skip the line, count it, report the count — never abort."""
    from cadra import collect
    enc = collect.encode_dir_name(Path(str(connected)).resolve())
    path = transcripts / enc / "sess1.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
        handle.write('"a bare string, not an entry"\n')

    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda *, base_url, token, body: (202, {"messages": 0}))
    code = cadra_submit.main(["--workspace", str(connected),
                              "--projects-root", str(transcripts)])
    out = capsys.readouterr().out
    assert code == 0
    assert "2 unreadable line(s) skipped" in out
    assert "SUBMITTED" in out


def test_a_2xx_that_refuses_the_chunk_is_not_treated_as_success(
    connected: Path, transcripts: Path, monkeypatch, capsys
):
    """§10: never report success the server did not state. `accepted: false`
    behind a 200 is a refusal, and the cursor must not move."""
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (200, {"accepted": False}))
    code = cadra_submit.main(["--workspace", str(connected),
                              "--projects-root", str(transcripts)])
    out = capsys.readouterr().out
    assert code == 1
    assert "SUBMITTED" not in out
    assert "not_accepted" in out
    state = json.loads(
        (config.config_dir(connected) / "state.json").read_text(encoding="utf-8"))
    assert state == {}


def test_the_confirmed_count_sums_every_chunk(connected: Path, transcripts: Path,
                                              monkeypatch, capsys):
    """`receipt` is overwritten per chunk, so quoting it reports only the last."""
    monkeypatch.setattr(cadra_submit.envelope, "CHUNK_BYTES", 1)  # one msg per chunk
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda *, base_url, token, body: (
                            202, {"messages": len(body["messages"]),
                                  "received_at": "now"}))
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    out = capsys.readouterr().out
    assert "server confirmed 2 of 2 messages" in out


def test_a_run_where_everything_skips_keeps_the_preview(
    connected: Path, transcripts: Path, monkeypatch
):
    """§4 calls this file what was last sent; blanking it on a no-op run makes
    the skill point the candidate at an empty file."""
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (202, {"messages": 2, "received_at": "now"}))
    args = ["--workspace", str(connected), "--projects-root", str(transcripts)]
    cadra_submit.main(args)
    cadra_submit.main(args)
    preview = json.loads(
        (config.config_dir(connected) / "last-preview.json").read_text(encoding="utf-8"))
    assert len(preview["sessions"]) == 1
    assert preview["sessions"][0]["messages"]


def test_a_directory_entered_after_the_preview_is_called_out(
    connected: Path, transcripts: Path, monkeypatch, capsys
):
    """The dry run and the send are separate runs and the transcript grows
    between them, so the preview cannot be the last word on where a session went."""
    from cadra import collect
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts), "--dry-run"])
    capsys.readouterr()

    enc = collect.encode_dir_name(Path(str(connected)).resolve())
    with open(transcripts / enc / "sess1.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"type": "user", "cwd": str(connected / "private-notes"),
             "sessionId": "sess1", "timestamp": "2026-08-11T09:01:00Z",
             "message": {"role": "user", "content": "later"}}) + "\n")

    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (202, {"messages": 3, "received_at": "now"}))
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    out = capsys.readouterr().out
    assert "NEW-CWD" in out
    assert "private-notes" in out
    assert "entered after the preview you approved" in out


def test_the_remote_is_read_at_submit_time_not_frozen_at_connect(
    connected: Path, transcripts: Path, monkeypatch
):
    """The candidate clones the template, works, then creates their own repo and
    re-points origin. Sending the connect-time value would name a repo they left."""
    monkeypatch.setattr(cadra_submit.repo, "git_remote",
                        lambda ws: "https://github.com/candidate/solution.git")
    sent: list[dict] = []
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda *, base_url, token, body: (sent.append(body),
                                                          (202, {"messages": 0}))[1])
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    # The fixture's config.json records github.com/c/s.git from connect time.
    assert sent[0]["binding"]["git_remote"] == \
        "https://github.com/candidate/solution.git"
