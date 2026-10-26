"""What every host reader shares for finding sessions: the `Session` record, the
workspace-scope test, and a read-only SQLite opener. Host-specific discovery lives
in the host's own module (`claude.py`, `codex.py`, ...).
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Session:
    session_id: str
    main_file: Path
    origin_cwd: str
    subagent_files: list[Path] = field(default_factory=list)
    #: agent-<id> -> its sidecar meta ({agentType, toolUseId, description}).
    #: Missing or unreadable sidecars are simply absent, never fabricated.
    subagent_meta: dict[str, dict] = field(default_factory=dict)
    #: Which agent wrote the transcript; also the chunk's `agent.name`. Required,
    #: keyword-only: a reader that forgets it fails loudly instead of being
    #: silently filed as Claude Code.
    host: str = field(kw_only=True)


_MSYS_RE = re.compile(r"^/([A-Za-z])(?:/|$)")


def norm_path(path: Path) -> Path:
    text = str(path)
    if os.name == "nt":
        # Git Bash / MSYS record /c/Users/x; Windows would read that as C:\c\Users\x.
        match = _MSYS_RE.match(text.replace("\\", "/"))
        if match:
            text = f"{match.group(1)}:/" + text.replace("\\", "/")[3:]
    return Path(text).resolve()


def in_scope(cwd: str, workspace: Path) -> bool:
    """True iff cwd is the workspace or a descendant. Resolved-path comparison,
    never string prefix — otherwise C:/Dev/ws matches C:/Dev/ws-other."""
    try:
        candidate = norm_path(Path(cwd))
        root = norm_path(workspace)
    except (OSError, ValueError):
        return False
    return candidate == root or root in candidate.parents


def open_sqlite_ro(db: Path) -> sqlite3.Connection:
    """Read-only, never creating -wal/-shm beside an agent's db. `immutable` ignores
    the WAL, and a live (or never-checkpointed) agent keeps its newest rows there —
    a fresh OpenCode db is a bare header with every table in the WAL. So go immutable
    only when there is no WAL; otherwise plain read-only reuses the writer's sidecars."""
    wal = db.with_name(db.name + "-wal")
    mode = "mode=ro" if wal.is_file() and wal.stat().st_size else "mode=ro&immutable=1"
    return sqlite3.connect(f"{db.resolve().as_uri()}?{mode}", uri=True)
