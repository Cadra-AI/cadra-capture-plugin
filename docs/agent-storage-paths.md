# Internal notes: agent storage paths, platform status, troubleshooting

Not for the public README. Moved here so the public repo stays install-and-use only.

## Platform support

Where each agent keeps the sessions this plugin reads, and how far each cell has
been checked. **Windows and Linux have not been run at all**: the paths are audited against
published docs and the path logic is unit-tested with Windows-style paths
(`PureWindowsPath`), nothing more. Treat those cells as "expected from docs".

Legend: **run** = executed on that OS here; **docs** = path confirmed by the
agent's published docs, plugin not run there; **untested** = inferred only.

| Agent | macOS | Linux | Windows | Store (env override) |
|---|---|---|---|---|
| Claude Code | run | docs | docs | `~/.claude/projects` (`CLAUDE_CONFIG_DIR`); `%USERPROFILE%\.claude\projects` on Windows |
| Codex CLI | run | docs | docs | `~/.codex/sessions` (`CODEX_HOME`); `%USERPROFILE%\.codex\sessions` on Windows |
| Kiro | run | docs | docs | `~/.kiro/sessions` (`KIRO_HOME`); v3 `cli/` layout verified on macOS, Windows `%USERPROFILE%\.kiro\sessions\cli\` is inferred from `~` |
| Copilot CLI | run | docs | docs | `~/.copilot/session-state` (`COPILOT_HOME`); `%USERPROFILE%\.copilot\session-state` on Windows |
| Cursor CLI | run | docs | docs | `~/.cursor` (`CURSOR_HOME`); `%USERPROFILE%\.cursor` on Windows |
| Antigravity (`agy`) | run | docs | docs | `~/.gemini/antigravity-cli` via `HOME` / `USERPROFILE` on Windows (`ANTIGRAVITY_HOME` is this plugin's own override); docs are third-party (SpecStory, txcript) |
| OpenCode | run | docs | docs | `~/.local/share/opencode` (`OPENCODE_HOME`, `XDG_DATA_HOME`); `%USERPROFILE%\.local\share\opencode` on Windows (fallback `%LOCALAPPDATA%\opencode` only if the default has no db); `OPENCODE_DB` picks the db file |

Notes:

- The Linux column is "docs" because the test suite is plain Python and the paths
  follow the home directory; the suite was not run on Linux here.
- OpenCode on Windows: the official docs give `%USERPROFILE%\.local\share\opencode`;
  one third-party report names `%LOCALAPPDATA%\opencode`, used only when the default
  has no db. `OPENCODE_DB` (absolute path, or a name inside the data folder) overrides
  the db file; non-stable channels name it `opencode-<channel>.db`, and all of those
  are read. Desktop app UI state (`%APPDATA%\ai.opencode.desktop\*.dat`) is not
  session data and is ignored. Windows and Linux paths here are from docs, not run.
- Kiro keeps older (v1/v2) chats in `data.sqlite3` (macOS `~/Library/Application
  Support/kiro-cli/`, Linux `~/.local/share/kiro-cli/`, Windows `%APPDATA%\kiro-cli\`).
  Its stored format could not be verified, so it is not read; the submit prints one
  note if that database holds conversations. v3 does not migrate those sessions.
- Antigravity's chat format is reverse-engineered, and its root (resolved from
  `HOME`, or `USERPROFILE` on Windows, with no vendor override) comes from
  third-party docs only. It also writes `brain/<id>/` artifacts and a JSONL
  transcript; only `conversations/<id>.db` is read.
- Paths a Windows agent records (`C:\Users\x\proj`, `c:/users/x/proj`, Git Bash
  `/c/Users/x/proj`) are compared case-blind and separator-blind, never by string
  prefix.


Read exception for transcripts: each agent stores its transcripts outside
the repo, and no copy of them exists inside it, so the plugin reads them where
they are. These stores are read-only to the plugin; it never writes there:

| Agent | Transcript store (read-only) |
|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR` or `~/.claude`, then `projects/<encoded-cwd>/*.jsonl` (Windows: `%USERPROFILE%\.claude`) |
| Codex CLI | `$CODEX_HOME/sessions/` or `~/.codex/sessions/` (`YYYY/MM/DD/rollout-*.jsonl`) |
| Kiro | `$KIRO_HOME` or `~/.kiro`, then `sessions/`: both v3 layouts are read, `<hash>/sess_<id>/` (`session.json`, `messages.jsonl`, `sub-executions/`; `tool-outputs/` is never opened) and `cli/<uuid>.json` + `<uuid>.jsonl` (`.history` and `.lock` are never opened). Older v1/v2 `data.sqlite3` is not read; a note appears when it holds conversations |
| Copilot CLI | `$COPILOT_HOME` or `~/.copilot`, then `session-state/<id>/events.jsonl` |
| Cursor CLI | `$CURSOR_HOME` or `~/.cursor`: `projects/*/agent-transcripts/` and `chats/*/<id>/meta.json` (the working directory); `store.db` is never opened |
| OpenCode | `$OPENCODE_HOME` or `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`: `$OPENCODE_DB` if set, else `opencode.db` then any `opencode-<channel>.db` (SQLite, opened read-only and immutable); `auth.json` and the desktop `.dat` files are never opened |
| Antigravity CLI (`agy`) | `$ANTIGRAVITY_HOME` or `~/.gemini/antigravity-cli`: `conversations/<id>.db` (SQLite, opened read-only and immutable, so no `-wal`/`-shm` file is created) and `history.jsonl` (working-directory fallback); `<id>.pb` conversations are counted, never read. `brain/<id>/` (artifacts and a JSONL transcript) is a separate store and is not read |

