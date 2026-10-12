# BYO Trace Capture Implementation Plan (plugin)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild this plugin as a skills-only Claude Code plugin that collects a candidate's assessment sessions, converts them to Cadra's canonical envelope, redacts secrets, and submits them to the Cadra proxy under the assessment token.

**Architecture:** Three skills (`cadra-connect`, `cadra-submit`, `cadra-traces`), each a thin conversational wrapper over a Python entry point. All state lives in `<workspace>/.cadra/`. No hooks, no PowerShell, no database credentials. The plugin owns every piece of Claude Code format knowledge — absolute-path rebasing, subagent merging, tab-numbered read results — so the server stays agent-agnostic.

**Tech Stack:** Python 3.11+, **standard library only** (`json`, `pathlib`, `urllib.request`, `hashlib`, `base64`, `re`). pytest for tests.

**Spec:** `docs/specs/2026-08-11-byo-trace-capture-design.md`. Section references below (§N) point there.

## Global Constraints

- **Standard library only in shipped code.** Candidates run this on their own machines; a `pip install` step is a support burden and a failure mode. pytest is a dev dependency, never imported by shipped code.
- **Python 3.11+.** Use `str | None` unions and `tomllib`-era stdlib freely.
- **Write boundary — the load-bearing invariant.** The plugin writes **only** inside `<workspace>/.cadra/`, plus one append to `<workspace>/.gitignore` at connect. Every other filesystem access is read-only. Task 7 tests this directly.
- **No secrets in logs or error messages.** The token is never printed, echoed, or written outside `.cadra/config.json`.
- **Never `rglob`, never content substring matching, never ancestor walking, never the desktop session roots.** Each was a real defect (§6.1.1).
- **The server is the authority on success.** Never report a submission as saved without a server receipt.
- **The proxy is NOT being modified in parallel.** `POST /v1/traces` does not exist yet. All network code is developed against stubs; Task 6 records exactly what must be verified once the proxy ships.
- **Branch:** create `byo-capture-rebuild` off `main` before Task 1.

---

### Task 1: Test harness, config module, and `cadra-connect`

**Files:**
- Create: `plugins/cadra-trace-tracker/scripts/cadra/__init__.py`
- Create: `plugins/cadra-trace-tracker/scripts/cadra/config.py`
- Create: `plugins/cadra-trace-tracker/scripts/cadra_connect.py`
- Create: `plugins/cadra-trace-tracker/skills/cadra-connect/SKILL.md`
- Create: `tests/__init__.py` (empty — makes `tests` importable)
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`
- Create: `pytest.ini`

**Interfaces:**
- Consumes: nothing.
- Produces, in `cadra.config`:
  - `CONFIG_DIRNAME = ".cadra"`, `CONFIG_FILENAME = "config.json"`
  - `find_workspace(start: Path) -> Path | None` — walks up from `start` to the first directory containing `.cadra/config.json`
  - `load(workspace: Path) -> dict` — raises `FileNotFoundError` if absent
  - `save(workspace: Path, cfg: dict) -> None` — creates `.cadra/`, writes `config.json`, chmod 0600 on POSIX
  - `ensure_gitignored(workspace: Path) -> bool` — appends `.cadra/` to `.gitignore` if absent; returns True if it wrote
  - `decode_claims(token: str) -> dict` — base64-decodes the JWT payload **without verifying**; raises `ValueError` on malformed input

- [ ] **Step 1: Create the test harness**

```ini
# pytest.ini
[pytest]
testpaths = tests
pythonpath = . plugins/cadra-trace-tracker/scripts
```

The leading `.` is required: later tasks do `from tests.conftest import write_jsonl`,
which needs the repo root importable. Also create an empty `tests/__init__.py` so
`tests` is a package.

```python
# tests/conftest.py
"""Shared fixtures. Every test uses a temp workspace — never the real home dir."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "solution"
    ws.mkdir()
    return ws


@pytest.fixture
def transcripts(tmp_path: Path) -> Path:
    """Stands in for ~/.claude/projects."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_config.py
"""Config lives in <workspace>/.cadra/ and the token never reaches git (§4)."""
import base64
import json
from pathlib import Path

import pytest

from cadra import config


def _token(claims: dict) -> str:
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{seg({'alg': 'HS256'})}.{seg(claims)}.signature"


def test_save_then_load_roundtrip(workspace: Path):
    config.save(workspace, {"token": "t", "workspace_root": str(workspace)})
    assert config.load(workspace)["token"] == "t"


def test_find_workspace_from_a_subdirectory(workspace: Path):
    config.save(workspace, {"token": "t"})
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    assert config.find_workspace(nested) == workspace


def test_find_workspace_returns_none_when_unconfigured(tmp_path: Path):
    assert config.find_workspace(tmp_path) is None


def test_nested_workspace_resolves_to_the_innermost(workspace: Path):
    inner = workspace / "inner"
    inner.mkdir()
    config.save(workspace, {"token": "outer"})
    config.save(inner, {"token": "inner"})
    assert config.find_workspace(inner) == inner


def test_gitignore_is_created_with_the_entry(workspace: Path):
    assert config.ensure_gitignored(workspace) is True
    assert ".cadra/" in (workspace / ".gitignore").read_text()


def test_gitignore_append_is_idempotent(workspace: Path):
    (workspace / ".gitignore").write_text("node_modules/\n")
    config.ensure_gitignored(workspace)
    config.ensure_gitignored(workspace)
    text = (workspace / ".gitignore").read_text()
    assert text.count(".cadra/") == 1
    assert "node_modules/" in text  # existing entries preserved


def test_decode_claims_reads_the_payload_without_verifying():
    claims = config.decode_claims(_token({"coding_assessment_id": "a-1", "exp": 99}))
    assert claims["coding_assessment_id"] == "a-1"


def test_decode_claims_rejects_malformed_tokens():
    for bad in ("", "notatoken", "only.two"):
        with pytest.raises(ValueError):
            config.decode_claims(bad)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cadra'`

- [ ] **Step 4: Implement the config module**

```python
# plugins/cadra-trace-tracker/scripts/cadra/__init__.py
"""Cadra BYO trace capture. Standard library only."""
```

```python
# plugins/cadra-trace-tracker/scripts/cadra/config.py
"""Workspace-local configuration (spec §4).

Everything the plugin writes lives under <workspace>/.cadra/, so the blast radius
is the project directory. The token is never written anywhere else, and .gitignore
is updated BEFORE config.json is created so the token file is born ignored and can
never enter git history.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

CONFIG_DIRNAME = ".cadra"
CONFIG_FILENAME = "config.json"
GITIGNORE_ENTRY = ".cadra/"
_MAX_WALK_UP = 12


def config_dir(workspace: Path) -> Path:
    return Path(workspace) / CONFIG_DIRNAME


def config_path(workspace: Path) -> Path:
    return config_dir(workspace) / CONFIG_FILENAME


def find_workspace(start: Path) -> Path | None:
    """First ancestor of `start` holding .cadra/config.json — innermost wins."""
    probe = Path(start).resolve()
    for _ in range(_MAX_WALK_UP):
        if config_path(probe).is_file():
            return probe
        if probe.parent == probe:
            break
        probe = probe.parent
    return None


def load(workspace: Path) -> dict:
    with open(config_path(workspace), encoding="utf-8") as handle:
        return json.load(handle)


def save(workspace: Path, cfg: dict) -> None:
    ensure_gitignored(workspace)  # ordering matters: ignore first, then write
    directory = config_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    target = config_path(workspace)
    tmp = target.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2)
    tmp.replace(target)
    if os.name != "nt":
        os.chmod(target, 0o600)


def ensure_gitignored(workspace: Path) -> bool:
    """Append the ignore entry if missing. Additive only; never reorders."""
    gitignore = Path(workspace) / ".gitignore"
    if gitignore.is_file():
        existing = gitignore.read_text(encoding="utf-8", errors="ignore")
        if any(line.strip() in (GITIGNORE_ENTRY, ".cadra")
               for line in existing.splitlines()):
            return False
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        with open(gitignore, "a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{GITIGNORE_ENTRY}\n")
        return True
    gitignore.write_text(f"{GITIGNORE_ENTRY}\n", encoding="utf-8")
    return True


def decode_claims(token: str) -> dict:
    """Read JWT claims WITHOUT verifying. The server verifies; this is only so the
    client can fail fast on an obviously wrong paste."""
    parts = (token or "").split(".")
    if len(parts) != 3 or not parts[1]:
        raise ValueError("Token is not a well-formed JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise ValueError("Token payload is not readable") from exc
    if not isinstance(claims, dict):
        raise ValueError("Token payload is not an object")
    return claims
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS (8 passed)

- [ ] **Step 6: Write the failing test for the connect entry point**

```python
# tests/test_connect.py
"""cadra-connect writes config and proves the token works (§5.1)."""
import json
from pathlib import Path

