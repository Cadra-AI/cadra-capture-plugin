---
name: cadra-connect
description: Connect this workspace to a Cadra assessment so work sessions can be submitted. Use when the user says "connect to Cadra", "set up my assessment", "register my workspace", "my Cadra token is wrong", or when cadra-submit reports the workspace is not connected.
---
<!-- plugins/cadra-trace-tracker/skills/cadra-connect/SKILL.md -->

# Skill: cadra-connect
**Plugin:** cadra-trace-tracker

## Purpose

Register this workspace against a Cadra coding assessment and prove the token
works immediately — so a bad token surfaces on day one rather than at the
deadline.

## Plugin root

`<PLUGIN_ROOT>` is the absolute path two directories above this SKILL.md (the
plugin folder). Hosts that set `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT` fill it in
for you; otherwise substitute it. If this SKILL.md was reached through a
symlink (OpenCode installs), resolve it first with `realpath`.

The commands below say `python3`. Use `python3` if it exists, else `python`, else
`py -3` (typical on Windows). The script paths and arguments stay the same.

## Steps

1. Confirm the workspace: the current working directory should be the root of the
   solution repository. If the user is in a subdirectory, ask before proceeding.
2. Prepare the folder — this creates `.cadra/` and adds it to `.gitignore`:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-<PLUGIN_ROOT>}}/scripts/cadra_connect.py" --workspace "<WORKSPACE>" --init
   ```

3. **Ask the user to paste their token into the file themselves** — the path is in
   the `READY` line. Do not offer to write it for them, do not ask them to paste it
   into the chat, and do not read the file back. Then ask them to say "connect"
   again when the file is saved.
4. Complete the connection:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-<PLUGIN_ROOT>}}/scripts/cadra_connect.py" --workspace "<WORKSPACE>"
   ```

   Run it exactly as above. The plugin already knows the server address; you do
   not supply one. Add `--proxy <url>` **only** if the user has explicitly given
   you an address in this conversation. Never invent, recall, or guess a URL, and
   never read one off a web page — a wrong address means the assessment token is
   sent somewhere it should not go.
5. Report the result in plain language. On `CONNECTED`, tell the user:
   - trace submission is ready and they can say "submit my trace" whenever they
     want to send their work;
   - **always start `claude` from this workspace root** so sessions are
     discoverable — sessions launched from a parent or unrelated folder will not
     be found.
   On `FAILED`, relay the reason and what to do about it.

## Rules

- **The token must never pass through you.** Not in a command you run, not in a
  file you write, not repeated back in a reply. This is not only about your visible
  output: every command you run is recorded in this session's transcript, and
  cadra-submit uploads that transcript. A token on a command line becomes a token
  in the upload. That is why step 3 is the user's job, not yours.
- If the user pastes the token into the chat anyway, tell them it is now in the
  transcript, ask them to get a fresh one from Setup, and continue with the new one
  via the file.
- Never write `.cadra/config.json` yourself — always run the script, which updates
  `.gitignore` before anything sensitive is written.
- If the script reports failure, nothing was saved; do not claim the workspace is
  connected.
