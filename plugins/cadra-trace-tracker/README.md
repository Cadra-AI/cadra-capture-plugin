# Cadra Trace Tracker — Claude Code Plugin

Submit your Claude Code work sessions to Cadra for a coding assessment.

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
has changed since the last accepted submission.

## Install

```
claude plugin marketplace add https://github.com/NishantCadra/Cadra-Claude-Code-Plugin
claude plugin install cadra-trace-tracker@cadra
```

Then, in the root of your solution repository, run `claude` and say
"connect to Cadra". The plugin prepares `.cadra/` and asks you to **paste the
assessment token from your Cadra Setup page into `.cadra/token.txt` yourself** —
never into the chat (chat is recorded in the transcript that later gets
uploaded). You will also provide the proxy URL from Setup (must be `https://`).
Requires Python 3.11+ on `PATH`; the plugin uses the standard library only, so
there is nothing to `pip install`.

Always start `claude` from that same solution root so your sessions are found
when you submit.

## Filesystem contract

```
<workspace>/
  .gitignore          ← cadra-connect appends ".cadra/" if it is not already there
  .cadra/
    config.json       ← token and assessment binding (chmod 0600 on macOS/Linux)
    state.json        ← which sessions have been accepted, and at what content
    last-preview.json ← exactly what the last submit sent, or would have sent
```

**The write boundary is the load-bearing guarantee:** the plugin writes *only*
inside `<workspace>/.cadra/`, plus that single append to `<workspace>/.gitignore`
at connect time. Every other filesystem access is read-only. This is enforced by
`tests/test_write_boundary.py`, which snapshots the whole workspace byte for byte
around a real submission, a dry run, and a connect.

There is one stated read exception: Claude Code stores transcripts under
`~/.claude/projects/<encoded-cwd>/`, and no copy of them exists inside the repo,
so the plugin reads them there. It never writes there, and your transcripts are
left untouched — also a test.

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
  .claude-plugin/plugin.json             plugin manifest
  skills/cadra-connect/                  register the workspace
  skills/cadra-submit/                   preview, confirm, submit
  skills/cadra-traces/                   read-only view of stored sessions
  scripts/cadra_connect.py               connect entry point
  scripts/cadra_submit.py                submit entry point
  scripts/cadra_traces.py                list entry point
  scripts/cadra/                         collect, adapt, redact, envelope, client
docs/specs/                              design spec
docs/plans/                              implementation plan
tests/                                   pytest suite (dev only)
```

Run the tests with `python -m pytest -q` from the repository root.
