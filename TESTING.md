# Cadra Trace Tracker — Testing Guide (v3.0.0)

For whoever is exercising the plugin by hand before it goes to candidates.
Windows, macOS and Linux; the plugin is Python 3.11+ standard library only, so
there is nothing to install beyond Python and the plugin itself.

> **Read this first.** The server half does not exist yet. `POST /v1/traces` is
> unbuilt, and `GET /v1/traces` is unbuilt with it. Everything up to and
> including the pre-send preview can be tested for real today; everything past
> the moment bytes leave the machine cannot. Part 5 lists exactly what is
> blocked, and the authoritative version of that list is the handoff block at
> the end of `docs/plans/2026-08-12-byo-trace-capture.md`.

## How the system works (30-second read)

Three skills, no hooks, no background processes, no database credentials.

| Say | Skill | What it does |
|---|---|---|
| "connect to Cadra" | `cadra-connect` | Validates the assessment token against the proxy, writes `<workspace>/.cadra/config.json`, appends `.cadra/` to `.gitignore` |
| "submit my trace" | `cadra-submit` | Dry run → you approve → real submission with a server receipt |
| "show my traces" | `cadra-traces` | Read-only list of what the server holds |

Claude Code's transcripts are read from `~/.claude/projects/<encoded-cwd>/` and
never modified. The plugin writes only inside `<workspace>/.cadra/`, plus that
one `.gitignore` append.

---

## Part 0 — The automated suite

From the repository root:

```
python -m pytest -q
```

- [ ] All tests pass. No test makes a network call; every one runs against a
      temp workspace and a temp transcript root.
- [ ] `tests/test_extraction_parity.py` reports 2 passed, not 2 skipped. It
      skips when the `cadra-prototype` checkout is not beside this repo; point
      it at one with `CADRA_PROTOTYPE_ROOT=/path/to/cadra-prototype`. This is
      the test that proves the envelope actually yields attestations in the
      server's extractor — a skip here means the load-bearing check did not run.

---

## Part 1 — Install

```
claude plugin marketplace add <MARKETPLACE_GIT_URL_OR_LOCAL_PATH>
claude plugin install cadra-trace-tracker@cadra
```

- [ ] Both commands succeed; `claude plugin list` shows `cadra-trace-tracker`
      enabled at version 3.0.0.
- [ ] `/hooks` lists **nothing** from this plugin. v3 ships no hooks; if any
      appear, an old install is still present.
- [ ] Prepare a scratch git repository to stand in for a candidate's solution
      repo, with a real `origin` remote and at least one commit. Use a fresh
      one — a leftover `.cadra/` from an earlier run masks first-run behaviour.

---

## Part 2 — Connect

Run `claude` **from the root of that repository** and say "connect to Cadra".

- [ ] Claude runs `--init` first, which creates `.cadra/` and adds it to
      `.gitignore`, then prints a `READY` line naming `.cadra/token.txt`.
      It does not ask for a user ID, roll number, or project name — those are v2
      concepts and are gone.
- [ ] **Claude asks _you_ to paste the token into that file yourself.** It must
      not offer to write it for you, ask you to paste it into the chat, or read
      the file back. This is the point of the design: anything Claude types
      appears in the transcript, and the transcript is what gets uploaded.
      Check afterwards that no `--token` argument appears anywhere in the
      session — the script has no such option.
- [ ] Claude never echoes the token back to you, in this step or any later one.
- [ ] On success the script prints `CONNECTED workspace=<path>`, `.cadra/token.txt`
      is **gone** (consumed, so there is only one copy), and Claude tells you
      submission is ready.
- [ ] `.cadra/config.json` exists and contains the token, `proxy_base_url`,
      `workspace_root`, `git_remote` and `connected_at`. On macOS/Linux its mode
      is `600`.
- [ ] `.gitignore` now contains a `.cadra/` line, and `git status` shows the
      `.cadra/` folder as ignored — the token is not stageable.

Failure paths, each of which must leave **no** `.cadra/` behind:

- [ ] A garbled token → `FAILED: ... Re-copy the token from your Setup page.`
- [ ] A token whose `exp` is in the past → `FAILED: this token has expired.`
- [ ] An unreachable proxy URL → `FAILED: could not reach Cadra (...). Nothing
      was saved.` and Claude does not claim the workspace is connected.
- [ ] An `http://` proxy URL → `FAILED: the proxy address must be an https:// URL.`
      Redirects carry the Authorization header, so plain http is refused outright.
- [ ] Connecting with your home directory as the workspace → `FAILED: ... too
      broad to be a project.` It would otherwise pull in every project you have.

Until the proxy exists, the only way past this step is a local stub — see the
appendix. That is a testing instrument, not a supported flow.

---

## Part 3 — Dry run, the consent gate

Do a few turns of real-looking work in the connected repository (have Claude
write and edit some files), then say "submit my trace".

- [ ] Claude runs `cadra_submit.py --dry-run` **first**, before anything is sent.
- [ ] Output includes `FOUND n session(s) started in this workspace.` — stated
      even when `n` is 0.
- [ ] One `CWD <id> — <dir>` line per directory each session visited, and Claude
      reads those directories out to you. This is the point of the gate: a
      session that started here but wandered somewhere private is visible before
      any bytes leave.
- [ ] A `DRY-RUN <id> — n messages, k chunk(s), r redaction(s). Nothing was
      sent.` line per session.
