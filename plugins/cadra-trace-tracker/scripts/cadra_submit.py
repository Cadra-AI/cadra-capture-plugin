"""cadra-submit — collect, transform, redact and send this workspace's sessions.

Pipeline order is load-bearing (§7, §8, §9): transform, then redact, then apply
size controls, and only then hash and chunk. Hashing before redaction would make
the fingerprint describe text that is never sent, so a rules change would look
like "nothing new". Both sets of touched paths — the ones redaction rewrote and
the ones the size caps truncated — are merged into one `truncated_paths` list, so
the server drops those files from the attestation denominator instead of scoring
them as unattested (§8.1).

The server is the authority on success: nothing is written to `state.json` for a
session until every one of its chunks has been accepted.

`--dry-run` runs the whole pipeline and writes the preview but sends nothing.
That is what makes the preview *pre*-send (§6.1): a session may leave the
workspace after starting in it, and the candidate has to be able to see where it
went and decline before anything leaves the machine.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cadra import adapt, client, collect, config, envelope, redact, repo

CAPTURE_VERSION = "3.0.0"


def _projects_root(override: str | None) -> Path:
    return Path(override) if override else Path.home() / ".claude" / "projects"


def _state_path(workspace: Path) -> Path:
    return config.config_dir(workspace) / "state.json"


def _load_state(workspace: Path) -> dict:
    try:
        with open(_state_path(workspace), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(workspace: Path, state: dict) -> None:
    config.config_dir(workspace).mkdir(parents=True, exist_ok=True)
    with open(_state_path(workspace), "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def current_remote(workspace: Path) -> str | None:
    """The remote as it is now — never the value frozen in config.json (§5.1).

    It legitimately moves during an assessment: the candidate clones the
    template repo, works for days, then creates their own repo and re-points
    origin. Falling back to the connect-time value would describe a repo they
    have since left, exactly when the evidence matters most. If git cannot
    answer, send null; the server records absence, not a stale claim.
    """
    return repo.git_remote(workspace)


def _previewed_cwds(start: Path) -> dict[str, list[str]]:
    """The cwds each session showed in the last preview, by session id.

    The dry run and the real send are separate invocations, and the transcript
    grows between them — the consent conversation itself is appended to it. So
    the send can legitimately carry directories the candidate never saw. Those
    are the ones worth naming."""
    workspace = config.find_workspace(start)
    if workspace is None:
        return {}
    try:
        with open(config.config_dir(workspace) / "last-preview.json",
                  encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    for entry in (previous or {}).get("sessions") or []:
        if isinstance(entry, dict) and isinstance(entry.get("session_id"), str):
            out[entry["session_id"]] = list(entry.get("cwds") or [])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--projects-root", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="build and preview the payload; send nothing")
    args = parser.parse_args(argv)

    previewed_cwds = _previewed_cwds(Path(args.workspace))
    workspace = config.find_workspace(Path(args.workspace))
    if workspace is None:
        print("NOT CONNECTED: this workspace has no Cadra configuration. "
              "Run cadra-connect first.")
        return 1
    cfg = config.load(workspace)
    state = _load_state(workspace)
    branch = repo.git_branch(workspace)
    remote = current_remote(workspace)

    sessions, notes = collect.discover(_projects_root(args.projects_root), workspace)
    for note in notes:
        print(f"NOTE {note}")
    # Stated even when zero: discovery only sees sessions started in this
    # workspace or below it, so an empty result is information, not silence.
    print(f"FOUND {len(sessions)} session(s) started in this workspace.")
    if not sessions:
        print("Nothing to submit — no sessions were found for this workspace.")
        return 0

    preview: dict = {"sessions": []}
    failures = 0
    submitted = 0

    for session in sessions:
        messages, meta = adapt.load_session(session, workspace)
        messages, redacted_paths, redacted_count = redact.redact_messages(messages)
        messages, truncated_paths = envelope.apply_size_controls(messages)
        truncated = sorted(truncated_paths | redacted_paths)
        fingerprint = envelope.chunk_hash(messages)
        if state.get(session.session_id) == fingerprint:
            print(f"SKIP {session.session_id[:8]} — already submitted, unchanged")
            # Still recorded: the preview claims to be what was last sent, and a
            # run where everything skips would otherwise blank the file that the
            # candidate is told to inspect.
            preview["sessions"].append({
                "session_id": session.session_id, "message_count": len(messages),
                "status": "unchanged since the last submission",
                "cwds": meta["cwds"], "redacted": redacted_count,
                "truncated_paths": truncated, "messages": messages,
            })
            continue

        chunks = envelope.build_chunks(messages)
        preview["sessions"].append({
            "session_id": session.session_id, "message_count": len(messages),
            "chunks": len(chunks), "cwds": meta["cwds"],
            "redacted": redacted_count, "truncated_paths": truncated,
            # The messages exactly as sent, so the candidate can inspect what left
            # the machine (§4: "exactly what the last submit sent").
            "messages": messages,
        })
        for cwd in meta["cwds"]:
            print(f"CWD {session.session_id[:8]} — {cwd}")
        if not args.dry_run and session.session_id in previewed_cwds:
            fresh = [c for c in meta["cwds"]
                     if c not in previewed_cwds[session.session_id]]
            for cwd in fresh:
                print(f"NEW-CWD {session.session_id[:8]} — {cwd} "
                      "(entered after the preview you approved)")
        if meta["unreadable_lines"]:
            # Never fatal, never silent (§10): a partially readable transcript
            # otherwise looks identical to a candidate who did less work.
            print(f"NOTE {session.session_id[:8]}: {meta['unreadable_lines']} "
                  "unreadable line(s) skipped")

        if args.dry_run:
            # Byte size is part of the consent surface (§5.2): candidates need
            # to know how large the upload is before agreeing.
            approx_bytes = sum(
                len(json.dumps(chunk_messages, ensure_ascii=False).encode("utf-8"))
                for _, chunk_messages in chunks
            )
            print(f"DRY-RUN {session.session_id[:8]} — {len(messages)} messages, "
                  f"{len(chunks)} chunk(s), {approx_bytes} bytes, "
                  f"{redacted_count} redaction(s). Nothing was sent.")
            continue

        receipt: dict = {}
        confirmed = 0
        ok = True
        for chunk_meta, chunk_messages in chunks:
            body = {
                "capture_version": CAPTURE_VERSION,
                "agent": {"name": "claude-code", "version": meta["agent_version"]},
                # Shape is §7.4 verbatim. The companion ingest spec defers to it
                # as the canonical envelope, so a flat variant here would be a
                # silent contract break with a server that is not written yet.
                "session": {"session_id": session.session_id,
                            "started_at": meta["started_at"],
                            "ended_at": meta["ended_at"],
                            "cwds": meta["cwds"]},
                "binding": {"workspace_root": str(workspace).replace("\\", "/"),
                            "git_remote": remote,
                            "git_branch": branch},
                "chunk": chunk_meta,
                "redaction": {"rules_version": redact.RULES_VERSION,
                              "redacted_count": redacted_count},
                "truncated_paths": truncated,
                "messages": chunk_messages,
            }
            status, payload = client.post_chunk(
                base_url=cfg["proxy_base_url"], token=cfg["token"], body=body)
            # A 2xx carrying `accepted: false` is a refusal wearing a success
            # code. §10: never report success the server did not state.
            if status in (200, 202) and payload.get("accepted") is False:
                status = 0
                payload = {"error": {"code": "not_accepted",
                                     "message": "the server did not accept the chunk"}}
            if status not in (200, 202):
                error = payload.get("error") or {}
                print(f"FAILED {session.session_id[:8]} — "
                      f"{error.get('code', 'unknown')}: {error.get('message', '')}")
                if status == 401:
                    print("HINT: the server did not accept the token. Run "
                          "cadra-connect again with a fresh token from Setup.")
                elif status == 0:
                    print("HINT: the server was not reached. Nothing was lost — "
                          "this session is retried whole on the next run.")
                ok = False
                failures += 1
                # Abandon the session mid-chain: `state` is deliberately not
                # touched, so the next run resubmits it from chunk 0 (§10).
                break
            receipt = payload
            # Sum across chunks: `receipt` alone is the LAST chunk's, and the
            # largest real sessions need two (§9.3), so quoting it would
            # under-report exactly where a candidate is most likely to check.
            count = payload.get("messages")
            if isinstance(count, int):
                confirmed += count
        if ok:
            state[session.session_id] = fingerprint
            submitted += 1
            print(f"SUBMITTED {session.session_id[:8]} — server confirmed "
                  f"{confirmed} of {len(messages)} messages at "
                  f"{receipt.get('received_at', '?')}")
            if confirmed != len(messages):
                print(f"NOTE {session.session_id[:8]}: the server counted "
                      f"{confirmed} messages, this machine sent {len(messages)}. "
                      "Report this if it persists.")

    config.config_dir(workspace).mkdir(parents=True, exist_ok=True)
    with open(config.config_dir(workspace) / "last-preview.json", "w",
              encoding="utf-8") as handle:
        json.dump(preview, handle, indent=2)

    if args.dry_run:
        # No send happened, so nothing may be marked submitted.
        print("DRY-RUN complete — nothing was sent. Review "
              ".cadra/last-preview.json, then submit for real to send it.")
        return 0

    _save_state(workspace, state)

    if submitted == 0 and failures == 0:
        print("Everything is already submitted — no new work since the last submission.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
