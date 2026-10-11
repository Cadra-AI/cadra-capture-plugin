"""The synthetic Codex, Kiro and Copilot CLI transcripts parse and carry every
record kind the host modules map (user, assistant, tool call, tool result)."""
import json
from pathlib import Path

import pytest


def _records(path: Path) -> tuple[list[dict], int]:
    records, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return records, bad


def _codex_kinds(rec: dict) -> str | None:
    payload = rec.get("payload", {})
    if rec.get("type") != "response_item":
        return None
    if payload.get("type") == "message":
        return {"user": "user", "assistant": "assistant"}.get(payload.get("role"))
    return {"function_call": "call", "function_call_output": "result"}.get(payload.get("type"))


def test_codex_fixture(codex_root: Path, workspace: Path):
    files = list(codex_root.glob("*/*/*/rollout-*.jsonl"))
    assert len(files) == 1
    records, bad = _records(files[0])
    assert bad == 1  # the one deliberate malformed line
    assert records[0]["type"] == "session_meta"
    assert records[0]["payload"]["cwd"] == str(workspace)
    assert {"user", "assistant", "call", "result"} <= {_codex_kinds(r) for r in records}


def test_kiro_fixture(kiro_root: Path, workspace: Path):
    sessions = list(kiro_root.glob("*/sess_*/session.json"))
    assert len(sessions) == 1
    meta = json.loads(sessions[0].read_text(encoding="utf-8"))
    assert meta["workspacePaths"] == [str(workspace)]
    records, bad = _records(sessions[0].parent / "messages.jsonl")
    assert bad == 0
    kinds = {r["payload"]["type"] for r in records}
    assert {"user", "assistant", "tool_call", "tool_result", "sub_agent_start"} <= kinds
    sub, bad = _records(sessions[0].parent / "sub-executions" / "sub-0001.jsonl")
    assert bad == 0
    assert {"assistant", "tool_call", "tool_result"} <= {r["payload"]["type"] for r in sub}
    assert not (sessions[0].parent / "tool-outputs").exists()


def test_copilot_fixture(copilot_root: Path, workspace: Path):
    files = list(copilot_root.glob("*/events.jsonl"))
    assert len(files) == 1
    assert (copilot_root / ".session-operation-locks").is_dir()
    records, bad = _records(files[0])
    assert bad == 0
    assert records[0]["type"] == "session.start"
    assert records[0]["data"]["context"]["cwd"] == str(workspace)
    kinds = {r["type"] for r in records}
    assert {"user.message", "assistant.message", "tool.execution_complete"} <= kinds
    assistant = next(r for r in records if r["type"] == "assistant.message")
    assert assistant["data"]["toolRequests"]


@pytest.mark.parametrize("name", ["codex_root", "kiro_root", "copilot_root", "cursor_root"])
def test_fixture_tree_has_no_placeholder_left(name: str, request):
    root: Path = request.getfixturevalue(name)
    for path in root.rglob("*"):
        if path.is_file():
            assert "{WS}" not in path.read_text(encoding="utf-8"), path