import pytest

import cadra_connect
from cadra import config


def _token(exp: int = 99999999999) -> str:
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return (f"{seg({'alg': 'HS256'})}."
            f"{seg({'coding_assessment_id': 'a-1', 'exp': exp})}.sig")


def test_connect_writes_config_and_gitignore(workspace: Path, monkeypatch):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    code = cadra_connect.main(["--token", _token(), "--workspace", str(workspace),
                               "--proxy", "https://proxy.test"])
    assert code == 0
    cfg = config.load(workspace)
    assert cfg["token"] == _token()
    assert cfg["proxy_base_url"] == "https://proxy.test"
    assert ".cadra/" in (workspace / ".gitignore").read_text()


def test_connect_records_the_git_remote_when_present(workspace: Path, monkeypatch):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    monkeypatch.setattr(cadra_connect, "git_remote",
                        lambda ws: "https://github.com/c/s.git")
    cadra_connect.main(["--token", _token(), "--workspace", str(workspace),
                        "--proxy", "https://proxy.test"])
    assert config.load(workspace)["git_remote"] == "https://github.com/c/s.git"


def test_expired_token_is_refused_before_anything_is_written(workspace: Path):
    code = cadra_connect.main(["--token", _token(exp=1), "--workspace", str(workspace),
                               "--proxy", "https://proxy.test"])
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_malformed_token_is_refused(workspace: Path):
    code = cadra_connect.main(["--token", "nonsense", "--workspace", str(workspace),
                               "--proxy", "https://proxy.test"])
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_server_rejection_does_not_leave_a_config(workspace: Path, monkeypatch):
    """Fail fast on day zero rather than at the deadline."""
    monkeypatch.setattr(cadra_connect, "verify_token",
                        lambda **kw: (False, "token not accepted (401)"))
    code = cadra_connect.main(["--token", _token(), "--workspace", str(workspace),
                               "--proxy", "https://proxy.test"])
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_token_is_never_printed(workspace: Path, monkeypatch, capsys):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    cadra_connect.main(["--token", _token(), "--workspace", str(workspace),
                        "--proxy", "https://proxy.test"])
    assert _token() not in capsys.readouterr().out
```

- [ ] **Step 7: Run test to verify it fails**

Run: `python -m pytest tests/test_connect.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cadra_connect'`

- [ ] **Step 8: Implement the connect entry point**

```python
# plugins/cadra-trace-tracker/scripts/cadra_connect.py
"""cadra-connect — register this workspace for trace submission (spec §5.1).

Exercises the token on day zero. A managed OpenCode candidate discovers a bad
token on their first prompt; a BYO candidate would otherwise discover it at
submission, after the work is done.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from cadra import config

TIMEOUT_S = 20


def git_remote(workspace: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(workspace), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    remote = out.stdout.strip()
    return remote or None


def verify_token(*, proxy_base_url: str, token: str) -> tuple[bool, str]:
    """Call GET /v1/traces. The signal is 200 vs 401; contents are irrelevant."""
    request = urllib.request.Request(
        f"{proxy_base_url.rstrip('/')}/v1/traces",
        headers={"Authorization": f"Bearer {token}"}, method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
            return (200 <= resp.status < 300), ""
    except urllib.error.HTTPError as err:
        if err.code == 401:
            return False, "the server did not accept this token (401)"
        return False, f"the server returned HTTP {err.code}"
    except Exception as exc:
        return False, f"could not reach Cadra ({type(exc).__name__})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"FAILED: {workspace} is not a directory")
        return 1

    try:
        claims = config.decode_claims(args.token)
    except ValueError as exc:
        print(f"FAILED: {exc}. Re-copy the token from your Setup page.")
        return 1
    if not claims.get("coding_assessment_id"):
        print("FAILED: this token carries no assessment. Re-copy it from Setup.")
        return 1
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        print("FAILED: this token has expired. Request a fresh one.")
        return 1

    ok, detail = verify_token(proxy_base_url=args.proxy, token=args.token)
    if not ok:
        print(f"FAILED: {detail}. Nothing was saved.")
        return 1

    config.save(workspace, {
        "token": args.token,
        "assessment_label": args.label,
        "workspace_root": str(workspace).replace("\\", "/"),
        "git_remote": git_remote(workspace),
        "proxy_base_url": args.proxy.rstrip("/"),
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    print(f"CONNECTED workspace={workspace}")
    print("Trace submission is ready. Say 'submit my trace' when you want to send "
          "your work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `python -m pytest tests/ -q`
Expected: PASS (14 passed)

- [ ] **Step 10: Write the skill**

```markdown
<!-- plugins/cadra-trace-tracker/skills/cadra-connect/SKILL.md -->
---
description: Connect this workspace to a Cadra assessment so work sessions can be submitted. Use when the user says "connect to Cadra", "set up my assessment", "register my workspace", "my Cadra token is wrong", or when cadra-submit reports the workspace is not connected.
---

# Skill: cadra-connect
**Plugin:** cadra-trace-tracker

## Purpose

Register this workspace against a Cadra coding assessment and prove the token
works immediately — so a bad token surfaces on day one rather than at the
deadline.

## Steps

1. Ask the user for the **assessment token** from their Cadra Setup page, and for
   the **proxy URL** shown alongside it (default `https://proxy.cadra.info`).
2. Confirm the workspace: the current working directory should be the root of the
   solution repository. If the user is in a subdirectory, ask before proceeding.
3. Run the connect script from the workspace root:

   ```
   python "$CLAUDE_PLUGIN_ROOT/scripts/cadra_connect.py" --token "<TOKEN>" --workspace "<WORKSPACE>" --proxy "<PROXY_URL>"
   ```

4. Report the result in plain language. On `CONNECTED`, tell the user trace
   submission is ready and they can say "submit my trace" whenever they want to
   send their work. On `FAILED`, relay the reason and what to do about it.

## Rules

- **Never print, echo, or repeat the token** in your replies.
- Never write `.cadra/config.json` yourself — always run the script, which also
  updates `.gitignore` in the correct order so the token cannot be committed.
- If the script reports failure, nothing was saved; do not claim the workspace is
  connected.
```

- [ ] **Step 11: Commit**

```bash
git add pytest.ini tests plugins/cadra-trace-tracker/scripts plugins/cadra-trace-tracker/skills/cadra-connect
git commit -m "feat: workspace config module and cadra-connect skill"
```

---

### Task 2: Session discovery and scoping

**Files:**
- Create: `plugins/cadra-trace-tracker/scripts/cadra/collect.py`
- Test: `tests/test_collect.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces, in `cadra.collect`:
  - `encode_dir_name(path: Path) -> str`
  - `candidate_dirs(projects_root: Path, workspace: Path) -> list[Path]`
  - `origin_cwd(jsonl: Path) -> str | None` — reads at most 50 lines
  - `in_scope(cwd: str, workspace: Path) -> bool`
  - `@dataclass Session` with `session_id: str`, `main_file: Path`, `subagent_files: list[Path]`, `origin_cwd: str`
  - `discover(projects_root: Path, workspace: Path) -> tuple[list[Session], list[str]]` — returns in-scope sessions and human-readable notes about excluded ones

