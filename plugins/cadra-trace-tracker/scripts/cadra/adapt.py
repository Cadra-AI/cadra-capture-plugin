"""What every host reader shares for the canonical OpenAI-style messages (spec §7):
workspace-relative path rebasing, the subagent tag, and the turn count shown to the
candidate. Each host's own format knowledge lives in its module (`claude.py`,
`codex.py`, ...), so the server stays agent-agnostic.
"""
from __future__ import annotations

import ntpath
import os
import re
from pathlib import Path, PureWindowsPath

PATH_ARG_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def rebase_path(value: str, workspace: Path) -> str:
    """Absolute paths under the workspace become relative; others stay absolute so
    the server's _GLOBAL_PATH_RE discards them (§7.1)."""
    if not isinstance(value, str) or not value:
        return value
    try:
        # A drive-letter workspace is compared with Windows rules (case-blind,
        # either separator) on every OS, so the result never depends on the host.
        if _DRIVE_RE.match(str(workspace)):
            return _rebase_windows(value, str(workspace))
        candidate = Path(value)
        if not candidate.is_absolute():
            return value.replace("\\", "/")
        # Collapse `..` lexically — `src/../a.py` must arrive as `a.py` or the
        # server cannot match it to a repo file. Lexical, not `resolve()`: this
        # runs on paths from a transcript, which may no longer exist.
        root = Path(os.path.normpath(str(workspace)))
        resolved = Path(os.path.normpath(str(candidate)))
        if resolved == root or root in resolved.parents:
            return str(resolved.relative_to(root)).replace("\\", "/")
    except (OSError, ValueError):
        return value
    return value.replace("\\", "/")


def _rebase_windows(value: str, workspace: str) -> str:
    candidate = PureWindowsPath(value)
    if not candidate.is_absolute():
        return value.replace("\\", "/")
    root = PureWindowsPath(ntpath.normpath(workspace))
    resolved = PureWindowsPath(ntpath.normpath(value))
    if resolved == root or root in resolved.parents:
        return resolved.relative_to(root).as_posix()
    return value.replace("\\", "/")


def agent_tag(meta: dict) -> dict:
    """`cadra_agent` for a subagent message (§7). Only fields actually present:
    an absent parent id is left out rather than guessed at."""
    tag: dict = {}
    if isinstance(meta.get("toolUseId"), str) and meta["toolUseId"]:
        tag["parent_tool_use_id"] = meta["toolUseId"]
    agent_type = meta.get("agentType")
    tag["agent_type"] = agent_type if isinstance(agent_type, str) and agent_type else "subagent"
    return tag


def count_turns(messages: list[dict]) -> int:
    """How many times the candidate spoke.

    The unit to SHOW a candidate. A raw message count mixes their prompts with
    the agent's own steps — assistant replies, tool calls, tool results — so a
    session they experienced as 8 exchanges reports as 89 messages, which reads
    like the plugin is capturing far more than it is. Turns are checkable
    against their memory of the session.

    Relies on injected content already being re-roled away from "user" (see
    claude.to_messages): otherwise skill text would count as turns the candidate
    never took. Not a substitute for the full count, which the sent-vs-confirmed
    comparison still needs.
    """
    return sum(1 for m in messages
               if isinstance(m, dict) and m.get("role") == "user")
