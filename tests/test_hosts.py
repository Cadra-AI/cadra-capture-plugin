"""Host routing in cadra-submit, and the I/O rules every entry point shares.

No real network: `client.post_chunk` / `client.get_traces` are always stubbed.
"""
import ast
import io
import json
from pathlib import Path

import pytest

import cadra_connect
import cadra_submit
import cadra_traces
from cadra import collect, config
from tests.conftest import write_jsonl

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "cadra-trace-tracker" / "scripts"


@pytest.fixture
def connected(workspace: Path, transcripts: Path):
    config.save(workspace, {"token": "tok", "workspace_root": str(workspace),
                            "proxy_base_url": "https://proxy.test",
                            "git_remote": "https://github.com/c/s.git"})
    enc = collect.encode_dir_name(Path(str(workspace)).resolve())
    write_jsonl(transcripts / enc / "sess1.jsonl", [
        {"type": "user", "cwd": str(workspace), "sessionId": "sess1",
         "timestamp": "2026-08-11T09:00:00Z", "version": "2.0.14",
         "message": {"role": "user", "content": "build it"}},
    ])
    return workspace


def _accept(sent: list[dict]):
    def _post(*, base_url, token, body):
        sent.append(body)
        return 202, {"accepted": True, "messages": len(body["messages"]),
                     "received_at": "now"}
    return _post


def _fake_host(name: str, calls: list[str], tmp_path: Path):
    def _discover(root, workspace):
        calls.append(name)
        session = collect.Session(session_id=f"{name}-0001", main_file=tmp_path / "x",
                                  origin_cwd=str(workspace), host=name)
        return [session], []

    def _load(session, workspace):
        meta = {"cwds": [str(workspace)], "started_at": None, "ended_at": None,
                "agent_version": "9.9", "unreadable_lines": 0}
        return [{"role": "user", "content": "hi"}], meta

    return (lambda: tmp_path / name, _discover, _load)


def test_agent_name_is_the_session_host(connected, tmp_path, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(cadra_submit, "HOSTS", {"fake": _fake_host("fake", [], tmp_path)})
    monkeypatch.setattr(cadra_submit.client, "post_chunk", _accept(sent))
    assert cadra_submit.main(["--workspace", str(connected), "--host", "fake"]) == 0
    assert sent[0]["agent"]["name"] == "fake"


def test_host_filter_limits_discovery(connected, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cadra_submit, "HOSTS", {"a": _fake_host("a", calls, tmp_path),
                                                "b": _fake_host("b", calls, tmp_path)})
    cadra_submit.main(["--workspace", str(connected), "--host", "a", "--dry-run"])
    assert calls == ["a"]


def test_default_runs_every_host(connected, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cadra_submit, "HOSTS", {"a": _fake_host("a", calls, tmp_path),
                                                "b": _fake_host("b", calls, tmp_path)})
    cadra_submit.main(["--workspace", str(connected), "--dry-run"])
    assert calls == ["a", "b"]


@pytest.mark.parametrize("flag", ["--claude-root", "--projects-root"])
def test_claude_state_keys_unchanged(connected, transcripts, monkeypatch, flag):
    monkeypatch.setattr(cadra_submit.client, "post_chunk", _accept([]))
    assert cadra_submit.main(["--workspace", str(connected), flag, str(transcripts)]) == 0
    state = json.loads((connected / ".cadra" / "state.json").read_text())
    assert list(state) == ["sess1"]


def test_non_claude_state_key_is_prefixed(connected, tmp_path, monkeypatch):
    monkeypatch.setattr(cadra_submit, "HOSTS", {"fake": _fake_host("fake", [], tmp_path)})
    monkeypatch.setattr(cadra_submit.client, "post_chunk", _accept([]))
    cadra_submit.main(["--workspace", str(connected), "--host", "fake"])
    state = json.loads((connected / ".cadra" / "state.json").read_text())
    assert list(state) == ["fake:fake-0001"]


def test_non_claude_preview_entry_names_its_host(connected, tmp_path, monkeypatch):
    monkeypatch.setattr(cadra_submit, "HOSTS", {"fake": _fake_host("fake", [], tmp_path)})
    cadra_submit.main(["--workspace", str(connected), "--dry-run"])
    preview = json.loads((connected / ".cadra" / "last-preview.json").read_text())
    assert preview["sessions"][0]["host"] == "fake"


def test_claude_preview_entry_gains_no_host_key(connected, transcripts):
    cadra_submit.main(["--workspace", str(connected), "--claude-root", str(transcripts),
                       "--dry-run"])
    preview = json.loads((connected / ".cadra" / "last-preview.json").read_text())
    assert "host" not in preview["sessions"][0]


def test_non_claude_new_cwd_after_preview_is_reported(connected, tmp_path, monkeypatch,
                                                       capsys):
    """The preview -> send round trip must find a non-Claude session by its
    host-prefixed key, or a cwd entered after the preview would go unreported."""
    root_fn, discover, load = _fake_host("fake", [], tmp_path)
    cwds = [str(connected)]

    def _load_with_cwds(session, workspace):
        messages, meta = load(session, workspace)
        return messages, {**meta, "cwds": list(cwds)}

    monkeypatch.setattr(cadra_submit, "HOSTS", {"fake": (root_fn, discover, _load_with_cwds)})
    monkeypatch.setattr(cadra_submit.client, "post_chunk", _accept([]))
    cadra_submit.main(["--workspace", str(connected), "--dry-run"])
    cwds.append(str(connected / "later"))
    capsys.readouterr()
    assert cadra_submit.main(["--workspace", str(connected)]) == 0
    assert f"NEW-CWD fake-000 — {connected / 'later'}" in capsys.readouterr().out


class _RecordingStdout(io.TextIOWrapper):
    def reconfigure(self, **kwargs):
        self.reconfigured = kwargs


@pytest.mark.parametrize("module", [cadra_connect, cadra_submit, cadra_traces])
def test_entry_points_reconfigure_stdout(module, tmp_path, monkeypatch):
    fake = _RecordingStdout(io.BytesIO(), encoding="ascii")
    monkeypatch.setattr("sys.stdout", fake)
    module.main(["--workspace", str(tmp_path / "not-a-workspace")])
    assert fake.reconfigured == {"encoding": "utf-8", "errors": "replace"}


def _script_trees():
    for path in sorted(SCRIPTS.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_scripts_never_prompt_or_use_cwd():
    """The host pipes stdin/stdout; a prompt would hang it, and the host's cwd is
    not the workspace. The workspace only ever comes from --workspace."""
    for path, tree in _script_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "input", path
            if isinstance(node, ast.Attribute) and node.attr == "getcwd":
                assert not (isinstance(node.value, ast.Name) and node.value.id == "os"), path


def test_workspace_comes_from_the_flag(connected, transcripts, tmp_path, monkeypatch,
                                       capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code = cadra_submit.main(["--workspace", str(connected), "--claude-root",
                              str(transcripts), "--dry-run"])
    assert code == 0
    assert "FOUND 1" in capsys.readouterr().out