- [ ] **Step 1: Write the failing test**

```python
# tests/test_collect.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_collect.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cadra.collect'`

- [ ] **Step 3: Implement discovery**

```python
# plugins/cadra-trace-tracker/scripts/cadra/collect.py
"""Find the transcripts belonging to this assessment (spec §6.1, §6.1.1).

Every restriction here exists because its absence was a real defect:
  - no content substring matching (uploaded unrelated personal sessions)
  - no rglob (swept subagent files up as anonymous sessions)
  - no ancestor walking (the old gate walked six levels up and one down)
  - no desktop storage roots (that is the Claude desktop app, not Claude Code)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_SCOPE_PROBE_LINES = 50
MAX_SCOPE_PROBE_BYTES = 256 * 1024

open_text = open  # indirection so tests can observe which files are opened


def encode_dir_name(path: Path) -> str:
    """Claude Code encodes a session's cwd into its storage folder name."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _norm(path: Path) -> Path:
    return Path(str(path)).resolve()


def in_scope(cwd: str, workspace: Path) -> bool:
    """True iff cwd is the workspace or a descendant. Resolved-path comparison,
    never string prefix — otherwise C:/Dev/ws matches C:/Dev/ws-other."""
    try:
        candidate = _norm(Path(cwd))
        root = _norm(workspace)
    except (OSError, ValueError):
        return False
    return candidate == root or root in candidate.parents


def candidate_dirs(projects_root: Path, workspace: Path) -> list[Path]:
    """Sound superset: an in-scope origin cwd always encodes to this prefix.

    Forward match only — ancestor directories are deliberately NOT matched;
    reaching up would open the candidate's home directory (design §6.1)."""
    prefix = encode_dir_name(_norm(workspace))
    lowered = prefix.lower()
    out: list[Path] = []
    if not projects_root.is_dir():
        return out
    for entry in sorted(projects_root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name.lower()
        if name == lowered or name.startswith(lowered + "-"):
            out.append(entry)
    return out


def origin_cwd(jsonl: Path) -> str | None:
    """The cwd of the first entry carrying one. Bounded read (§6.1.1 step 3)."""
    try:
        with open_text(jsonl, encoding="utf-8", errors="ignore") as handle:
            consumed = 0
            for index, line in enumerate(handle):
                consumed += len(line)
                if index >= MAX_SCOPE_PROBE_LINES or consumed > MAX_SCOPE_PROBE_BYTES:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd") if isinstance(entry, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


@dataclass
class Session:
    session_id: str
    main_file: Path
    origin_cwd: str
    subagent_files: list[Path] = field(default_factory=list)


def discover(projects_root: Path, workspace: Path) -> tuple[list[Session], list[str]]:
    """-> (in-scope sessions, notes about what was excluded and why)."""
    sessions: list[Session] = []
    notes: list[str] = []
    for directory in candidate_dirs(projects_root, workspace):
        for main_file in sorted(directory.glob("*.jsonl")):
            cwd = origin_cwd(main_file)
            if cwd is None:
                notes.append(f"{main_file.name}: no working directory recorded — skipped")
                continue
            if not in_scope(cwd, workspace):
                notes.append(
                    f"{main_file.stem[:8]}: started elsewhere ({cwd}) — not included"
                )
                continue
            session_id = main_file.stem
            subagents = sorted(
                (directory / session_id / "subagents").glob("agent-*.jsonl")
            )
            sessions.append(Session(session_id=session_id, main_file=main_file,
                                    origin_cwd=cwd, subagent_files=list(subagents)))
    return sessions, notes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_collect.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add plugins/cadra-trace-tracker/scripts/cadra/collect.py tests/test_collect.py
git commit -m "feat: discover in-scope sessions by origin cwd without rglob"
```

---

### Task 3: Transformation to the canonical envelope

**Files:**
- Create: `plugins/cadra-trace-tracker/scripts/cadra/adapt.py`
- Test: `tests/test_adapt.py`

**Interfaces:**
- Consumes: `Session` from `cadra.collect`.
- Produces, in `cadra.adapt`:
  - `rebase_path(value: str, workspace: Path) -> str`
  - `strip_line_numbers(text: str) -> str`
  - `to_messages(entries: list[dict], workspace: Path) -> list[dict]`
  - `load_session(session: Session, workspace: Path) -> tuple[list[dict], dict]` — returns `(messages, meta)` where `meta` has `cwds: list[str]`, `started_at`, `ended_at`, `agent_version`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapt.py
"""Claude Code -> canonical OpenAI-style envelope (§7)."""
from pathlib import Path

from cadra import adapt


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
    out = adapt.to_messages([_assistant([
        {"type": "tool_use", "id": "t1", "name": "Write",
         "input": {"file_path": "C:/Dev/ws/src/a.py", "content": "x = 1\n"}}])], WS)
    call = out[0]["tool_calls"][0]
    assert call["id"] == "t1"
    assert call["function"]["name"] == "Write"
    assert call["function"]["arguments"]["file_path"] == "src/a.py"


def test_paths_outside_the_workspace_stay_absolute():
    """So the server's _GLOBAL_PATH_RE discards them and they are never attested."""
    out = adapt.to_messages([_assistant([
        {"type": "tool_use", "id": "t1", "name": "Write",
         "input": {"file_path": "C:/Other/secret.py", "content": "x = 1\n"}}])], WS)
    assert out[0]["tool_calls"][0]["function"]["arguments"]["file_path"] == \
        "C:/Other/secret.py"


def test_tab_numbered_read_results_are_stripped():
    out = adapt.to_messages([_tool_result("t1", "     1\timport os\n     2\tx = 1")], WS)
    assert out[0]["content"] == "import os\nx = 1"


def test_prose_that_merely_starts_with_a_number_is_not_stripped():
    out = adapt.to_messages([_tool_result("t1", "1\tfirst\nplain line\nanother line")], WS)
    assert "1\tfirst" in out[0]["content"]


def test_thinking_blocks_are_dropped_including_signature():
    out = adapt.to_messages([_assistant([
        {"type": "thinking", "thinking": "", "signature": "A" * 2000},
        {"type": "text", "text": "done"}])], WS)
    assert "A" * 100 not in str(out)
    assert out[0]["content"] == "done"


def test_images_become_placeholders_inside_tool_results():
    out = adapt.to_messages([_tool_result("t1", [
        {"type": "image", "source": {"data": "B" * 5000}},
        {"type": "text", "text": "ok"}])], WS)
    assert "B" * 100 not in str(out)
    assert "[image removed before upload]" in out[0]["content"]


def test_every_message_carries_client_ts():
    """Without this the server's velocity clock flags the candidate (§7.3)."""
    out = adapt.to_messages([_assistant([{"type": "text", "text": "hi"}],
                                        ts="2026-08-11T10:11:12Z")], WS)
    assert out[0]["client_ts"] == "2026-08-11T10:11:12Z"


def test_entry_without_a_timestamp_omits_client_ts_rather_than_guessing():
    entry = _assistant([{"type": "text", "text": "hi"}])
    del entry["timestamp"]
    out = adapt.to_messages([entry], WS)
    assert "client_ts" not in out[0]


def test_non_conversation_entry_types_are_dropped():
    entries = [{"type": t, "cwd": "C:/Dev/ws"} for t in
               ("mode", "permission-mode", "ai-title", "file-history-snapshot",
                "attachment", "queue-operation", "agent-name", "last-prompt")]
    assert adapt.to_messages(entries, WS) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_adapt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cadra.adapt'`

- [ ] **Step 3: Implement the adapter**

