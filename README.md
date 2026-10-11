# Cadra Trace Tracker — Coding Agent Plugin

Submit your coding-agent work sessions to Cadra for a coding assessment. Works
with **Claude Code, OpenAI Codex CLI, Kiro, GitHub Copilot CLI, Cursor CLI,
Google Antigravity CLI (`agy`) and OpenCode**.

The plugin is **skills only**: there are no hooks and no background processes.
Nothing is read, converted, or sent unless you ask for it in a conversation. All
state lives in your workspace's `.cadra/` folder, which the plugin gitignores
before it writes anything into it.

## The three skills

| Say | Skill | What happens |
|---|---|---|
| "connect to Cadra" | `cadra-connect` | Registers this workspace against your assessment token and proves the token works right away. Writes `.cadra/config.json`. |
| "submit my trace" | `cadra-submit` | Previews exactly what would be sent, waits for your go-ahead, then submits and reports the server's receipt. |
| "show my traces" | `cadra-traces` | Read-only list of what the server actually holds. Never uploads. |

You can submit as many times as you like. Each session is sent again only if it
has changed since the last accepted submission. One submit collects the sessions
of **every** supported agent that ran in this workspace.

## Requirements

- Python 3.11+ on `PATH`. The skills say `python3`; use `python3` if it exists,
  else `python`, else `py -3` (usual on Windows). The plugin uses the standard
  library only; there is nothing to `pip install`.
- One of the agents above, started in the root of your solution repository.
- Your assessment token and the proxy URL from your Cadra Setup page.

## Install

Repository: https://github.com/NishantCadra/Cadra-Claude-Code-Plugin

Claude Code, Codex and Copilot CLI install straight from GitHub. Cursor,
Antigravity, OpenCode and Kiro load a local folder, so get the files first (the
location below is only a suggestion; any folder works).

### Get the files

**macOS / Linux (bash or zsh)**

```bash
git clone https://github.com/NishantCadra/Cadra-Claude-Code-Plugin.git "$HOME/Cadra-Claude-Code-Plugin"
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/NishantCadra/Cadra-Claude-Code-Plugin.git "$HOME\Cadra-Claude-Code-Plugin"
```

The plugin folder is `<clone>/plugins/cadra-trace-tracker` below. After
installing, **restart the agent**: skills load when a session starts.

### Claude Code

The same commands on every OS (macOS, Linux, Windows PowerShell):

```
claude plugin marketplace add https://github.com/NishantCadra/Cadra-Claude-Code-Plugin
claude plugin install cadra-trace-tracker@cadra
```

Check: `claude plugin list`. Then start `claude` and say "connect to Cadra".

### Codex CLI

Same on every OS:

```
codex plugin marketplace add NishantCadra/Cadra-Claude-Code-Plugin
codex plugin add cadra-trace-tracker@cadra
```

