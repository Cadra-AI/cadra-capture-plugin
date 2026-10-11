"""Find the transcripts belonging to this assessment (spec §6.1, §6.1.1).

Every restriction here exists because its absence was a real defect:
  - no content substring matching (uploaded unrelated personal sessions)
  - no rglob (swept subagent files up as anonymous sessions)
  - no ancestor walking (the old gate walked six levels up and one down)
  - no desktop storage roots (that is the Claude desktop app, not Claude Code)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_SCOPE_PROBE_LINES = 50
MAX_SCOPE_PROBE_BYTES = 256 * 1024

open_text = open  # indirection so tests can observe which files are opened


def encode_dir_name(path: Path) -> str:
    """Claude Code encodes a session's cwd into its storage folder name."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


_MSYS_RE = re.compile(r"^/([A-Za-z])(?:/|$)")
def _norm(path: Path) -> Path:
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
        candidate = _norm(Path(cwd))
        root = _norm(workspace)
    except (OSError, ValueError):
        return False
    return candidate == root or root in candidate.parents


def candidate_dirs(projects_root: Path, workspace: Path) -> list[Path]:
    """Sound superset: an in-scope origin cwd always encodes to this prefix.

    Forward match only — the directory name is the workspace encoding or
    extends it. Ancestor directories are deliberately NOT matched: a session
    started above the workspace is out of scope by §6.1, and reaching up to
    find one would open the candidate's home directory. `origin_cwd` +
    `in_scope` then filter this superset exactly."""
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
    #: agent-<id> -> its sidecar meta ({agentType, toolUseId, description}).
    #: Missing or unreadable sidecars are simply absent, never fabricated.
    subagent_meta: dict[str, dict] = field(default_factory=dict)
    #: Which agent wrote the transcript; also the chunk's `agent.name`. Last, so
    #: positional construction elsewhere is unaffected.
    host: str = "claude-code"


def _read_agent_meta(jsonl: Path) -> dict:
    """The `agent-<id>.meta.json` sitting beside a subagent transcript.

    It carries the parent `toolUseId` and the real `agentType`, which is what
    lets the server attribute subagent work (§7 `cadra_agent`)."""
    sidecar = jsonl.with_suffix(".meta.json")
    try:
        with open_text(sidecar, encoding="utf-8", errors="ignore") as handle:
            meta = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


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
            sessions.append(Session(
                session_id=session_id, main_file=main_file, origin_cwd=cwd,
                subagent_files=list(subagents),
                subagent_meta={f.stem: _read_agent_meta(f) for f in subagents},
            ))
    return sessions, notes