```python
# plugins/cadra-trace-tracker/scripts/cadra/adapt.py
"""Claude Code JSONL -> canonical OpenAI-style messages (spec §7).

All Claude Code format knowledge lives here so the server stays agent-agnostic.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from cadra.collect import Session

PATH_ARG_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path")
_LINENO_RE = re.compile(r"^\s*\d+\t")
IMAGE_PLACEHOLDER = "[image removed before upload]"


def rebase_path(value: str, workspace: Path) -> str:
    """Absolute paths under the workspace become relative; others stay absolute so
    the server's _GLOBAL_PATH_RE discards them (§7.1)."""
    if not isinstance(value, str) or not value:
        return value
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            return value.replace("\\", "/")
        root = Path(str(workspace))
        resolved = Path(str(candidate))
        if resolved == root or root in resolved.parents:
            return str(resolved.relative_to(root)).replace("\\", "/")
    except (OSError, ValueError):
        return value
    return value.replace("\\", "/")


def strip_line_numbers(text: str) -> str:
    """Claude Code prefixes read results with '<n>\\t' (§7.2)."""
    if not isinstance(text, str) or "\t" not in text:
        return text
    lines = text.split("\n")
    hits = sum(1 for line in lines if _LINENO_RE.match(line))
    if hits / max(len(lines), 1) < 0.8:
        return text
    return "\n".join(_LINENO_RE.sub("", line, count=1) for line in lines)


def _flatten_result(content: object) -> str:
    if isinstance(content, str):
        return strip_line_numbers(content)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            parts.append(IMAGE_PLACEHOLDER)
        elif isinstance(block.get("text"), str):
            parts.append(strip_line_numbers(block["text"]))
    return "\n".join(parts)


def to_messages(entries: list[dict], workspace: Path,
                subagent: bool = False) -> list[dict]:
    messages: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        ts = entry.get("timestamp")
        role = message.get("role") or entry.get("type")
        content = message.get("content")

        if isinstance(content, str):
            if role in ("user", "assistant") and content:
                out = {"role": role, "content": content}
                if isinstance(ts, str) and ts:
                    out["client_ts"] = ts
                messages.append(out)
            continue
        if not isinstance(content, list):
            continue

        if role == "assistant":
            tool_calls, texts = [], []
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "thinking":
                    continue  # dropped whole, including `signature` (§9.2)
                if kind == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif kind == "image":
                    texts.append(IMAGE_PLACEHOLDER)
                elif kind == "tool_use":
                    args = dict(block.get("input") or {})
                    for key in PATH_ARG_KEYS:
                        if isinstance(args.get(key), str):
                            args[key] = rebase_path(args[key], workspace)
                    call = {"id": block.get("id"), "type": "function",
                            "function": {"name": block.get("name"), "arguments": args}}
                    if subagent:
                        call["cadra_agent"] = {"agent_type": "subagent"}
                    tool_calls.append(call)
            out = {"role": "assistant"}
            if texts:
                out["content"] = "\n".join(texts)
            if tool_calls:
                out["tool_calls"] = tool_calls
            if len(out) > 1:
                if isinstance(ts, str) and ts:
                    out["client_ts"] = ts
                messages.append(out)

        elif role == "user":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    out = {"role": "tool", "tool_call_id": block.get("tool_use_id"),
                           "content": _flatten_result(block.get("content"))}
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    out = {"role": "user", "content": block["text"]}
                elif block.get("type") == "image":
                    out = {"role": "user", "content": IMAGE_PLACEHOLDER}
                else:
                    continue
                if isinstance(ts, str) and ts:
                    out["client_ts"] = ts
                messages.append(out)
    return messages


def _read_entries(path: Path) -> list[dict]:
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        return entries
    return entries


def load_session(session: Session, workspace: Path) -> tuple[list[dict], dict]:
    """Main transcript plus subagents, interleaved by timestamp (§6.2)."""
    main = _read_entries(session.main_file)
    tagged: list[tuple[str, dict, bool]] = [
        (str(e.get("timestamp") or ""), e, False) for e in main
    ]
    for sub_file in session.subagent_files:
        for entry in _read_entries(sub_file):
            tagged.append((str(entry.get("timestamp") or ""), entry, True))
    tagged.sort(key=lambda item: item[0])

    messages: list[dict] = []
    cwds: list[str] = []
    version = ""
    for _ts, entry, is_sub in tagged:
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd and cwd not in cwds:
            cwds.append(cwd)
        if not version and isinstance(entry.get("version"), str):
            version = entry["version"]
        messages.extend(to_messages([entry], workspace, subagent=is_sub))

    stamps = [t for t, _e, _s in tagged if t]
    meta = {"cwds": cwds, "started_at": stamps[0] if stamps else None,
            "ended_at": stamps[-1] if stamps else None, "agent_version": version}
    return messages, meta
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_adapt.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add plugins/cadra-trace-tracker/scripts/cadra/adapt.py tests/test_adapt.py
git commit -m "feat: transform Claude Code transcripts to the canonical envelope"
```

---

### Task 4: Redaction (vendored copy)

**Files:**
- Create: `plugins/cadra-trace-tracker/scripts/cadra/redact.py`
- Test: `tests/test_redact.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RULES_VERSION = "1"`, `redact_text(text) -> tuple[str, int]`, `redact_messages(messages) -> tuple[list[dict], set[str], int]` — the `set[str]` is paths whose **write content** was redacted, which must be reported as truncated (§8.1).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_redact.py
"""Client-side redaction (§8). Vendored copy of the canonical proxy module."""
from cadra.redact import RULES_VERSION, redact_messages, redact_text


def test_rules_version_is_declared():
    assert RULES_VERSION == "1"