Check: `codex plugin list`. In Codex, type `$cadra-connect` (or say "connect to
Cadra"); `/skills` also lists the three skills.

### GitHub Copilot CLI

Same on every OS:

```
copilot plugin install NishantCadra/Cadra-Claude-Code-Plugin:plugins/cadra-trace-tracker
```

Check: `/skills list` inside `copilot` shows the three skills.

### Cursor CLI

Cursor loads the plugin from a local folder (get the files first). Start it with
the folder every time:

```bash
# macOS / Linux
agent --plugin-dir "$HOME/Cadra-Claude-Code-Plugin/plugins/cadra-trace-tracker"
```

```powershell
# Windows (PowerShell)
agent --plugin-dir "$HOME\Cadra-Claude-Code-Plugin\plugins\cadra-trace-tracker"
```

(`cursor-agent` is the older name of the same binary.) Pass `--plugin-dir` every
time you start a session that should have the skills. Then type `/cadra-connect`,
`/cadra-submit` or `/cadra-traces`.

### Antigravity CLI (`agy`)

Needs the local files:

```bash
# macOS / Linux
agy plugin install "$HOME/Cadra-Claude-Code-Plugin/plugins/cadra-trace-tracker"
```

```powershell
# Windows (PowerShell)
agy plugin install "$HOME\Cadra-Claude-Code-Plugin\plugins\cadra-trace-tracker"
```

Check: `agy plugin list`. Then say "connect to Cadra".

### OpenCode

OpenCode does not read Claude plugins, so one script copies the three Cadra skills
and the `/cadra-*` commands into your OpenCode config. It needs only Python 3 and
the local files from "Get the files" above.

1. Run the installer.

   macOS / Linux:

   ```bash
   python3 ~/Cadra-Claude-Code-Plugin/plugins/cadra-trace-tracker/scripts/install_opencode.py
   ```

   Windows (PowerShell):

   ```powershell
   py -3 "$HOME\Cadra-Claude-Code-Plugin\plugins\cadra-trace-tracker\scripts\install_opencode.py"
   ```

   This installs globally into `~/.config/opencode` (`%USERPROFILE%\.config\opencode`
   on Windows). To install for one project only, add `--project <DIR>`. To preview
   without writing anything, add `--dry-run`.

2. Restart OpenCode.

3. In your solution folder, type `/cadra-connect`, then `/cadra-submit` when you are
   done. `/cadra-traces` lists what the server holds.

No environment variables are needed. The installer writes the plugin's location into
the copied skills and does not touch your shell profile.

Check the install (write to a file first; piping `opencode debug skill` straight into
`grep` can truncate the output):

```bash
opencode debug skill > /tmp/skills.json; grep cadra /tmp/skills.json
```

```powershell
opencode debug skill > $env:TEMP\skills.json; Select-String cadra $env:TEMP\skills.json
```

Update: `git pull` in the clone, then run the installer
again. Uninstall: run it with `--uninstall`; it removes only the three Cadra skills
and three command files it created. Moving or renaming the cloned folder breaks the
copies; run the installer again.

### Kiro

- **Kiro IDE:** Powers panel → **Add Custom Power** → local path → select the
  plugin folder: `~/Cadra-Claude-Code-Plugin/plugins/cadra-trace-tracker` on
  macOS/Linux, `%USERPROFILE%\Cadra-Claude-Code-Plugin\plugins\cadra-trace-tracker`
  on Windows. Then say "connect to Cadra".
- **Kiro CLI:** it has no plugin command. Use the IDE power, or run the scripts
  directly (see "Running the scripts directly" below).

The GitHub install gives whatever is on `main`. Agents other than Claude Code
get their readers only after the multi-agent branch is merged there.

### Python command

The skills run `python3`. On macOS/Linux that is normal. On Windows `python3` is
often missing or a Store stub; use `py -3` or `python` when you run the scripts by
hand, and make sure one of them reports 3.11+ (`py -3 --version`).

### Update or reinstall

Every agent installs a copy, so reinstall after the plugin changes. The commands
are the same on every OS except where a shell form is shown:

| Agent | Reinstall |
|---|---|
| Claude Code | `claude plugin marketplace remove cadra`, then the two install commands again |
| Codex | `codex plugin remove cadra-trace-tracker@cadra`, then `codex plugin add cadra-trace-tracker@cadra` |
| Copilot | `copilot plugin uninstall cadra-trace-tracker`, then install again |
| Cursor | nothing to reinstall: `--plugin-dir` reads the folder directly (`git pull` the clone to update it) |
| Antigravity | `agy plugin uninstall cadra-trace-tracker`, then `git pull` the clone and install again |
| OpenCode | `git pull` the clone, then run `install_opencode.py` again (`--uninstall` removes it) |
| Kiro IDE | `git pull` the clone, remove the power in the Powers panel and add it again |

Reinstalling never touches your connection, which lives in the workspace
(`.cadra/`), not in the plugin.

## Usage

### 1. Connect (once per workspace)

1. In your solution repository, start your agent and say **"connect to Cadra"**.
   It creates `.cadra/` and adds it to `.gitignore`, then tells you where to put
   the token: `<workspace>/.cadra/token.txt`.
2. **Paste the token into that file yourself and save it.** Do not paste it into
   the chat and do not put it on a command line: the transcript is uploaded on
   submit, so a token in the chat would be uploaded too.
3. Say **"connect"** again and give the proxy URL from your Setup page. It must
   start with `https://`. The plugin proves the token against the server, saves
   `.cadra/config.json`, and deletes `token.txt`.

Both live assessment tokens and offline-capture tokens are accepted. You see
`CONNECTED` on success; on `FAILED` nothing is saved.

Already connected? Do not connect again. `token.txt` is deleted after a
successful connect, so a "no token file" message only means you are done.

**Changing the proxy URL.** The URL is stored in `.cadra/config.json` at connect
time, and nothing else changes it. To switch servers, put the token back in
`.cadra/token.txt` and connect again with the new URL. If you name no URL, the
agent may fall back to the default in the skill text, so always state it.

### 2. Submit

Say **"submit my trace"**. The plugin first runs a dry run: it lists the sessions
found, the folders each one visited, the message and redaction counts, and writes
`.cadra/last-preview.json` with exactly what would be sent. **Nothing leaves your
machine until you agree.** After you confirm it sends, and prints the server's
receipt per session (`SUBMITTED ... server confirmed N messages`).

### 3. Check what Cadra holds

Say **"show my traces"** for a read-only list of the sessions the server stored.

## Filesystem contract

```
<workspace>/
  .gitignore          ← cadra-connect appends ".cadra/" if it is not already there
  .cadra/
    token.txt         ← only while connecting: you paste the token here; deleted on success
    config.json       ← token, proxy URL and assessment binding (chmod 0600 on macOS/Linux)
    state.json        ← which sessions have been accepted, and at what content
    last-preview.json ← exactly what the last submit sent, or would have sent
```

**The write boundary is the load-bearing guarantee:** the plugin writes *only*
inside `<workspace>/.cadra/`, plus that single append to `<workspace>/.gitignore`
at connect time. Every other filesystem access is read-only.

The plugin also reads each supported agent's own saved chat history, which lives
outside your repository. That access is read-only: it never writes there. System
prompts, reasoning and account ids recorded by the agent are dropped before
anything is previewed or sent.

## Privacy model

- **You trigger everything.** No hooks, no session-end uploads, no background
  daemon. If you never say "submit my trace", nothing ever leaves the machine.
- **Preview before send.** `cadra-submit` runs a dry run first: it builds the
  exact payload, writes it to `.cadra/last-preview.json`, prints every working
  directory the sessions visited, and sends nothing. You decide from there.
- **Scope.** Only sessions started in this workspace or below it are collected.
  Sessions started elsewhere are never read.
- **Redaction.** Secrets matching the shared rule set are replaced before the
  payload is hashed or sent, and the affected files are reported to the server so
  they are excluded from scoring rather than counted against you.
- **No database credentials.** The plugin holds one assessment token, issued to
  you, and talks only to the Cadra proxy. Identity is derived server-side from
  the token; the client never asserts who you are.
- **The server is the authority.** A submission is reported as saved only when
  the server has confirmed it.

## Repository layout

```
.claude-plugin/marketplace.json          marketplace listing
plugins/cadra-trace-tracker/
  plugin.json                            Agent Plugins 1.0 manifest (all hosts)
  .claude-plugin/plugin.json             Claude Code manifest
  skills/cadra-connect/                  register the workspace
  skills/cadra-submit/                   preview, confirm, submit
  skills/cadra-traces/                   read-only view of stored sessions
  scripts/cadra_connect.py               connect entry point
  scripts/cadra_submit.py                submit entry point
  scripts/cadra_traces.py                list entry point
  scripts/cadra/                         collect, adapt, redact, envelope, client
  scripts/cadra/codex.py                 Codex CLI rollout reader
  scripts/cadra/kiro.py                  Kiro session reader
  scripts/cadra/copilot.py               Copilot CLI session reader
  scripts/cadra/cursor.py                Cursor CLI transcript reader
  scripts/cadra/antigravity.py           Antigravity CLI conversation reader
  scripts/cadra/opencode.py              OpenCode session reader
  scripts/install_opencode.py            OpenCode installer (skills + /cadra-* commands)
```