- [ ] `DRY-RUN complete — nothing was sent.` at the end.
- [ ] Claude **stops and asks for your agreement**. It must not submit on its
      own initiative. Say no once: nothing is sent, and there is nothing to undo.
- [ ] `.cadra/last-preview.json` now exists and contains each session's full
      message array — the exact payload. Open it and read it; that is a
      transparency check a candidate is expected to be able to do.
- [ ] `.cadra/state.json` does **not** exist yet. A dry run advances nothing.

Scope and redaction checks, all visible in the preview:

- [ ] Start a session from a directory **above** the workspace, work in it, then
      dry-run again: that session does not appear, and is not mentioned. It is
      out of scope by design and is never read (spec §6.1).
- [ ] Start a session from a **subdirectory** of the workspace: it does appear.
- [ ] Paste an image into a session, then dry-run: the preview contains
      `[image removed before upload]` and no base64.
- [ ] Have Claude read a file containing something shaped like a secret (an API
      key in a `.env`), then dry-run: the value is replaced in the preview and
      the `redaction(s)` count is non-zero.
- [ ] Use a subagent (Task tool), then dry-run: the subagent's messages appear
      in timestamp order alongside the main transcript, tagged with
      `cadra_agent` carrying the real `agent_type` and `parent_tool_use_id`.
- [ ] The current session's last few turns are missing from the preview. That is
      expected — Claude Code writes the transcript as it goes.

---

## Part 4 — Submit, and the read-back

Approve the submission. **Everything in this part needs the proxy** (Part 5).

- [ ] `SUBMITTED <id> — server confirmed n messages at <timestamp>` — a server
      receipt, quoted from the response, not a local claim.
- [ ] `.cadra/state.json` now records the session.
- [ ] Say "submit my trace" again with no new work → `SKIP <id> — already
      submitted, unchanged`, then `Everything is already submitted`.
- [ ] Do one more turn of work, submit again → that session is re-sent (its
      content fingerprint changed), and nothing else is.
- [ ] Say "show my traces" → one compact line per stored session with its span,
      message count and size. Claude describes sessions by span and size, never
      by their opening message.
- [ ] Kill the network and submit → `FAILED ... HINT: the server was not
      reached. Nothing was lost` and `state.json` is unchanged, so the next run
      retries the session whole.
- [ ] Kill the network and say "show my traces" → `UNAVAILABLE`, and Claude says
      the server could not be reached rather than guessing from local files.

Write-boundary spot check, worth doing by hand even though a test covers it:

- [ ] `git status` in the repository after a connect, a dry run and a real
      submit shows **no** changes other than the one `.gitignore` line.
- [ ] The transcript files under `~/.claude/projects/` have unchanged mtimes.

---

## Part 5 — What cannot be tested yet

`POST /v1/traces` and `GET /v1/traces` do not exist. Every network path in the
plugin was built against stubs, so the following are unverified and must be
re-tested the day the proxy ships. The full, authoritative list — consolidated
from the review notes on each commit of this branch — is the **Handoff** block at
the end of `docs/plans/2026-08-12-byo-trace-capture.md`. In summary:

- `cadra-connect` against a live `GET /v1/traces` — the 200-vs-401 signal.
- A real chunked submission and its receipt: the 200/202 split, the
  `messages` / `received_at` / `session_id` fields the `SUBMITTED` line quotes,
  and whether a duplicate chunk returns 200 or 202.
- The error body shape `{"error": {"code", "message"}}` behind 401/403/409, and
  the codes `rejected_revoked`, `rejected_expired`, `binding_mismatch` —
  including `binding_mismatch` behaviour after the git remote is changed.
- That `prefix_hash` / `chunk_hash` chaining matches what the server validates,
  and that a 4 MB body survives the proxy's ingress.
- That the nested §7.4 envelope is what the server expects, and that the server
  derives the assessment id from the token (the client sends no `assessment_id`).
- The `GET /v1/traces` list shape backing `cadra-traces`.
- That submitted traces produce non-zero attestations **server-side**, end to
  end. `tests/test_extraction_parity.py` proves this against the extractor
  function locally; it does not prove it through the ingest path.

Until then, Parts 0–3 are the real acceptance surface, and Part 4 can only be
rehearsed against the stub below.

---

## Appendix — local proxy stub (testing only)

Standard library only; accepts any token, accepts every chunk, and stores
nothing. It exists so Parts 2–4 can be walked through end to end before the real
proxy is built. Save as `stub_proxy.py` **outside** the test repository and run
`python stub_proxy.py`, then connect with `--proxy https://127.0.0.1:8787`.

Plain `http://` is refused by design, so the stub needs a TLS certificate —
generate a self-signed one and wrap the socket with `ssl.SSLContext`. That
friction is deliberate: the check it exercises is what stops a redirect from
walking off with the assessment token. Do not add an "allow http for testing"
flag to the shipped code to avoid it.

```python
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._reply(200, {"sessions": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        print("chunk", body["chunk"], len(body["messages"]), "messages")
        self._reply(202, {"accepted": True,
                          "session_id": body["session"]["session_id"],
                          "messages": len(body["messages"]),
                          "received_at": "2026-08-12T00:00:00Z"})


HTTPServer(("127.0.0.1", 8787), Handler).serve_forever()
```

A stub that always says yes proves the plugin's happy path and nothing about the
contract. Do not let a green run against it be mistaken for Part 5 being done.