def test_tokens_are_redacted():
    for secret in ("sk-abcdefghijklmnopqrstuvwxyz012345",
                   "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                   "AKIAIOSFODNN7EXAMPLE"):
        out, n = redact_text(f"value {secret} end")
        assert secret not in out and n == 1


def test_assignment_keeps_key_drops_value():
    out, n = redact_text("DATABASE_PASSWORD=hunter2supersecret")
    assert out.startswith("DATABASE_PASSWORD=") and "hunter2" not in out and n == 1


def test_git_sha_and_uuid_survive():
    """False positives here would damage attestation coverage."""
    for safe in ("9ee8167a1b2c3d4e5f60718293a4b5c6d7e8f900",
                 "62cda7a2-399b-462b-8b92-11472cd32750"):
        out, n = redact_text(f"id {safe}")
        assert safe in out and n == 0


def test_tool_result_bodies_are_redacted():
    messages = [{"role": "tool", "tool_call_id": "t1",
                 "content": "AWS_SECRET=abcdef123456789"}]
    out, paths, n = redact_messages(messages)
    assert n == 1 and paths == set()
    assert "abcdef123456789" not in out[0]["content"]


def test_redacted_write_content_reports_its_path():
    """Redacting a write changes the lines attestation hashes, so the file must be
    excluded from coverage rather than scored as unattested (§8.1)."""
    messages = [{"role": "assistant", "tool_calls": [{
        "id": "t1", "type": "function", "function": {
            "name": "Write", "arguments": {
                "file_path": "src/config.py",
                "content": "API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\n"}}}]}]
    out, paths, n = redact_messages(messages)
    assert n >= 1
    assert paths == {"src/config.py"}
    assert "sk-abcdefghij" not in str(out)


def test_clean_write_content_reports_no_path():
    messages = [{"role": "assistant", "tool_calls": [{
        "id": "t1", "type": "function", "function": {
            "name": "Write", "arguments": {"file_path": "src/a.py",
                                           "content": "x = 1\n"}}}]}]
    _out, paths, n = redact_messages(messages)
    assert paths == set() and n == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_redact.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cadra.redact'`

- [ ] **Step 3: Implement the redactor**

Copy the rule engine from the ingest plan's Task 8 verbatim — `RULES_VERSION`, `_TOKEN_PATTERNS`, `_ASSIGNMENT_RE`, `_SAFE_SHAPES`, `_ENTROPY_CANDIDATE_RE`, `_shannon_bits`, `_is_safe_shape`, `redact_text` — into `plugins/cadra-trace-tracker/scripts/cadra/redact.py` with this header, then add the plugin-specific `redact_messages`:

```python
"""Secret redaction (spec §8). VENDORED COPY.

Canonical source is cadra-prototype/proxy/redact.py. Edit order on any rules
change: proxy -> backend -> plugin, then bump RULES_VERSION in all three.
Do not add rules here first.
"""
```

```python
WRITE_CONTENT_KEYS = ("content", "new_string", "file_text", "code_edit", "newString")
PATH_ARG_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path")


def redact_messages(messages: list[dict]) -> tuple[list[dict], set[str], int]:
    """Redact in place. Returns (messages, redacted_write_paths, count).

    Write-argument redaction changes the very lines attestation matches against the
    repo, so those paths are reported and later marked truncated — the file leaves
    the coverage denominator instead of scoring as unattested (§8.1).
    """
    total = 0
    redacted_paths: set[str] = set()
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"], n = redact_text(content)
            total += n
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            args = (call.get("function") or {}).get("arguments")
            if not isinstance(args, dict):
                continue
            path = next((args[k] for k in PATH_ARG_KEYS
                         if isinstance(args.get(k), str)), None)
            touched = 0
            for key in WRITE_CONTENT_KEYS:
                if isinstance(args.get(key), str):
                    args[key], n = redact_text(args[key])
                    touched += n
            for edit in args.get("edits") or []:
                if isinstance(edit, dict):
                    for key in WRITE_CONTENT_KEYS:
                        if isinstance(edit.get(key), str):
                            edit[key], n = redact_text(edit[key])
                            touched += n
            if touched and isinstance(path, str):
                redacted_paths.add(path)
            total += touched
    return messages, redacted_paths, total
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_redact.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add plugins/cadra-trace-tracker/scripts/cadra/redact.py tests/test_redact.py
git commit -m "feat: vendor client-side secret redaction"
```

---

### Task 5: Size controls and chunking

**Files:**
- Create: `plugins/cadra-trace-tracker/scripts/cadra/envelope.py`
- Test: `tests/test_envelope.py`

**Interfaces:**
- Consumes: nothing. It operates on already-transformed messages, so it imports
  neither `cadra.adapt` nor `cadra.redact`; Task 6 is what wires the order.
- Produces:
  - `MAX_WRITE_LINES = 2000`, `TOOL_RESULT_CAP = 64 * 1024`, `CHUNK_BYTES = 4 * 1024 * 1024`
  - `apply_size_controls(messages: list[dict]) -> tuple[list[dict], set[str]]`
  - `chunk_hash(messages: list[dict]) -> str`
  - `build_chunks(messages: list[dict]) -> list[tuple[dict, list[dict]]]` — `(chunk_meta, messages)` pairs with `index`, `total`, `prefix_hash`, `chunk_hash`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_envelope.py
"""Size controls and chunking (§9)."""
from cadra import envelope


def _write(content: str, path: str = "src/a.py") -> dict:
    return {"role": "assistant", "tool_calls": [{
        "id": "t1", "type": "function",
        "function": {"name": "Write",
                     "arguments": {"file_path": path, "content": content}}}]}


def test_oversize_write_is_truncated_and_reported():
    body = "\n".join(f"line{i} = {i}" for i in range(envelope.MAX_WRITE_LINES + 500))
    out, truncated = envelope.apply_size_controls([_write(body)])
    content = out[0]["tool_calls"][0]["function"]["arguments"]["content"]
    assert content.count("\n") < envelope.MAX_WRITE_LINES + 10
    assert truncated == {"src/a.py"}


def test_write_within_the_cap_is_untouched():
    body = "\n".join(f"line{i} = {i}" for i in range(10))
    out, truncated = envelope.apply_size_controls([_write(body)])
    assert out[0]["tool_calls"][0]["function"]["arguments"]["content"] == body
    assert truncated == set()


def test_oversize_tool_result_is_capped_with_a_marker():
    out, _ = envelope.apply_size_controls(
        [{"role": "tool", "tool_call_id": "t1", "content": "x" * (70 * 1024)}])
    assert len(out[0]["content"].encode()) < envelope.TOOL_RESULT_CAP + 200
    assert "[truncated by cadra capture]" in out[0]["content"]


def test_single_small_session_is_one_chunk():
    chunks = envelope.build_chunks([{"role": "user", "content": "hi"}])
    assert len(chunks) == 1
    meta, _messages = chunks[0]
    assert meta == {"index": 0, "total": 1, "prefix_hash": "",
                    "chunk_hash": envelope.chunk_hash([{"role": "user",
                                                        "content": "hi"}])}


def test_large_session_splits_and_chains():
    big = [{"role": "user", "content": "x" * 200_000} for _ in range(30)]
    chunks = envelope.build_chunks(big)
    assert len(chunks) > 1
    assert chunks[0][0]["prefix_hash"] == ""
    seen: list[dict] = []
    for meta, messages in chunks:
        assert meta["prefix_hash"] == (envelope.chunk_hash(seen) if seen else "")
        seen.extend(messages)
    assert sum(len(m) for _meta, m in chunks) == len(big)


def test_a_single_message_larger_than_the_chunk_is_not_dropped():
    huge = [{"role": "user", "content": "x" * (envelope.CHUNK_BYTES + 1000)}]
    chunks = envelope.build_chunks(huge)
    assert sum(len(m) for _meta, m in chunks) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_envelope.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cadra.envelope'`

- [ ] **Step 3: Implement size controls and chunking**

```python
# plugins/cadra-trace-tracker/scripts/cadra/envelope.py
"""Size controls and chunking (spec §9).

Measured on real transcripts: image stripping and dropping thinking blocks do the
work (both handled in adapt.py); the caps below never fired on any observed
session and exist as bounded-cost insurance against a pathological one.
"""
from __future__ import annotations

import hashlib
import json

MAX_WRITE_LINES = 2000          # == MAX_LINES_PER_EVENT, the extractor's hashing cap
TOOL_RESULT_CAP = 64 * 1024
CHUNK_BYTES = 4 * 1024 * 1024
TRUNCATION_MARKER = "[truncated by cadra capture]"

WRITE_CONTENT_KEYS = ("content", "new_string", "file_text", "code_edit", "newString")
PATH_ARG_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path")


def _truncate_lines(text: str, max_lines: int) -> tuple[str, bool]:
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text, False
    return "\n".join(lines[:max_lines]) + f"\n{TRUNCATION_MARKER}", True


def apply_size_controls(messages: list[dict]) -> tuple[list[dict], set[str]]:
    """-> (messages, paths whose write content was truncated)."""
    truncated: set[str] = set()
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if (message.get("role") == "tool" and isinstance(content, str)
                and len(content.encode("utf-8")) > TOOL_RESULT_CAP):
            half = TOOL_RESULT_CAP // 2
            message["content"] = f"{content[:half]}\n{TRUNCATION_MARKER}\n{content[-half:]}"
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            args = (call.get("function") or {}).get("arguments")
            if not isinstance(args, dict):
                continue
            path = next((args[k] for k in PATH_ARG_KEYS
                         if isinstance(args.get(k), str)), None)
            hit = False
            for key in WRITE_CONTENT_KEYS:
                if isinstance(args.get(key), str):
                    args[key], did = _truncate_lines(args[key], MAX_WRITE_LINES)
                    hit = hit or did
            for edit in args.get("edits") or []:
                if isinstance(edit, dict):
                    for key in WRITE_CONTENT_KEYS:
                        if isinstance(edit.get(key), str):
                            edit[key], did = _truncate_lines(edit[key], MAX_WRITE_LINES)
                            hit = hit or did
            if hit and isinstance(path, str):
                truncated.add(path)
    return messages, truncated


def chunk_hash(messages: list[dict]) -> str:
    payload = json.dumps(messages, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_chunks(messages: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Split into <= CHUNK_BYTES groups, chained by prefix_hash.

    A single message larger than the limit gets its own chunk rather than being
    dropped — correctness beats the size guarantee.
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for message in messages or []:
        nbytes = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        if current and size + nbytes > CHUNK_BYTES:
            groups.append(current)
            current, size = [], 0
        current.append(message)
        size += nbytes
    if current or not groups:
        groups.append(current)

    out: list[tuple[dict, list[dict]]] = []
    seen: list[dict] = []
    for index, group in enumerate(groups):
        meta = {"index": index, "total": len(groups),
                "prefix_hash": chunk_hash(seen) if seen else "",
                "chunk_hash": chunk_hash(group)}
        out.append((meta, group))
        seen = seen + group
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_envelope.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add plugins/cadra-trace-tracker/scripts/cadra/envelope.py tests/test_envelope.py
git commit -m "feat: apply size controls and build chained chunks"
```

---

### Task 6: `cadra-submit` and `cadra-traces`

**Files:**
- Create: `plugins/cadra-trace-tracker/scripts/cadra/client.py`
- Create: `plugins/cadra-trace-tracker/scripts/cadra_submit.py`
- Create: `plugins/cadra-trace-tracker/scripts/cadra_traces.py`
- Create: `plugins/cadra-trace-tracker/skills/cadra-submit/SKILL.md`
- Create: `plugins/cadra-trace-tracker/skills/cadra-traces/SKILL.md`
- Test: `tests/test_submit.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces:
  - `cadra.client.post_chunk(*, base_url, token, body) -> tuple[int, dict]`
  - `cadra.client.get_traces(*, base_url, token) -> tuple[int, dict]`
  - `cadra_submit.main(argv) -> int`
  - `cadra_traces.main(argv) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_submit.py
"""End-to-end submission against a stubbed proxy (§5.2, §10)."""
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
        return 202, {"accepted": True, "session_id": body["session_id"],
                     "messages": len(body["messages"]), "received_at": "now"}

    monkeypatch.setattr(cadra_submit.client, "post_chunk", _post)
    code = cadra_submit.main(["--workspace", str(connected),
                              "--projects-root", str(transcripts)])
    assert code == 0
    body = sent[0]
    assert body["session_id"] == "sess1"
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_submit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cadra_submit'`

- [ ] **Step 3: Implement the HTTP client**

```python
# plugins/cadra-trace-tracker/scripts/cadra/client.py
"""HTTP client for the Cadra proxy. Standard library only."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

TIMEOUT_S = 120


def _call(method: str, url: str, token: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read().decode("utf-8") or "{}")
        except Exception:
            return err.code, {"error": {"code": f"http_{err.code}",
                                        "message": "request failed"}}
    except Exception as exc:
        return 0, {"error": {"code": "network_error", "message": type(exc).__name__}}


def post_chunk(*, base_url: str, token: str, body: dict) -> tuple[int, dict]:
    return _call("POST", f"{base_url.rstrip('/')}/v1/traces", token, body)


def get_traces(*, base_url: str, token: str) -> tuple[int, dict]:
    return _call("GET", f"{base_url.rstrip('/')}/v1/traces", token, None)
```

- [ ] **Step 4: Implement submit**

```python
# plugins/cadra-trace-tracker/scripts/cadra_submit.py
"""cadra-submit — collect, transform, redact and send this workspace's sessions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cadra import adapt, client, collect, config, envelope, redact

CAPTURE_VERSION = "3.0.0"


def _projects_root(override: str | None) -> Path:
    return Path(override) if override else Path.home() / ".claude" / "projects"


def _state_path(workspace: Path) -> Path:
    return config.config_dir(workspace) / "state.json"


def _load_state(workspace: Path) -> dict:
    try:
        with open(_state_path(workspace), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _save_state(workspace: Path, state: dict) -> None:
    config.config_dir(workspace).mkdir(parents=True, exist_ok=True)
    with open(_state_path(workspace), "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--projects-root", default=None)
    args = parser.parse_args(argv)

    workspace = config.find_workspace(Path(args.workspace))
    if workspace is None:
        print("NOT CONNECTED: this workspace has no Cadra configuration. "
              "Run cadra-connect first.")
        return 1
    cfg = config.load(workspace)
    state = _load_state(workspace)

    sessions, notes = collect.discover(_projects_root(args.projects_root), workspace)
    for note in notes:
        print(f"NOTE {note}")
    if not sessions:
        print("Nothing to submit — no sessions were found for this workspace.")
        return 0

    preview = {"sessions": []}
    failures = 0
    submitted = 0

    for session in sessions:
        messages, meta = adapt.load_session(session, workspace)
        messages, redacted_paths, redacted_count = redact.redact_messages(messages)
        messages, truncated_paths = envelope.apply_size_controls(messages)
        truncated = sorted(truncated_paths | redacted_paths)
        fingerprint = envelope.chunk_hash(messages)
        if state.get(session.session_id) == fingerprint:
            print(f"SKIP {session.session_id[:8]} — already submitted, unchanged")
            continue

        chunks = envelope.build_chunks(messages)
        preview["sessions"].append({
            "session_id": session.session_id, "messages": len(messages),
            "chunks": len(chunks), "cwds": meta["cwds"],
            "redacted": redacted_count, "truncated_paths": truncated,
        })

        receipt: dict = {}
        ok = True
        for chunk_meta, chunk_messages in chunks:
            body = {
                "capture_version": CAPTURE_VERSION,
                "agent": {"name": "claude-code", "version": meta["agent_version"]},
                "session_id": session.session_id,
                "started_at": meta["started_at"], "ended_at": meta["ended_at"],
                "cwds": meta["cwds"],
                "workspace_root": str(workspace).replace("\\", "/"),
                "binding": {"workspace_root": str(workspace).replace("\\", "/"),
                            "git_remote": cfg.get("git_remote")},
                "chunk": chunk_meta,
                "redaction": {"rules_version": redact.RULES_VERSION,
                              "redacted_count": redacted_count},
                "truncated_paths": truncated,
                "messages": chunk_messages,
            }
            status, payload = client.post_chunk(
                base_url=cfg["proxy_base_url"], token=cfg["token"], body=body)
            if status not in (200, 202):
                error = payload.get("error") or {}
                print(f"FAILED {session.session_id[:8]} — "
                      f"{error.get('code', 'unknown')}: {error.get('message', '')}")
                ok = False
                failures += 1
                break
            receipt = payload
        if ok:
            state[session.session_id] = fingerprint
            submitted += 1
            print(f"SUBMITTED {session.session_id[:8]} — server confirmed "
                  f"{receipt.get('messages', '?')} messages at "
                  f"{receipt.get('received_at', '?')}")

    with open(config.config_dir(workspace) / "last-preview.json", "w",
              encoding="utf-8") as handle:
        json.dump(preview, handle, indent=2)
    _save_state(workspace, state)

    if submitted == 0 and failures == 0:
        print("Everything is already submitted — no new work since the last submission.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Implement traces**

```python
# plugins/cadra-trace-tracker/scripts/cadra_traces.py
"""cadra-traces — read-only view of what the server holds."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cadra import client, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args(argv)

    workspace = config.find_workspace(Path(args.workspace))
    if workspace is None:
        print("NOT CONNECTED: run cadra-connect first.")
        return 1
    cfg = config.load(workspace)
    status, payload = client.get_traces(base_url=cfg["proxy_base_url"],
                                        token=cfg["token"])
    if status != 200:
        error = payload.get("error") or {}
        print(f"UNAVAILABLE: could not reach Cadra "
              f"({error.get('code', f'http_{status}')}).")
        return 1
    sessions = payload.get("sessions") or []
    if not sessions:
        print("No sessions are stored yet. Say 'submit my trace' to send your work.")
        return 0
    print(f"STORED SESSIONS ({len(sessions)}):")
    for row in sessions:
        print(f"  {str(row.get('submitted_at', ''))[:16]}  "
              f"{row.get('message_count', '?')} messages  "
              f"{int(row.get('bytes', 0)) // 1024} KB  "
              f"id={str(row.get('session_id', ''))[:8]}  "
              f"binding={row.get('binding_status', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/ -q`
Expected: PASS (all tests)

- [ ] **Step 7: Write the two skills**

```markdown
<!-- plugins/cadra-trace-tracker/skills/cadra-submit/SKILL.md -->
---
description: Submit this workspace's Claude Code work sessions to Cadra for assessment. Use when the user says "submit my trace", "save my work to Cadra", "send my session", "sync my traces", or wants to make sure their assessment work is recorded.
---

# Skill: cadra-submit
**Plugin:** cadra-trace-tracker

## Purpose

Collect the sessions run in this workspace, convert and redact them, and submit
them to Cadra with a server-confirmed receipt.

## Preconditions

The workspace must be connected (`.cadra/config.json` present). If it is not, run
the cadra-connect skill instead.

## Steps

1. Run the submit script and capture ALL output:

   ```
   python "$CLAUDE_PLUGIN_ROOT/scripts/cadra_submit.py" --workspace "<WORKSPACE>"
   ```

2. Report the result in plain language:
   - `SUBMITTED` lines → how many sessions were sent, and the server's confirmed
     message count. This is the server's receipt, not a local claim.
   - `SKIP` lines → already submitted and unchanged.
   - `NOTE` lines → transcripts that were examined and left out (started in a
     sibling directory, or carrying no recorded working directory). Tell the
     user, and mention that starting `claude` from the workspace root keeps
     future sessions in scope. Sessions started *above* the workspace are out of
     scope and are not detected at all — see design §6.1.
   - `FAILED` lines → say which session failed and why in one sentence. Common
     cases: no internet (retry later, nothing is lost); `rejected_revoked` or
     `rejected_expired` (contact the program team); `binding_mismatch` (this
     workspace's git remote differs from the one registered).
   - "Everything is already submitted" → say exactly that.

3. If the user asks what was sent, point them at `.cadra/last-preview.json`, which
   records the sessions, the directories each visited, and how many secrets were
   redacted.

## Rules

- Always run the script. Never claim work is submitted without a server receipt.
- Never print the token.
- The current session's final turns are not included — the transcript is written
  as the session progresses. Mention this only if the user asks.
```

```markdown
<!-- plugins/cadra-trace-tracker/skills/cadra-traces/SKILL.md -->
---
description: Show which of the user's work sessions Cadra has stored. Use when the user says "show my traces", "what have I submitted", "is my work saved", or "list my Cadra sessions".
---

# Skill: cadra-traces
**Plugin:** cadra-trace-tracker

## Purpose

Read-only view of what the server actually holds. Never uploads anything.

## Steps

1. Run:

   ```
   python "$CLAUDE_PLUGIN_ROOT/scripts/cadra_traces.py" --workspace "<WORKSPACE>"
   ```

2. Render one compact line per stored session — date, message count, size. Keep it
   a status glance, not a report. Never show transcript content.
3. If the output says `UNAVAILABLE`, say the server could not be reached and do
   not guess from local files.
4. If nothing is stored, say so and suggest "submit my trace".

## Rules

- Read-only. Never trigger a submission from this skill — point at cadra-submit.
- Show only what the command returned; never invent entries.
```

- [ ] **Step 8: Commit**

```bash
git add plugins/cadra-trace-tracker/scripts plugins/cadra-trace-tracker/skills tests/test_submit.py
git commit -m "feat: add cadra-submit and cadra-traces skills"
```

---

### Task 7: Remove the legacy plugin and verify the write boundary

Deleted last, deliberately: the old path stays intact until the new one is proven, so there is no window in which neither works.

**Files:**
- Delete: `plugins/cadra-trace-tracker/hooks/hooks.json`
- Delete: `plugins/cadra-trace-tracker/scripts/trace_hook.py`, `trace-hook.ps1`, `submit_traces.py`, `submit-traces.ps1`
- Delete: `plugins/cadra-trace-tracker/skills/register-user/`, `save-trace/`, `my-traces/`
- Modify: `plugins/cadra-trace-tracker/.claude-plugin/plugin.json`
- Modify: `README.md`
- Test: `tests/test_write_boundary.py`

**Interfaces:**
- Consumes: everything.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_write_boundary.py
"""The load-bearing invariant: writes only inside .cadra/ plus one .gitignore
append (§4). Also asserts the legacy plugin surface is gone."""
from pathlib import Path

import pytest

import cadra_submit
from cadra import collect, config
from tests.conftest import write_jsonl

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cadra-trace-tracker"


def test_no_hooks_are_shipped():
    assert not (PLUGIN / "hooks").exists()


def test_no_powershell_scripts_remain():
    assert list(PLUGIN.rglob("*.ps1")) == []


def test_no_supabase_credentials_remain():
    for path in PLUGIN.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "supabase.co" not in text
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in text


def test_legacy_skills_are_removed():
    for name in ("register-user", "save-trace", "my-traces"):
        assert not (PLUGIN / "skills" / name).exists()


def test_submission_writes_only_inside_cadra(workspace: Path, transcripts: Path,
                                             monkeypatch):
    config.save(workspace, {"token": "tok", "workspace_root": str(workspace),
                            "proxy_base_url": "https://proxy.test"})
    enc = collect.encode_dir_name(Path(str(workspace)).resolve())
    write_jsonl(transcripts / enc / "s1.jsonl", [
        {"type": "user", "cwd": str(workspace), "sessionId": "s1",
         "timestamp": "2026-08-11T09:00:00Z",
         "message": {"role": "user", "content": "hi"}}])
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (202, {"accepted": True, "session_id": "s1",
                                            "messages": 1, "received_at": "n"}))

    before = {p for p in workspace.rglob("*") if p.is_file()}
    cadra_submit.main(["--workspace", str(workspace),
                       "--projects-root", str(transcripts)])
    after = {p for p in workspace.rglob("*") if p.is_file()}

    for path in after - before:
        relative = path.relative_to(workspace)
        assert relative.parts[0] == ".cadra", f"wrote outside .cadra/: {relative}"


def test_transcripts_are_never_modified(workspace: Path, transcripts: Path,
                                        monkeypatch):
    config.save(workspace, {"token": "tok", "workspace_root": str(workspace),
                            "proxy_base_url": "https://proxy.test"})
    enc = collect.encode_dir_name(Path(str(workspace)).resolve())
    jsonl = transcripts / enc / "s1.jsonl"
    write_jsonl(jsonl, [{"type": "user", "cwd": str(workspace), "sessionId": "s1",
                         "timestamp": "2026-08-11T09:00:00Z",
                         "message": {"role": "user", "content": "hi"}}])
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (202, {"accepted": True, "session_id": "s1",
                                            "messages": 1, "received_at": "n"}))
    original = jsonl.read_bytes()
    mtime = jsonl.stat().st_mtime
    cadra_submit.main(["--workspace", str(workspace),
                       "--projects-root", str(transcripts)])
    assert jsonl.read_bytes() == original
    assert jsonl.stat().st_mtime == mtime
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_write_boundary.py -q`
Expected: FAIL on `test_no_hooks_are_shipped` — the legacy files are still present.

- [ ] **Step 3: Delete the legacy surface**

```bash
git rm -r plugins/cadra-trace-tracker/hooks
git rm plugins/cadra-trace-tracker/scripts/trace_hook.py plugins/cadra-trace-tracker/scripts/trace-hook.ps1
git rm plugins/cadra-trace-tracker/scripts/submit_traces.py plugins/cadra-trace-tracker/scripts/submit-traces.ps1
git rm -r plugins/cadra-trace-tracker/skills/register-user plugins/cadra-trace-tracker/skills/save-trace plugins/cadra-trace-tracker/skills/my-traces
```

- [ ] **Step 4: Update the manifest**

```json
{
  "name": "cadra-trace-tracker",
  "version": "3.0.0",
  "description": "Submit your Claude Code work sessions to Cadra for assessment. Skills only — nothing is captured or sent unless you ask. All state lives in the workspace's .cadra/ folder, which is gitignored.",
  "author": { "name": "Cadra", "email": "nishant@cadra.info" }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Rewrite the README**

Replace `README.md` with a description of the three skills, the `.cadra/`
filesystem contract, the privacy model (skills-only, explicit submission, on-disk
preview, no database credentials), and the install commands. Remove every
reference to hooks, `_traces/`, `.claude-project`, roll numbers and Supabase.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: remove legacy hooks, PowerShell and Supabase surface"
```

---

### Task 8: Final verification

**Files:** none modified.

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 2: Confirm no third-party imports in shipped code**

Add this test rather than running an ad-hoc command, so the constraint stays
enforced:

```python
# tests/test_no_third_party_imports.py
"""Shipped code must import only the standard library — candidates run this on
their own machines and a pip install is a support burden and a failure mode."""
import ast
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cadra-trace-tracker"
ALLOWED = set(sys.stdlib_module_names) | {"cadra"}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_shipped_code_imports_stdlib_only():
    offenders: dict[str, set[str]] = {}
    for path in PLUGIN.rglob("*.py"):
        extra = _imported_roots(path) - ALLOWED
        if extra:
            offenders[str(path.relative_to(PLUGIN))] = extra
    assert offenders == {}, f"third-party imports in shipped code: {offenders}"
```

Run: `python -m pytest tests/test_no_third_party_imports.py -q`
Expected: PASS

- [ ] **Step 3: Record what still needs the proxy**

The proxy half is **not** built. Write a handoff note listing what cannot be
verified until it is:
- `cadra-connect` against a live `GET /v1/traces` (currently stubbed in tests)
- a real chunked submission and its receipt
- `binding_mismatch` behaviour on a changed git remote
- that submitted traces produce non-zero attestations server-side

- [ ] **Step 4: Stop**

Do not merge into `main` and do not push. Both are Varun's calls.

---

## Self-Review

**Spec coverage:** §2 decisions → D1 Task 7, D2/D3 Task 1, D4/D5 Task 3, D6 Task 4, D7 Tasks 1 and 7, D8 Task 2, D9 Task 7. §3 → Task 7. §4 → Tasks 1, 7. §5.1 → Task 1. §5.2/§5.3 → Task 6. §6.1/§6.1.1 → Task 2. §6.2 → Tasks 2, 3. §6.3 → Task 6 (state fingerprint). §7 → Task 3. §7.3 → Task 3. §7.4 → Task 6. §8/§8.0/§8.1 → Task 4. §9 → Task 5. §10 → Task 6. §11 → every task. §13.1 → dashboard deliverable, outside this repo. §13.2 → no code needed.

**Deliberately deferred:** §13.1's onboarding checklist line lives in `cadra-prototype/docs/candidate-onboarding.md`, which this plan does not touch, per the instruction to leave that repo alone.

**Type consistency:** `Session(session_id, main_file, origin_cwd, subagent_files)` — defined Task 2, consumed Tasks 3 and 6. `to_messages(entries, workspace, subagent=False)` — Task 3, consumed by `load_session`. `redact_messages -> (messages, paths, count)` — Task 4, consumed Task 6. `apply_size_controls -> (messages, paths)` and `build_chunks -> [(meta, messages)]` — Task 5, consumed Task 6. `post_chunk(base_url, token, body)` — Task 6, stubbed identically in every test.

---

## Status — 2026-08-12

All eight tasks implemented on `byo-capture-rebuild`. Full suite: 75 passing, no
test makes a network call. Not merged and not pushed — both are Varun's calls.

Amendments made during task reviews, which override the plan text where they
differ:

- `cadra_submit.py` takes `--dry-run`. The skill runs it first and requires the
  user's agreement before the real send, because the preview is worthless after
  the fact (Task 6 review).
- The envelope is nested exactly as spec §7.4 — `session` object, and `binding`
  carrying `workspace_root` / `git_remote` / `git_branch`. Task 6's plan text
  and test both said flat; §7.4 wins, since the companion ingest spec defers to
  it as canonical.
- Discovery never looks above the workspace, and the "sessions started
  elsewhere" report was dropped as undeliverable without a content read that
  §6.1.1 forbids (Task 2 review). Prevention replaced it: `cadra-connect` tells
  the candidate to launch `claude` from the workspace root.
- `redact.py` carries `# --- BEGIN/END SHARED REDACTION RULES ---` sentinels; the
  cross-repo drift check hashes only the bytes between them, because the plugin
  copy is deliberately not a byte twin of the proxy's (Task 4 review).
- `adapt.load_session` meta carries `unreadable_lines`, printed as a NOTE, per
  §10's "skip it, count it, report it".

## Handoff — what needs the proxy

`POST /v1/traces` and `GET /v1/traces` do not exist. Every network path in this
plugin was developed against stubs. Consolidated from the review notes on this
branch's commits; nothing below has been observed against a real server.

**Connect (`GET /v1/traces`)**

1. A live 200 for a valid token and 401 for a revoked/unknown one — `verify_token`
   treats any other status as a hard failure and saves nothing.
2. The list shape `cadra-traces` renders: `sessions[]` with `submitted_at`,
   `message_count`, `bytes`, `session_id`, `binding_status`.

**Submission (`POST /v1/traces`)**

3. The 202 accept shape: `session_id` echoed, plus `messages` and `received_at`,
   since the `SUBMITTED` line quotes both as the receipt.
4. Which status a **duplicate** chunk returns. The client accepts 200 and 202;
   anything else fails the session and blocks it.
5. That `chunk.prefix_hash` / `chunk_hash` chaining is what the server actually
   validates, and that a 4 MB body survives the proxy and its ingress.
6. Bearer header name, and that the server — not the client — derives the
   assessment id from the token. No `assessment_id` is sent.
7. That the nested §7.4 envelope is the shape the server parses. This is the one
   place where the plan and the spec disagreed; the spec was followed, so it is
   also the most likely integration surprise.

**Error contract**

8. Body shape `{"error": {"code", "message"}}` behind 401, 403 and 409, and the
   codes `rejected_revoked`, `rejected_expired`, `binding_mismatch`. Each maps to
   a specific sentence in the `cadra-submit` skill; a different shape degrades to
   `unknown:` with an empty message.
9. `binding_mismatch` end to end: connect, change the workspace's git remote,
   submit, and confirm the candidate gets a comprehensible message rather than a
   silent rejection.

**Scoring, end to end**

10. That a submitted trace produces **non-zero attestations server-side**.
    `tests/test_extraction_parity.py` proves the envelope yields attestation
    items in `attestation_items_from_payload`, which is the check that would have
    caught the absolute-path defect — but it calls the extractor directly and
    proves nothing about the ingest path in between.
11. That the velocity guard honours `client_ts` (companion spec §14). Without it
    every BYO submission inserts in one burst and every candidate trips
    `prompt_paste_velocity`. The plugin's half is done and tested; the server's
    is not.

**Cross-repo**

12. `proxy/redact.py` and the backend copy do not exist yet (ingest plan Task 8
    unexecuted). They must land carrying the same `BEGIN/END SHARED REDACTION
    RULES` sentinels, or the release drift check has nothing to hash and fails
    loudly by design.
13. §13.1's onboarding checklist line still needs adding to
    `cadra-prototype/docs/candidate-onboarding.md`; this repo deliberately does
    not touch that repo.

**Open, not proxy-dependent**

14. There is no local history of what was submitted when:
    `.cadra/last-preview.json` is overwritten every run. The legacy plugin's
    `tracker.log` was the only deleted behaviour judged worth revisiting
    (Task 7 review).
