"""Extraction parity — spec §11 calls this the load-bearing test.

The plugin's whole reason for existing is that the *server* stays agent-agnostic:
every piece of Claude Code format knowledge lives here, and the server's extractor
is handed a canonical envelope. Nothing in this repo can prove that on its own —
a transformation can be perfectly self-consistent and still produce zero
attestations, which is exactly the absolute-path defect (§7.1) that motivated the
rebuild. So this test runs the real `attestation_items_from_payload` from
`cadra-prototype` over the exact payload `cadra-submit` writes.

It reads that repo and never writes to it. If the sibling checkout is not present
(CI, a fresh clone, a candidate's machine) the test skips loudly rather than
failing — but it must not be allowed to skip silently on the machine where the
proxy contract is actually being developed, so the skip reason names the override.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

import cadra_submit
from cadra import collect, config
from tests.conftest import write_jsonl

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROTOTYPE = REPO.parent / "cadra-prototype"


def _load_line_attest():
    root = Path(os.environ.get("CADRA_PROTOTYPE_ROOT", str(DEFAULT_PROTOTYPE)))
    module_path = root / "proxy" / "line_attest.py"
    if not module_path.is_file():
        pytest.skip(
            f"cadra-prototype not found at {module_path}; set CADRA_PROTOTYPE_ROOT "
            "to run the extraction-parity test"
        )
    spec = importlib.util.spec_from_file_location("cadra_proto_line_attest",
                                                  module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # pure stdlib helpers, no side effects
    return module


def _payload(workspace: Path, transcripts: Path, entries: list[dict]) -> dict:
    """Run the real pipeline and return exactly what would have been sent."""
    config.save(workspace, {"token": "tok", "workspace_root": str(workspace),
                            "proxy_base_url": "https://proxy.test"})
    encoded = collect.encode_dir_name(workspace.resolve())
    write_jsonl(transcripts / encoded / "s1.jsonl", entries)
    assert cadra_submit.main(["--workspace", str(workspace),
                              "--projects-root", str(transcripts),
                              "--dry-run"]) == 0
    preview = json.loads(
        (workspace / ".cadra" / "last-preview.json").read_text(encoding="utf-8"))
    return {"messages": preview["sessions"][0]["messages"]}


def _write_entries(workspace: Path, path: str, body: str) -> list[dict]:
    return [
        {"type": "user", "cwd": str(workspace), "sessionId": "s1",
         "timestamp": "2026-08-11T09:00:00Z",
         "message": {"role": "user", "content": "build it"}},
        {"type": "assistant", "cwd": str(workspace), "sessionId": "s1",
         "timestamp": "2026-08-11T09:00:01Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_1", "name": "Write",
              "input": {"file_path": path, "content": body}}]}},
    ]


BODY = ("def compute_total(rows):\n"
        "    subtotal = sum(row.amount for row in rows)\n"
        "    return subtotal * TAX_RATE\n")


def test_workspace_writes_produce_attestation_items(workspace: Path,
                                                    transcripts: Path):
    """The defect this exists to catch: an absolute path inside the workspace
    reaches the server as a repo-relative one, and therefore scores."""
    line_attest = _load_line_attest()
    target = workspace / "src" / "billing.py"
    payload = _payload(workspace, transcripts,
                       _write_entries(workspace, str(target), BODY))

    items, shell_writes = line_attest.attestation_items_from_payload(payload)

    assert items, "no attestation items — the envelope would score zero"
    assert [item["path"] for item in items] == ["src/billing.py"]
    assert len(items[0]["hashes"]) == 3
    assert items[0]["source"] == "tool"
    assert shell_writes is False


def test_writes_outside_the_workspace_are_excluded(workspace: Path,
                                                   transcripts: Path):
    """The other half of §7.1: paths outside the workspace stay absolute, so the
    server's _GLOBAL_PATH_RE drops them instead of crediting foreign work."""
    line_attest = _load_line_attest()
    payload = _payload(workspace, transcripts,
                       _write_entries(workspace, "C:/Users/someone/notes.py", BODY))

    items, _shell_writes = line_attest.attestation_items_from_payload(payload)

    assert items == []
