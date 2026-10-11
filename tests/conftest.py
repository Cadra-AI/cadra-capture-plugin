"""Shared fixtures. Every test uses a temp workspace — never the real home dir."""
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path: Path, monkeypatch):
    """Every host's default root lives under the home dir. Without this, a run that
    only overrides one host would read the real ~/.codex, ~/.kiro or ~/.copilot."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_HOME", raising=False)
    for var in ("OPENCODE_DB", "OPENCODE_HOME", "XDG_DATA_HOME", "LOCALAPPDATA",
                "APPDATA", "KIRO_HOME"):
        monkeypatch.delenv(var, raising=False)


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


FIXTURES = Path(__file__).parent / "fixtures"


def _install(src: Path, dest: Path, workspace: Path) -> None:
    """Copy a hand-written synthetic fixture, putting the temp workspace where `{WS}`
    stands. The path is JSON-escaped once; tmp paths here never need a second escape
    for the Codex `arguments` string (no backslashes or quotes on POSIX)."""
    escaped = json.dumps(str(workspace))[1:-1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8").replace("{WS}", escaped),
                    encoding="utf-8")


@pytest.fixture
def codex_root(tmp_path: Path, workspace: Path) -> Path:
    """Stands in for ~/.codex/sessions. Shapes confirmed read-only against local
    rollouts during planning; all content here is synthetic."""
    root = tmp_path / "codex" / "sessions"
    _install(FIXTURES / "codex" / "rollout-synthetic.jsonl",
             root / "2026" / "08" / "11" / "rollout-synthetic.jsonl", workspace)
    return root


@pytest.fixture
def kiro_root(tmp_path: Path, workspace: Path) -> Path:
    """Stands in for ~/.kiro/sessions. Shapes confirmed read-only against local
    sessions during planning; all content here is synthetic."""
    root = tmp_path / "kiro" / "sessions"
    session = root / "h1" / "sess_s1"
    for name in ("session.json", "messages.jsonl", "sub-executions/sub-0001.jsonl"):
        _install(FIXTURES / "kiro" / name, session / name, workspace)
    return root


@pytest.fixture
def copilot_root(tmp_path: Path, workspace: Path) -> Path:
    """Stands in for ~/.copilot/session-state. Only `session.start` was seen locally;
    the message and tool event shapes come from the published Copilot SDK docs and
    were NOT verified against a real transcript."""
    root = tmp_path / "copilot" / "session-state"
    _install(FIXTURES / "copilot" / "events.jsonl",
             root / "00000000-0000-4000-8000-000000000003" / "events.jsonl", workspace)
    (root / ".session-operation-locks").mkdir()
    return root


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
CURSOR_UUID = "00000000-0000-4000-8000-0000000000c1"
@pytest.fixture
def cursor_root(tmp_path: Path, workspace: Path) -> Path:
    """Stands in for ~/.cursor. Shapes observed read-only on a real machine; all
    content here is synthetic. store.db is a decoy that must never be opened."""
    root = tmp_path / "cursor"
    _install(FIXTURES / "cursor" / "transcript.jsonl",
             root / "projects" / "slug-a" / "agent-transcripts" / CURSOR_UUID
             / f"{CURSOR_UUID}.jsonl", workspace)
    chat = root / "chats" / "h1" / CURSOR_UUID
    _install(FIXTURES / "cursor" / "meta.json", chat / "meta.json", workspace)
    (chat / "store.db").write_bytes(b"not a database")
    return root

AGY_ID = "00000000-0000-4000-8000-0000000000a1"
def pb(number: int, value) -> bytes:
    """Minimal protobuf encoder: int -> varint, str/bytes -> length-delimited."""
    def varint(n: int) -> bytes:
        out = b""
        while True:
            byte, n = n & 0x7F, n >> 7
            out += bytes([byte | (0x80 if n else 0)])
            if not n:
                return out
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    data = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(data)) + data
def agy_step(step_type: int, body: bytes = b"", seconds: int = 1_786_000_000) -> tuple:
    """-> (step_type, metadata, step_payload) as stored in `steps`."""
    stamp = pb(1, pb(1, seconds) + pb(2, 250_000_000))
    return step_type, stamp, pb(1, step_type) + pb(4, 3) + body
def agy_call(call_id: str, name: str, args: dict) -> bytes:
    return pb(7, pb(1, call_id) + pb(2, name) + pb(3, json.dumps(args)))
def make_agy_db(path: Path, steps: list[tuple], cwd: Path | None, wal: bool = False) -> None:
    """A SQLite file with the real table layout and synthetic steps only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("pragma journal_mode=wal")
    conn.execute("create table trajectory_metadata_blob (id integer primary key, data blob)")
    conn.execute("create table steps (idx integer primary key, step_type integer, "
                 "status integer, metadata blob, step_payload blob, step_format integer)")
    if cwd is not None:
        conn.execute("insert into trajectory_metadata_blob (data) values (?)",
                     (pb(1, pb(1, cwd.as_uri())) + pb(7, cwd.as_uri()),))
    for idx, (step_type, metadata, payload) in enumerate(steps):
        conn.execute("insert into steps values (?, ?, 3, ?, ?, 0)",
                     (idx, step_type, metadata, payload))
    conn.commit()
    conn.close()
def agy_steps(workspace: Path) -> list[tuple]:
    readme = str(workspace / "README.md")
    return [
        agy_step(14, pb(19, pb(2, "add a readme"))),
        agy_step(15, pb(20, pb(8, "Creating it.") + pb(1, "Creating it.")
                        + agy_call("srv-a", "write_to_file", {"TargetFile": readme})
                        + agy_call("srv-b", "view_file", {"AbsolutePath": readme})),
                 seconds=1_786_000_010),
        agy_step(5, pb(5, pb(4, pb(1, "srv-a") + pb(2, "write_to_file")))),  # unmapped
        agy_step(8, pb(5, pb(4, pb(1, "srv-b") + pb(2, "view_file"))) + pb(14, pb(4, "# hi\n")),
                 seconds=1_786_000_020),
        agy_step(98, pb(111, b"")),  # unmapped
    ]
@pytest.fixture
def agy_root(tmp_path: Path, workspace: Path) -> Path:
    """Stands in for ~/.gemini/antigravity-cli. The layout was observed read-only on a
    real machine; every byte here is synthetic and built by the encoder above."""
    root = tmp_path / "agy"
    make_agy_db(root / "conversations" / f"{AGY_ID}.db", agy_steps(workspace), workspace)
    return root
