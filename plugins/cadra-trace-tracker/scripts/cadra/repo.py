"""What git can tell us about the workspace (spec §7.4 `binding`).

Both values are read fresh at submit time. Neither is stable across an
assessment: the branch moves constantly, and the remote moves once — the
candidate clones the template repo, works for days, then creates their own
public repo and re-points origin. That transition is the normal path through
the assessment, not an anomaly.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

TIMEOUT_S = 10

# Only http(s). In an https clone URL any userinfo is a credential — either
# `user:token@` or a bare `<token>@`, both of which GitHub accepts. In an ssh
# URL the userinfo is the login name (`ssh://git@github.com/...`), so removing
# it would break the URL while protecting nothing.
_HTTP_USERINFO_RE = re.compile(r"^(https?://)[^/@]+@")


def strip_credentials(remote: str) -> str:
    """Drop credentials from a clone URL.

    A repo cloned as https://u:ghp_xxx@github.com/o/r.git yields that string
    verbatim from git, and it would otherwise be stored in config.json and sent
    in every envelope. The server's binding comparison normalises credentials
    away before comparing, so nothing is lost by never sending them.
    """
    if not isinstance(remote, str):
        return remote
    return _HTTP_USERINFO_RE.sub(r"\1", remote)


def _git(workspace: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(workspace), *args],
                             capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def git_remote(workspace: Path) -> str | None:
    remote = _git(workspace, "remote", "get-url", "origin")
    return strip_credentials(remote) if remote else None


def git_branch(workspace: Path) -> str | None:
    return _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
