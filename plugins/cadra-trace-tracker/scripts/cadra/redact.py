"""Secret redaction (spec §8). VENDORED COPY.

Canonical source is cadra-prototype/proxy/redact.py. Edit order on any rules
change: proxy -> backend -> plugin, then bump RULES_VERSION in all three.
Do not add rules here first.

Only the sentinel-delimited region below is shared; the release check hashes
exactly those bytes across the three copies (§8.0). `redact_messages` sits
outside it because it is plugin-only — the server has no use for the write
paths it returns.
"""
from __future__ import annotations

import math
import re
from typing import Any

# --- BEGIN SHARED REDACTION RULES (see byo-trace-capture-design §8.0) ---
RULES_VERSION = "1"

_TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)"
    r"[A-Z0-9_]*)(\s*[=:]\s*)(\S+)"
)

# Known-safe shapes that must never be redacted: redacting real code content
# breaks attestation line matching and penalises the candidate for our filter.
_SAFE_SHAPES = [
    re.compile(r"^[0-9a-f]{7,40}$"),                                   # git sha
    re.compile(r"^[0-9a-fA-F-]{36}$"),                                 # uuid
    re.compile(r"^[A-Za-z]:[\\/]"),                                    # windows path
]
_ENTROPY_CANDIDATE_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{32,}\b")
_ENTROPY_MIN_BITS = 4.0

SECRET_FILE_MARKER = "[redacted:secret-file]"

# Files that are secrets by identity rather than by content shape. Everything
# else here is a pattern matcher, and a pattern matcher only catches what it
# recognises: in a realistic .env, `SMTP_PASS=` slips past the assignment rule
# (its keyword list has `password`, not `pass`) and `postgres://app:pw@host`
# slips past it too, because the secret is inside a URL rather than after the
# `=`. For these files, guessing is the wrong instrument — drop the body whole.
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
_SECRET_STEMS = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
# Templates carry placeholder values and no secrets. Dropping them is pure loss:
# reading .env.example is exactly the kind of orientation work worth crediting.
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")


def is_secret_file(path: str) -> bool:
    """True for a path whose *contents* must never be uploaded, whatever they say."""
    if not isinstance(path, str) or not path:
        return False
    name = re.split(r"[\\/]", path.strip().strip("\"'"))[-1].lower()
    if not name:
        return False
    if name.endswith(_SECRET_SUFFIXES):
        return True
    if any(name.startswith(stem) for stem in _SECRET_STEMS):
        return True
    if name == ".env":
        return True
    if name.startswith(".env"):
        return not name.endswith(_ENV_TEMPLATE_SUFFIXES)
    return False


# A shell command reaches the same files without a path argument — `cat .env` is
# at least as common as a Read. Matched on a filename token in the command text,
# which is coarse, but the asymmetry justifies it: a false positive costs one
# tool result, a false negative costs a live credential.
_SECRET_TOKEN_RE = re.compile(r"[^\s\"'=|;&<>()]+")


def command_touches_secret_file(command: str) -> bool:
    if not isinstance(command, str) or not command:
        return False
    return any(is_secret_file(token) for token in _SECRET_TOKEN_RE.findall(command))


def _shannon_bits(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_safe_shape(token: str) -> bool:
    return any(p.match(token) for p in _SAFE_SHAPES)


def redact_text(text: str) -> tuple[str, int]:
    """Returns (redacted, count). Never raises."""
    if not isinstance(text, str) or not text:
        return text, 0
    count = 0
    out = text
    for pattern in _TOKEN_PATTERNS:
        out, n = pattern.subn("[redacted:token]", out)
        count += n

    def _assign(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{match.group(2)}[redacted:secret]"

    out = _ASSIGNMENT_RE.sub(_assign, out)

    def _entropy(match: re.Match[str]) -> str:
        nonlocal count
        token = match.group(0)
        if _is_safe_shape(token) or _shannon_bits(token) < _ENTROPY_MIN_BITS:
            return token
        count += 1
        return "[redacted:entropy]"

    out = _ENTROPY_CANDIDATE_RE.sub(_entropy, out)
    return out, count
# --- END SHARED REDACTION RULES ---


WRITE_CONTENT_KEYS = ("content", "new_string", "file_text", "code_edit", "newString")
PATH_ARG_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path")


def redact_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str], int]:
    """Redact in place. Returns (messages, redacted_write_paths, count).

    EVERY string argument is scanned, not only write content. `Bash.command` is
    the one that matters most: a candidate who runs `export API_KEY=…`, curls with
    a bearer header, or connects to a database with a password in the URL has that
    secret recorded verbatim in the transcript as a tool argument.

    Only a change to *write content* adds to `redacted_paths`. Those are the lines
    attestation hashes against the repo, so a rewritten one must be reported and
    marked truncated — the file leaves the coverage denominator instead of scoring
    as unattested (§8.1). Rewriting a command string changes nothing that is
    hashed, so it must not truncate the file it happens to name.
    """
    total = 0
    redacted_paths: set[str] = set()
    # tool_call_id -> the secret file that call touched. Messages arrive in order,
    # so a call is always seen before the result it produced.
    secret_results: dict[str, str] = {}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            call_id = message.get("tool_call_id")
            if message.get("role") == "tool" and call_id in secret_results:
                # Whole body, unexamined. The file is a secret by identity, so
                # there is nothing to be gained by reading it more closely.
                message["content"] = f"{SECRET_FILE_MARKER} {secret_results[call_id]}"
                total += 1
                continue
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

            secret_target = path if is_secret_file(path or "") else None
            if secret_target is None:
                command = args.get("command")
                if isinstance(command, str) and command_touches_secret_file(command):
                    secret_target = "a credentials file named in the command"
            if secret_target is not None:
                call_id = call.get("id")
                if isinstance(call_id, str):
                    secret_results[call_id] = secret_target
                # Writing a secrets file puts its contents in the transcript just
                # as surely as reading one. Dropping the body changes the lines
                # attestation hashes, so the path is reported as truncated (§8.1)
                # — that costs the file its coverage, which is the right trade.
                for key in WRITE_CONTENT_KEYS:
                    if isinstance(args.get(key), str) and args[key]:
                        args[key] = SECRET_FILE_MARKER
                        total += 1
                        if isinstance(path, str):
                            redacted_paths.add(path)

            touched = 0
            for key, value in list(args.items()):
                if not isinstance(value, str):
                    continue
                if key in PATH_ARG_KEYS:
                    continue  # a path is not a secret, and rewriting it breaks scoping
                args[key], n = redact_text(value)
                if key in WRITE_CONTENT_KEYS:
                    touched += n
                total += n
            for edit in args.get("edits") or []:
                if not isinstance(edit, dict):
                    continue
                if secret_target is not None:
                    for key in WRITE_CONTENT_KEYS:
                        if isinstance(edit.get(key), str) and edit[key]:
                            edit[key] = SECRET_FILE_MARKER
                            total += 1
                            touched += 1
                            if isinstance(path, str):
                                redacted_paths.add(path)
                for key, value in list(edit.items()):
                    if not isinstance(value, str) or key in PATH_ARG_KEYS:
                        continue
                    edit[key], n = redact_text(value)
                    if key in WRITE_CONTENT_KEYS:
                        touched += n
                    total += n
            if touched and isinstance(path, str):
                redacted_paths.add(path)
    return messages, redacted_paths, total
