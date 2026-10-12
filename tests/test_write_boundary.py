"""The load-bearing invariant: the plugin writes only inside `<workspace>/.cadra/`,
plus one append to `<workspace>/.gitignore` at connect (§4). Also asserts the
legacy plugin surface is gone.

The boundary tests take a full byte-level snapshot of the workspace rather than a
set of paths, so a stray write is caught whether it *creates* a file, *rewrites*
an existing one in place (same name, same size), or *deletes* one. A path-set
diff would miss the last two.
"""
import json
from pathlib import Path

import pytest

import cadra_connect
import cadra_submit
from cadra import collect, config
from tests.conftest import write_jsonl

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cadra-trace-tracker"


# --------------------------------------------------------------------------
# The legacy surface is gone (§3)
# --------------------------------------------------------------------------

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


def test_no_legacy_scripts_remain():
    for name in ("trace_hook.py", "submit_traces.py"):
        assert not (PLUGIN / "scripts" / name).exists()


# --------------------------------------------------------------------------
# The write boundary
# --------------------------------------------------------------------------

def _snapshot(root: Path) -> dict[str, bytes]:
    """Content of every file under `root`, keyed by workspace-relative path."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def _assert_only_cadra_changed(before: dict[str, bytes], after: dict[str, bytes],
                               *, gitignore_may_grow: bool = False) -> None:
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if name.split("/")[0] == ".cadra":
            continue
        if gitignore_may_grow and name == ".gitignore":
            # Append-only: whatever was there must still be there, verbatim.
            assert old is None or (new is not None and new.startswith(old)), \
                ".gitignore was rewritten, not appended to"
            continue
        raise AssertionError(
            f"wrote outside .cadra/: {name} "
            f"({'created' if old is None else 'deleted' if new is None else 'modified'})")


@pytest.fixture
def populated(workspace: Path) -> Path:
    """A workspace that already has files, so in-place rewrites are detectable."""
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / "README.md").write_text("# solution\n", encoding="utf-8")
    (workspace / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    return workspace


@pytest.fixture
def connected(populated: Path, transcripts: Path, monkeypatch) -> Path:
    config.save(populated, {"token": "tok", "workspace_root": str(populated),
                            "proxy_base_url": "https://proxy.test"})
    enc = collect.encode_dir_name(Path(str(populated)).resolve())
    write_jsonl(transcripts / enc / "s1.jsonl", [
        {"type": "user", "cwd": str(populated), "sessionId": "s1",
         "timestamp": "2026-08-11T09:00:00Z",
         "message": {"role": "user", "content": "hi"}}])
    monkeypatch.setattr(cadra_submit.client, "post_chunk",
                        lambda **kw: (202, {"accepted": True, "session_id": "s1",
                                            "messages": 1, "received_at": "n"}))
    return populated


def test_submission_writes_only_inside_cadra(connected: Path, transcripts: Path):
    before = _snapshot(connected)
    code = cadra_submit.main(["--workspace", str(connected),
                             "--projects-root", str(transcripts)])
    assert code == 0
    _assert_only_cadra_changed(before, _snapshot(connected))


def test_dry_run_writes_only_inside_cadra(connected: Path, transcripts: Path):
    """The dry run writes `last-preview.json`, so it has a boundary of its own."""
    before = _snapshot(connected)
    code = cadra_submit.main(["--workspace", str(connected),
                              "--projects-root", str(transcripts), "--dry-run"])
    assert code == 0
    after = _snapshot(connected)
    _assert_only_cadra_changed(before, after)
    assert ".cadra/last-preview.json" in after


def test_connect_writes_only_cadra_and_appends_to_gitignore(populated: Path,
                                                            monkeypatch):
    """The single sanctioned write outside `.cadra/` — and it must be an append."""
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    monkeypatch.setattr(cadra_connect, "git_remote", lambda ws: None)
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    token = (f"{seg({'alg': 'HS256'})}."
             f"{seg({'coding_assessment_id': 'a-1', 'exp': 99999999999})}.sig")

    before = _snapshot(populated)
    cadra_connect.main(["--workspace", str(populated), "--init"])
    cadra_connect.token_path(populated).write_text(token, encoding="utf-8")
    code = cadra_connect.main(["--workspace", str(populated),
                               "--proxy", "https://proxy.test"])
    assert code == 0
    after = _snapshot(populated)
    _assert_only_cadra_changed(before, after, gitignore_may_grow=True)
    assert ".cadra/" in after[".gitignore"].decode()
    # The paste file is consumed, not left lying around as a second copy.
    assert ".cadra/token.txt" not in after


def test_transcripts_are_never_modified(connected: Path, transcripts: Path):
    enc = collect.encode_dir_name(Path(str(connected)).resolve())
    jsonl = transcripts / enc / "s1.jsonl"
    original = jsonl.read_bytes()
    mtime = jsonl.stat().st_mtime
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    assert jsonl.read_bytes() == original
    assert jsonl.stat().st_mtime == mtime


def test_the_projects_root_gains_no_files(connected: Path, transcripts: Path):
    """Reads of ~/.claude/projects are the stated read exception (§4) — reads only."""
    before = _snapshot(transcripts)
    cadra_submit.main(["--workspace", str(connected),
                       "--projects-root", str(transcripts)])
    assert _snapshot(transcripts) == before
