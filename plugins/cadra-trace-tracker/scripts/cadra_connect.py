"""cadra-connect — register this workspace for trace submission (spec §5.1).

Exercises the token on day zero. A managed OpenCode candidate discovers a bad
token on their first prompt; a BYO candidate would otherwise discover it at
submission, after the work is done.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from cadra import client, config
from cadra.repo import git_remote, strip_credentials  # noqa: F401

TIMEOUT_S = 20


def verify_token(*, proxy_base_url: str, token: str,
                 workspace_root: str = "") -> tuple[bool, str]:
    """Prove the token and claim this folder in one call (§5.1).

    Binding here rather than at the first submission is deliberate. Under
    trust-on-first-use the first submission is accepted whatever folder it comes
    from, so a candidate whose first submission is from the wrong project binds
    to it and has their real work rejected afterwards — the failure lands late
    and backwards. Connect happens on day zero, while they are following the
    Setup instructions in the folder they just prepared.
    """
    status, payload = client.bind_workspace(
        base_url=proxy_base_url, token=token, workspace_root=workspace_root)
    if 200 <= status < 300:
        return True, ""
    error = payload.get("error") or {}
    if status == 401:
        return False, "the server did not accept this token (401)"
    if status == 409:
        expected = error.get("expected_workspace") or "another folder"
        return False, (f"this assessment is already registered to '{expected}'. "
                       "Connect from that folder, or ask the program team to "
                       "reset it if you have moved your work")
    if status == 0:
        return False, f"could not reach Cadra ({error.get('message', 'no reply')})"
    return False, f"the server returned HTTP {status}"


TOKEN_FILENAME = "token.txt"


def token_path(workspace: Path) -> Path:
    return config.config_dir(workspace) / TOKEN_FILENAME


def _stored_connection(workspace: Path) -> dict | None:
    """The saved connection, or None if this workspace was never connected.

    A config.json with no token is treated as not connected: it cannot submit,
    so reporting "already connected" would be wrong.
    """
    try:
        cfg = config.load(workspace)
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) and cfg.get("token") else None


def _claims_expiry(token: str) -> float | None:
    """`exp` from a stored token, or None if unreadable.

    Unverified, like every other local claim read — the server is what decides
    whether a token is good. This only needs to tell "still inside the window"
    from "lapsed since you connected", which is a numeric field, not a trust
    decision. None means "cannot tell", and the caller must not treat that as
    expired.
    """
    try:
        exp = config.decode_claims(token).get("exp")
    except ValueError:
        return None
    return float(exp) if isinstance(exp, (int, float)) else None


def main(argv: list[str] | None = None) -> int:
    # Host engines pipe stdout in the platform's default encoding; a non-ASCII
    # path must not crash the run.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--init", action="store_true",
                        help="prepare .cadra/ and print where to paste the token")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"FAILED: {workspace} is not a directory")
        return 1
    # A workspace that is the home directory or a drive root would make every
    # project folder beneath it "in scope" — the candidate's whole transcript
    # store, which §6.1.1 exists to keep out.
    if workspace == Path.home().resolve() or workspace == workspace.parent:
        print(f"FAILED: {workspace} is too broad to be a project. Connect from "
              "the folder holding your solution.")
        return 1

    if args.init:
        # Create the folder and its ignore entry BEFORE the candidate pastes
        # anything, so the token file cannot exist un-ignored even briefly.
        config.ensure_gitignored(workspace)
        config.config_dir(workspace).mkdir(parents=True, exist_ok=True)
        print(f"READY paste your assessment token into {token_path(workspace)} "
              "and save the file, then say 'connect' again.")
        return 0

    source = token_path(workspace)
    try:
        token = source.read_text(encoding="utf-8").strip()
    except OSError:
        # token.txt is DELETED on a successful connect, so its absence usually
        # means "already connected", not "lost your token". Saying the latter
        # sent candidates back to the Setup page to re-copy a credential they
        # had just used successfully — and the README had to warn that the
        # message "only means you are done". config.json distinguishes the
        # states locally, so answer from it.
        already = _stored_connection(workspace)
        if already is None:
            print(f"FAILED: no token file at {source}. Run with --init first, then "
                  "paste your token into that file yourself.")
            return 1
        expires = _claims_expiry(already.get("token", ""))
        if expires is not None and expires < time.time():
            when = time.strftime("%Y-%m-%d", time.gmtime(expires))
            print(f"FAILED: your token has expired (it ran out on {when}). Paste a "
                  f"fresh one into {source} and connect again.")
            return 1
        print(f"CONNECTED workspace={already.get('workspace_root') or workspace}")
        print("Already connected — nothing to do. Say 'submit my trace' when you "
              "want to send your work.")
        return 0
    if not token:
        print(f"FAILED: {source} is empty. Paste your assessment token into it.")
        return 1

    try:
        claims = config.decode_claims(token)
    except ValueError as exc:
        print(f"FAILED: {exc}. Re-copy the token from your Setup page.")
        return 1
    # Live assessments carry coding_assessment_id; offline capture tokens
    # (type f18-offline-capture) carry capture_id instead. The server decides
    # which one it accepts; this is only a fail-fast check for a wrong paste.
    if not (claims.get("coding_assessment_id") or claims.get("capture_id")):
        print("FAILED: this token carries no assessment. Re-copy it from Setup.")
        return 1
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        print("FAILED: this token has expired. Request a fresh one.")
        return 1

    # NOT from the token. `claims` is decoded without verifying the signature,
    # so a proxy_base_url claim would be unauthenticated input choosing where a
    # bearer credential is sent. The address is the same for every candidate in
    # an environment, so it is client config — see config.DEFAULT_PROXY_BASE_URL.
    proxy = config.resolve_proxy(args.proxy)
    # Same predicate client enforces, so connect and submit can never disagree
    # about what is reachable.
    if not client.is_allowed_base_url(proxy):
        print("FAILED: the proxy address must be an https:// URL "
              "(or http:// on localhost for local testing). Nothing was saved.")
        return 1

    ok, detail = verify_token(proxy_base_url=proxy, token=token,
                              workspace_root=str(workspace).replace("\\", "/"))
    if not ok:
        print(f"FAILED: {detail}. Nothing was saved.")
        return 1

    config.save(workspace, {
        "token": token,
        "assessment_label": args.label,
        "workspace_root": str(workspace).replace("\\", "/"),
        "git_remote": git_remote(workspace),
        "proxy_base_url": proxy.rstrip("/"),
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    # The paste file has done its job. Two plaintext copies of the token is one
    # more than necessary, and this one has no 0600 mode.
    try:
        source.unlink()
    except OSError:
        print(f"NOTE could not remove {source}; delete it yourself.")
    print(f"CONNECTED workspace={workspace}")
    print("Trace submission is ready. Always start `claude` from this workspace "
          "root so your sessions are discoverable. Say 'submit my trace' when "
          "you want to send your work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