System prompts, steering files, reasoning and account ids recorded by the host
are dropped before anything is previewed or sent.

## Notes per agent

- **Codex** runs commands in a sandbox that blocks network access by default. If a
  submit reports that the server was not reached, approve network access for that
  command and run it again. Nothing is lost: the session is retried whole.
- **Cursor** does not record tool outputs, so file contents that tools read or
  produced are not in its transcripts; the submit prints a note per session.
- **Antigravity** publishes no schema for its chat files. The reader was written by
  observing `agy` 1.3.1 and is unverified: it carries over user prompts, assistant
  text, tool calls and `view_file` results only. Every other step is skipped and the
  submit prints a note with the count. Conversations stored as `.pb` files are not
  supported (one note counts them).
- **OpenCode** is read from its SQLite store (`opencode.db`), verified against
  opencode 1.18.x. Reasoning, injected prompts and provider ids are dropped; other
  part types (patches, files, compactions) are counted in a note. Sub-agent
  sessions are not included. The older JSON-file layout is not supported.
- **Copilot CLI**: the conversation format is taken from published docs and has not
  been verified against a real transcript; the submit says so when it reads one.
- **Kiro** reads both v3 session layouts (a session id found in both is read once).
  In the `cli/` layout reasoning is dropped and unknown record kinds are counted in a
  note. **Kiro** and **Cursor** readers were built from real session files of those
  tools; none of the non-Claude readers has been confirmed against the server yet.


## Troubleshooting

| You see | Cause and fix |
|---|---|
| `FAILED: no token file at .../token.txt` | You are already connected (the file is deleted after a good connect), or you have not run `--init` yet. Check for `.cadra/config.json`. |
| `FAILED: this token carries no assessment` | The pasted value is not a trace token (for example an OpenCode token). Re-copy the right one from Setup. |
| `FAILED: the proxy address must be an https:// URL` | Use an `https://` address; for a local proxy use a tunnel. |
| `FAILED ... server was not reached` | Server or tunnel is down, or (Codex) network access was not approved. Nothing is lost; run again. |
| `NOT CONNECTED` | No `.cadra/config.json` in this workspace or above it. Connect first. |
| `FOUND 0 session(s)` | No session of a supported agent started in this workspace. Start the agent from the repository root. |
| `python3` not found (Windows) | Use `py -3` or `python` for the scripts; the skills try `python3`, so install Python 3.11+ from python.org with "Add to PATH", or enable the `py` launcher. |
| Skills do not appear | Restart the agent; check the agent's plugin list (`claude plugin list`, `codex plugin list`, `agy plugin list`, `/skills list`). |
| A host's sessions missing | An older installed copy lacks that reader. Reinstall the plugin for that agent. |


## Running the scripts directly

The skills just run three scripts, so you can too. Use an absolute `--workspace`.

**macOS / Linux**

```bash
SCRIPTS="$HOME/Cadra-Claude-Code-Plugin/plugins/cadra-trace-tracker/scripts"

python3 "$SCRIPTS/cadra_connect.py" --workspace /path/to/solution --init
python3 "$SCRIPTS/cadra_connect.py" --workspace /path/to/solution --proxy https://YOUR-PROXY
python3 "$SCRIPTS/cadra_submit.py"  --workspace /path/to/solution --dry-run
python3 "$SCRIPTS/cadra_submit.py"  --workspace /path/to/solution
python3 "$SCRIPTS/cadra_traces.py"  --workspace /path/to/solution
```

**Windows (PowerShell)**: use `py -3` (or `python`) in place of `python3`.

```powershell
$S = "$HOME\Cadra-Claude-Code-Plugin\plugins\cadra-trace-tracker\scripts"
$W = "C:\path\to\solution"

py -3 "$S\cadra_connect.py" --workspace $W --init
py -3 "$S\cadra_connect.py" --workspace $W --proxy https://YOUR-PROXY
py -3 "$S\cadra_submit.py"  --workspace $W --dry-run
py -3 "$S\cadra_submit.py"  --workspace $W
py -3 "$S\cadra_traces.py"  --workspace $W
```

`cadra_submit.py` options:

| Option | Meaning |
|---|---|
| `--dry-run` | Build and preview the payload; send nothing. |
| `--host NAME` | Only collect this agent's sessions (repeatable). Names: `claude-code`, `codex`, `kiro`, `github-copilot-cli`, `cursor`, `antigravity`, `opencode`. Default: all. |
| `--claude-root`, `--codex-root`, `--kiro-root`, `--copilot-root`, `--cursor-root`, `--antigravity-root`, `--opencode-root` | Read that agent's transcripts from another folder (testing). `--projects-root` is the older name for `--claude-root`. |


## Local proxy testing

`cadra-connect` only accepts `https://` proxy URLs, so a proxy on `localhost`
needs a tunnel, for example `cloudflared tunnel --url http://localhost:8787`.
Install `cloudflared` with `brew install cloudflared` (macOS),
`winget install Cloudflare.cloudflared` (Windows; open a new terminal after), or
on Linux the package/binary from Cloudflare's downloads page (unrun here).
Connect with the address it prints (base address only, no `/v1`). Quick-tunnel
addresses change every run, so connect again with the new one.


## Tests

Run the tests with `python -m pytest -q` from the repository root. Two tests in
`tests/test_adapt.py` use Windows-style `C:/` paths and fail on macOS and Linux;
they fail the same way on the original code.
