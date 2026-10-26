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

# The Cadra proxy address. This is CLIENT configuration, not candidate data:
# every candidate in an environment submits to the same proxy, so the value is
# invariant and belongs here rather than in anything the candidate handles.
#
# It is deliberately NOT read from a token claim. Claims are decoded without
# verifying the signature (see decode_claims), so a claim deciding where a
# bearer token gets sent would be unauthenticated input controlling a credential
# path — undoing the same-host guarantee client._NoRedirects exists to keep.
# Before this constant existed the resolution chain ended in "", and the only
# place the real hostname lived was prose in SKILL.md for the agent to recall.
DEFAULT_PROXY_BASE_URL = "https://proxy.cadra.info"
# Override for local and staging work, matching the <HOST>_HOME override idiom
# the host adapters already use. Operator-supplied, same trust level as --proxy.
PROXY_ENV_VAR = "CADRA_PROXY_BASE_URL"


def resolve_proxy(cli_value: str, stored: str = "") -> str:
    """The proxy base URL, the ONE chain every script uses: explicit flag, then
    environment, then the address pinned in `.cadra/config.json` (only present
    when connect was given one explicitly), then the built-in default.

    So moving the proxy is one change: a new default in a release moves every
    workspace that never pinned one, and `CADRA_PROXY_BASE_URL` moves all of them
    at once without reconnecting. Takes no token on purpose — see
    DEFAULT_PROXY_BASE_URL. Never returns empty.
    """
    for candidate in (cli_value, os.environ.get(PROXY_ENV_VAR, ""), stored):
        value = (candidate or "").strip()
        if value:
            return value.rstrip("/")
    return DEFAULT_PROXY_BASE_URL.rstrip("/")


def explicit_proxy(cli_value: str) -> str:
    """The address the operator gave (flag or environment), "" when neither."""
    for candidate in (cli_value, os.environ.get(PROXY_ENV_VAR, "")):
        if (candidate or "").strip():
            return candidate.strip().rstrip("/")
    return ""


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
