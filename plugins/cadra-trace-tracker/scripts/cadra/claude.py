"""Claude Code transcripts -> canonical messages (same shape as every other host).
Store (read-only): `$CLAUDE_CONFIG_DIR` or `~/.claude`, then `projects/<encoded cwd>/
<session>.jsonl` with subagents in `<session>/subagents/agent-<id>.jsonl` (+ a
`.meta.json` sidecar naming the agent type and the parent tool use).

Discovery (spec §6.1, §6.1.1). Every restriction here exists because its absence
was a real defect:
  - no content substring matching (uploaded unrelated personal sessions)
  - no rglob (swept subagent files up as anonymous sessions)
  - no ancestor walking (the old gate walked six levels up and one down)
  - no desktop storage roots (that is the Claude desktop app, not Claude Code)

Conversion (spec §7): only `user` / `assistant` records are conversation. Claude
Code also writes `api-request-blob` (a mirror of each request body, with a
`message` dict and no timestamp), `system`, `attachment`, `queue-operation`,
`file-history-*` and more; all of those are dropped.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from cadra import adapt, collect

HOST = "claude-code"
MAX_SCOPE_PROBE_LINES = 50
MAX_SCOPE_PROBE_BYTES = 256 * 1024
IMAGE_PLACEHOLDER = "[image removed before upload]"
_LINENO_RE = re.compile(r"^\s*\d+\t")

open_text = open  # indirection so tests can observe which files are opened


def default_root() -> Path:
    home = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(home) if home else Path.home() / ".claude") / "projects"


# ── discovery ────────────────────────────────────────────────────────────────

def encode_dir_name(path: Path) -> str:
    """Claude Code encodes a session's cwd into its storage folder name."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def candidate_dirs(projects_root: Path, workspace: Path) -> list[Path]:
    """Sound superset: an in-scope origin cwd always encodes to this prefix.

    Forward match only — the directory name is the workspace encoding or
    extends it. Ancestor directories are deliberately NOT matched: a session
    started above the workspace is out of scope by §6.1, and reaching up to
    find one would open the candidate's home directory. `origin_cwd` +
    `in_scope` then filter this superset exactly."""
    prefix = encode_dir_name(collect.norm_path(workspace))
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


def discover(projects_root: Path, workspace: Path) -> tuple[list[collect.Session], list[str]]:
    """-> (in-scope sessions, notes about what was excluded and why)."""
    sessions: list[collect.Session] = []
    notes: list[str] = []
    for directory in candidate_dirs(projects_root, workspace):
        for main_file in sorted(directory.glob("*.jsonl")):
            # `<session>.orphaned-<ts>-<suffix>.jsonl` is a transcript Claude Code set
            # aside; its content is already in (or superseded by) the live session.
            if ".orphaned-" in main_file.name:
                continue
            cwd = origin_cwd(main_file)
            if cwd is None:
                notes.append(f"{main_file.name}: no working directory recorded — skipped")
                continue
            if not collect.in_scope(cwd, workspace):
                notes.append(
                    f"{main_file.stem[:8]}: started elsewhere ({cwd}) — not included"
                )
                continue
            session_id = main_file.stem
            subagents = sorted(
                (directory / session_id / "subagents").glob("agent-*.jsonl")
            )
            sessions.append(collect.Session(
                session_id=session_id, main_file=main_file, origin_cwd=cwd,
                subagent_files=list(subagents),
                subagent_meta={f.stem: _read_agent_meta(f) for f in subagents},
                host=HOST,
            ))
    return sessions, notes


# ── conversion ───────────────────────────────────────────────────────────────

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
                agent: dict | None = None) -> list[dict]:
    """`agent` is the `cadra_agent` tag for subagent entries, None for the main
    transcript. The server's extractor ignores it (§7); it exists so a human
    reading the trace can tell which agent did what."""
    messages: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # Only conversation records. `api-request-blob` (Claude Code >= 2.1.285)
        # also carries a `message` dict — a mirror of the request body — and
        # without this gate every tool call was sent twice, timestamp-less, and
        # sorted ahead of the first real prompt.
        if entry.get("type") not in ("user", "assistant"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        ts = entry.get("timestamp")
        role = message.get("role") or entry.get("type")
        content = message.get("content")
        # Claude Code injects skill text as a role=user entry with isMeta=True
        # and a sourceToolUseID. It is the AGENT's own context, not something the
        # candidate typed, and recording it as role=user makes the grader score
        # it as candidate prose: a multi-thousand-character "prompt" arriving
        # instantly is exactly what paste_velocity_evidence flags. Re-roled to
        # "system", which the managed path already uses for its injected bulk
        # and which _user_rows_from_trace ignores, so the content survives for
        # context without being attributed to anyone.
        injected = bool(entry.get("isMeta")) and role == "user"

        if isinstance(content, str):
            if role in ("user", "assistant") and content:
                out = {"role": "system" if injected else role, "content": content}
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
                    for key in adapt.PATH_ARG_KEYS:
                        if isinstance(args.get(key), str):
                            args[key] = adapt.rebase_path(args[key], workspace)
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
                    # tool_result above keeps role="tool" regardless: re-roling
                    # it would drop the tool_call_id linkage. Only prose blocks,
                    # which are the ones authorship analysis reads, are affected.
                    out = {"role": "system" if injected else "user",
                           "content": block["text"]}
                elif block.get("type") == "image":
                    out = {"role": "system" if injected else "user",
                           "content": IMAGE_PLACEHOLDER}
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
        with open_text(path, encoding="utf-8", errors="ignore") as handle:
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


def load_session(session: collect.Session, workspace: Path) -> tuple[list[dict], dict]:
    """Main transcript plus subagents, interleaved by timestamp (§6.2)."""
    main, unreadable = _read_entries(session.main_file)
    tagged: list[tuple[str, dict, dict | None]] = [
        (str(e.get("timestamp") or ""), e, None) for e in main
    ]
    for sub_file in session.subagent_files:
        tag = adapt.agent_tag(session.subagent_meta.get(sub_file.stem) or {})
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
