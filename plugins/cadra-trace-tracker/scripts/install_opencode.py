#!/usr/bin/env python3
"""Install the three Cadra skills and /cadra-* slash commands into OpenCode.

    python3 install_opencode.py              # global:  ~/.config/opencode
    python3 install_opencode.py --project .  # project: ./.opencode
    python3 install_opencode.py --uninstall  # remove only what this script wrote
    python3 install_opencode.py --dry-run    # print the plan, write nothing

OpenCode does not follow symlinks and a copied skill is no longer two folders under
the plugin, so the absolute plugin root is written into the copied SKILL.md files in
place of the <PLUGIN_ROOT> token. No environment variable or shell profile is touched.
Re-running (for example after `git pull`) refreshes the copies.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SKILLS = ("cadra-connect", "cadra-submit", "cadra-traces")
TOKEN = "<PLUGIN_ROOT>"
COMMANDS = {
    "cadra-connect": (
        "Connect this workspace to Cadra",
        "Load the `cadra-connect` skill and follow it to connect this workspace to Cadra.",
    ),
    "cadra-submit": (
        "Submit this workspace's work sessions to Cadra",
        "Load the `cadra-submit` skill and follow it: run the dry run first, show me "
        "the preview, and send only after I confirm.",
    ),
    "cadra-traces": (
        "List what the Cadra server holds for this workspace",
        "Load the `cadra-traces` skill and follow it to list what the server holds.",
    ),
}


def config_dir(args) -> Path:
    if args.target:
        return Path(args.target).expanduser().resolve()
    if args.project:
        return Path(args.project).expanduser().resolve() / ".opencode"
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".config" / "opencode"


def root_text() -> str:
    # Forward slashes work inside quotes in cmd, PowerShell and python on Windows.
    return PLUGIN_ROOT.as_posix() if os.name == "nt" else str(PLUGIN_ROOT)


def inside(base: Path, path: Path) -> Path:
    if base.resolve() not in path.resolve().parents:
        sys.exit(f"refusing to touch {path}: outside {base}")
    return path


def command_text(name: str) -> str:
    description, body = COMMANDS[name]
    return f"---\ndescription: {description}\n---\n{body} $ARGUMENTS\n"


def verify_hint() -> str:
    if os.name == "nt":
        return ("opencode debug skill > $env:TEMP\\skills.json; "
                "Select-String cadra $env:TEMP\\skills.json")
    return "opencode debug skill > /tmp/skills.json; grep cadra /tmp/skills.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", metavar="DIR", help="install into DIR/.opencode")
    ap.add_argument("--target", metavar="DIR", help="override the OpenCode config dir")
    ap.add_argument("--dry-run", action="store_true", help="print actions, write nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove what this installed")
    args = ap.parse_args(argv)

    base = config_dir(args)
    skills_dir, cmds_dir = base / "skills", base / "commands"
    say = (lambda msg: print(("[dry-run] " if args.dry_run else "") + msg))

    for name in SKILLS:
        dest = inside(base, skills_dir / name)
        cmd = inside(base, cmds_dir / f"{name}.md")
        if args.uninstall:
            say(f"remove {dest}")
            say(f"remove {cmd}")
            if not args.dry_run:
                shutil.rmtree(dest, ignore_errors=True)
                cmd.unlink(missing_ok=True)
            continue
        src = PLUGIN_ROOT / "skills" / name
        if not (src / "SKILL.md").is_file():
            sys.exit(f"missing {src / 'SKILL.md'}")
        say(f"copy skill   {src} -> {dest}")
        say(f"write command {cmd}")
        if args.dry_run:
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, symlinks=False)
        md = dest / "SKILL.md"
        md.write_bytes(md.read_bytes().replace(TOKEN.encode(), root_text().encode()))
        cmds_dir.mkdir(parents=True, exist_ok=True)
        cmd.write_bytes(command_text(name).encode("utf-8"))

    if args.uninstall:
        say("Done. Restart opencode.")
        return 0
    say(f"Plugin root baked into skills: {root_text()}")
    say("Next: restart opencode, then type /cadra-connect")
    say(f"Verify (piping straight to grep truncates output, so use a file): {verify_hint()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
