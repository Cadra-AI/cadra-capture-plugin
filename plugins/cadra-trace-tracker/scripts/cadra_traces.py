"""cadra-traces — read-only view of what the server holds (spec §5.3).

Never uploads, never writes, and never falls back to a local guess: if the server
cannot be reached it says so, because a local file cannot prove what Cadra stored.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cadra import client, config


def main(argv: list[str] | None = None) -> int:
    # Host engines pipe stdout in the platform's default encoding; a non-ASCII
    # trace title must not crash the run.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args(argv)

    workspace = config.find_workspace(Path(args.workspace))
    if workspace is None:
        print("NOT CONNECTED: run cadra-connect first.")
        return 1
    cfg = config.load(workspace)
    status, payload = client.get_traces(base_url=cfg["proxy_base_url"],
                                        token=cfg["token"])
    if status != 200:
        error = payload.get("error") or {}
        print(f"UNAVAILABLE: could not reach Cadra "
              f"({error.get('code', f'http_{status}')}).")
        return 1
    sessions = payload.get("sessions") or []
    if not sessions:
        print("No sessions are stored yet. Say 'submit my trace' to send your work.")
        return 0
    print(f"STORED SESSIONS ({len(sessions)}):")
    for row in sessions:
        print(f"  {str(row.get('submitted_at', ''))[:16]}  "
              f"{row.get('message_count', '?')} messages  "
              f"{int(row.get('bytes', 0)) // 1024} KB  "
              f"id={str(row.get('session_id', ''))[:8]}  "
              f"binding={row.get('binding_status', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
