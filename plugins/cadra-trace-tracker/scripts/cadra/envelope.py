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
        if message.get("role") == "tool" and isinstance(content, str):
            encoded = content.encode("utf-8")
            if len(encoded) > TOOL_RESULT_CAP:
                # Slice bytes, not characters: TOOL_RESULT_CAP is a byte budget,
                # and one CJK character is three bytes, so a character slice
                # would overshoot the cap it exists to enforce.
                half = TOOL_RESULT_CAP // 2
                head = encoded[:half].decode("utf-8", errors="ignore")
                tail = encoded[-half:].decode("utf-8", errors="ignore")
                message["content"] = f"{head}\n{TRUNCATION_MARKER}\n{tail}"
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

    `prefix_hash` is the hash of every message in chunks 0..N-1, so a dropped,
    reordered or altered chunk breaks the chain for every chunk after it (§9.3).

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
