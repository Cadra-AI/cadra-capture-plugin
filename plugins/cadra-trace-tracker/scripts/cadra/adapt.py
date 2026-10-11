"""Claude Code JSONL -> canonical OpenAI-style messages (spec §7).

All Claude Code format knowledge lives here so the server stays agent-agnostic.
"""
from __future__ import annotations

import json
import ntpath
import os
import re
from pathlib import Path, PureWindowsPath

from cadra.collect import Session

PATH_ARG_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_LINENO_RE = re.compile(r"^\s*\d+\t")
IMAGE_PLACEHOLDER = "[image removed before upload]"


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


def agent_tag(meta: dict) -> dict:
    """`cadra_agent` for a subagent message (§7). Only fields actually present:
    an absent parent id is left out rather than guessed at."""
    tag: dict = {}
    if isinstance(meta.get("toolUseId"), str) and meta["toolUseId"]:
        tag["parent_tool_use_id"] = meta["toolUseId"]
    agent_type = meta.get("agentType")
    tag["agent_type"] = agent_type if isinstance(agent_type, str) and agent_type else "subagent"
    return tag


def to_messages(entries: list[dict], workspace: Path,
                agent: dict | None = None) -> list[dict]:
    """`agent` is the `cadra_agent` tag for subagent entries, None for the main
    transcript. The server's extractor ignores it (§7); it exists so a human
    reading the trace can tell which agent did what."""
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
                if agent:
                    out["cadra_agent"] = agent
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
                    tool_calls.append({
                        "id": block.get("id"), "type": "function",
                        "function": {"name": block.get("name"), "arguments": args},
                    })
            out = {"role": "assistant"}
            if texts:
                out["content"] = "\n".join(texts)
            if tool_calls:
                out["tool_calls"] = tool_calls
            if len(out) > 1:
                if isinstance(ts, str) and ts:
                    out["client_ts"] = ts
                if agent:
                    out["cadra_agent"] = agent
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
                if agent:
                    out["cadra_agent"] = agent
                messages.append(out)
    return messages


def _read_entries(path: Path) -> tuple[list[dict], int]:
    """-> (entries, unreadable line count).

    A malformed line is skipped, never fatal (§10) — but it is counted and
    surfaced, because a transcript that is quietly half-read looks exactly like
    a candidate who did less work."""
    entries: list[dict] = []
    bad = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
                else:
                    bad += 1
    except OSError:
        return entries, bad + 1
    return entries, bad


def load_session(session: Session, workspace: Path) -> tuple[list[dict], dict]:
    """Main transcript plus subagents, interleaved by timestamp (§6.2)."""
    main, unreadable = _read_entries(session.main_file)
    tagged: list[tuple[str, dict, dict | None]] = [
        (str(e.get("timestamp") or ""), e, None) for e in main
    ]
    for sub_file in session.subagent_files:
        tag = agent_tag(session.subagent_meta.get(sub_file.stem) or {})
        sub_entries, sub_bad = _read_entries(sub_file)
        unreadable += sub_bad
        for entry in sub_entries:
            tagged.append((str(entry.get("timestamp") or ""), entry, tag))
    tagged.sort(key=lambda item: item[0])

    messages: list[dict] = []
    cwds: list[str] = []
    version = ""
    for _ts, entry, tag in tagged:
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd and cwd not in cwds:
            cwds.append(cwd)
        if not version and isinstance(entry.get("version"), str):
            version = entry["version"]
        messages.extend(to_messages([entry], workspace, agent=tag))

    stamps = [t for t, _e, _tag in tagged if t]
    meta = {"cwds": cwds, "started_at": stamps[0] if stamps else None,
            "ended_at": stamps[-1] if stamps else None, "agent_version": version,
            "unreadable_lines": unreadable}
    return messages, meta
