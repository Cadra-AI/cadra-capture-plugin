# BYO trace capture — plugin design

**Date:** 2026-08-11
**Scope:** this repository only — what the Claude Code plugin collects, transforms,
redacts and submits.
**Companion spec:** the ingest route, token minting, session-state policy,
server-side redaction re-scan, attestation extraction wiring, storage and
migrations are specified separately in `cadra-prototype`
(`docs/superpowers/specs/2026-08-12-byo-trace-ingest-design.md`).
**Evidence base:** [`../cadra-integration-assessment.md`](../cadra-integration-assessment.md)
(gap register G1–G13, G5 spike), the `tool-results/` offload spike
(`docs/spike/tool_results_offload_spike.py`), and the measurements in §9 below.

---

## 1. Purpose

Cadra assesses how a candidate builds with an AI agent. The managed path runs
OpenCode through the Cadra proxy, which observes every model call and derives the
F6 integrity evidence from it. Candidates who must work in **their own
environment** — own machine, own model key, no proxy in the loop — produce no
such evidence today.

This plugin closes that gap. It reads the transcripts Claude Code already writes,
converts them to the canonical envelope Cadra's extractor consumes, strips
secrets, and submits them to the Cadra proxy under a per-assessment token.

**Non-goal:** enforcing limits. BYO sessions are unbounded — no prompt cap, no
48-hour window, no forced model. The report marks BYO provenance rather than
pretending the constraints applied. Reconstructing a fake budget from
candidate-supplied data would be weak enforcement presented as strong.

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Skills only. No hooks.** | Consent must be an explicit act, not a background upload from a personal machine. Claude Code retains transcripts under `~/.claude/projects`, so nothing needs snapshotting as work happens. |
| D2 | **Submission authenticated by the existing per-assessment `f1-coding` JWT** — the same token the Setup page already issues. | Replaces the self-asserted 5-digit ID (G1). One token, one secret to manage. A separate submission-only type was considered and rejected: BYO candidates still need proxy model access for problems where their *deployed app* calls an LLM (the `app_runtime` channel), so a submission-only token would force a second secret for no gain. Chat remains bounded by `session_state.check()`, not by token type. |
| D3 | **No database credentials ship to candidates.** | Removes the published anon key (G2). The plugin's only network peer is the Cadra proxy. |
| D4 | **Canonical envelope = OpenAI-style `{"messages": [...]}`.** | Exactly what `proxy/attest.py` already consumes, so BYO and OpenCode traces feed one extractor. Agent-agnostic: a future Cursor/Codex client writes its own adapter and the server is untouched. |
| D5 | **The plugin owns all Claude Code format knowledge.** | Absolute-path rebasing, subagent merging, tab-numbered read results. None of it leaks into shared server code. |
| D6 | **Redaction runs client-side; the proxy re-scans.** | Secrets never leave the machine in the normal case, and a stale or tampered client cannot defeat the guarantee. |
| D7 | **All plugin state lives in `<workspace>/.cadra/`.** | Blast radius is the project directory. Nothing is written to the home directory or anywhere else. |
| D8 | **Scoping keys on the `cwd` recorded inside transcripts.** | The encoded folder name is both incomplete and over-inclusive (§6.1). |
| D9 | **Single Python implementation.** | The parallel PowerShell port caused roughly half of G13, including a missing verification step that made a shipped skill instruction unfulfillable on Windows. |

---

## 3. What is removed

The plugin is repurposed, not extended. Deleted outright:

- `hooks/hooks.json` and both hook scripts — D1.
- `scripts/submit-traces.ps1`, `scripts/trace-hook.ps1` — D9.
- All Supabase constants and RPC calls — D3.
- The `.claude-project` marker — it held a bearer token at the root of a repo the
  candidate publishes to GitHub.
- `_traces/` in the workspace: `raw/` snapshots (unnecessary without hooks),
  `pending/`, `sent/`, `state.json`, `tracker.log`.
- The `register-user`, `save-trace`, `my-traces` skills, superseded by §5.

---

## 4. Filesystem contract

```
<workspace>/
  .gitignore          ← cadra-connect appends ".cadra/" if not already present
  .cadra/
    config.json       ← token, assessment binding      (chmod 0600 on POSIX)
    state.json        ← per-session submission cursor
    last-preview.json ← exactly what submit would send / did send (§5.2 step 2)
```

**Invariant — enforced in review and tests:**

> The plugin writes **only** inside `<workspace>/.cadra/`, plus one append to
> `<workspace>/.gitignore` at connect time. Every other filesystem access is
> read-only.

The `.gitignore` entry is written **before** `config.json` is created, so the
token file is born ignored and cannot enter git history. The append is idempotent
and never reorders or rewrites existing entries. If `.gitignore` is absent it is
created.

**Read exception, stated explicitly:** the plugin must read
`~/.claude/projects/<encoded-cwd>/…`, because that is where Claude Code stores
transcripts and no copy exists inside the repo. The containment guarantee covers
writes, not reads.

`.cadra` must be added to `_IGNORED_DIR_PARTS` in `line_attest.py` (companion
spec) so plugin files never count toward attestation coverage.

### 4.1 `config.json`

```jsonc
{
  "token": "<assessment JWT — type f1-coding>",
  "assessment_label": "Suryaa FMCG",     // display only
  "workspace_root": "C:/Dev/my-solution",
  "git_remote": "https://github.com/candidate/solution.git",
  "proxy_base_url": "https://proxy.cadra.info",
  "connected_at": "2026-08-11T09:00:00Z"
}
```

`assessment_id` is deliberately absent — the server derives it from the token.
The client never asserts identity.

---

## 5. Skills

Three skills. Each is a thin conversational wrapper: the model collects input and
reports results; a Python script does all state manipulation and I/O. The current
plugin has the model hand-write JSON into a security-critical file, which depends
on model fidelity for correctness.

### 5.1 `cadra-connect`

Triggered by "connect to Cadra", "set up my assessment", "my token is wrong".

1. `cadra_connect.py --init` appends `.cadra/` to `.gitignore` and creates the
   folder, then names the file the candidate must paste their token into.
   **The token is never handled by the model** — not as a command-line argument,
   not written on the candidate's behalf, not read back. Every command the model
   runs is recorded in the session transcript, and `cadra-submit` uploads that
   transcript, so a token on a command line becomes a token in the upload.
2. The candidate pastes it into `.cadra/token.txt` themselves.
3. `cadra_connect.py` reads that file, validates the token's shape locally (three
   dot-separated segments, decodes as JWT, carries `coding_assessment_id`,
   unexpired) and refuses a non-`https` proxy. It does **not** assert on `type`:
   the server decides what a token may do, and hard-coding the check client-side
   would break on any future type.
4. It then calls `POST /v1/traces/bind` with the resolved `workspace_root`, which
   proves the token and claims the folder in one call. Only on success does it
   write `config.json` and delete the paste file, leaving one copy of the token.
5. Report: connected, or the specific failure (expired token, unaccepted token,
   folder already bound elsewhere — with the expected folder named — no network).

**Why this exists.** A managed OpenCode candidate discovers a bad token on their
first prompt, within seconds. A BYO candidate would otherwise discover it at
submission — after the work is done. `cadra-connect` forces the token to be
exercised on day zero.

Re-running is supported and expected: tokens get reissued, and candidates change
machines. A re-connect that changes `git_remote` is accepted locally; the server
decides whether to flag it.

#### What `binding` is for, and what it cannot do

`git_remote` and `git_branch` are **read fresh at submit time**, never taken from
`config.json`. The remote is not stable across an assessment, and its instability
is the ordinary path rather than an anomaly: Setup hands the candidate a *template*
repo, and they push to a public repo of their own at the end. So across one
assessment `origin` can be absent (they downloaded the template), then the
template, then their own repo.

That has a consequence the ingest design has to absorb: **the remote cannot be a
record-time gate.** The authority it would be checked against —
`coding_assessment_runs.repo_url` — is created by `POST /coding-assessments/{id}/submit`,
the same call that flips the assessment to `submitted` and starts grading. It does
not exist while traces are arriving. A trust-on-first-use rule over the remote
would therefore compare against nothing but the candidate's own earlier claim, and
would reject the template→own-repo transition, which is correct behaviour.

So the remote is **evidence, reconciled at grade time**: record every remote a
submission was captured under, never reject on it, and when `repo_url` finally
arrives, compare. Traces captured entirely against an unrelated repo are a real
provenance signal; the template→own-repo progression reads as what it is.

Credentials are stripped before the remote is stored or sent. A repo cloned as
`https://user:ghp_xxx@github.com/o/r.git` yields that string verbatim from git,
and the server's comparison normalises credentials away anyway — so transmitting
them stores a live token that is discarded unread at the only point it is used.
Stripping applies to `http(s)` only: in `ssh://git@github.com/…` the userinfo is
a login name, not a secret.

**The record-time check that does work is `workspace_root`, and it binds at
connect.** `cadra-connect` calls `POST /v1/traces/bind` with the resolved
workspace, which proves the token and claims the folder in one call. Binding at
the first *submission* instead would leave a trust-on-first-use window with the
failure pointing the wrong way: the first submission is accepted whatever folder
it comes from, so a candidate whose first submission is from the wrong project
binds to it and has their real work rejected for days afterwards. Connect happens
on day zero, while they are following the Setup instructions in the folder they
just prepared, and a mismatch is reported there — naming the expected folder — with
nothing saved.

The submit-time check stays as well. Connect is a client-side call and a crafted
request can skip it, so `POST /v1/traces` re-checks against the same binding.

**The record-time check that does work is `workspace_root`.** Unlike the remote it
exists from the moment the candidate connects, and it does not move during normal
work. Compare the **folder name**, not the full path — `C:\Dev\solution` and
`/home/me/solution` are one project on two machines, and rejecting that buys
nothing. A candidate who connects an unrelated side project has a different folder
name, which is the accidental-capture and casual-misdirection case this is for.
A changed full path with an unchanged folder name is recorded and reported, not
rejected.

The value the plugin sends is the directory that actually contains
`.cadra/config.json`, resolved at submit time — not the string stored at connect.
Copying `config.json` into another folder therefore produces a different
`workspace_root`, which is exactly what the check catches.

### 5.2 `cadra-submit`

Triggered by "submit my trace", "save my work", "send my session to Cadra".

1. Resolve the assessment: walk up from the cwd to the first directory containing
   `.cadra/config.json`; that directory is the workspace. Nested workspaces
   therefore resolve to the innermost one. If none is found, stop and point at
   `cadra-connect` — never guess.
2. Collect (§6), transform (§7), redact (§8), and write `last-preview.json`.
   This is `cadra_submit.py --dry-run`: the full pipeline with the network step
   omitted, so the preview is genuinely *pre*-send rather than a record of what
   already left.
3. **Show the candidate a summary before sending**: sessions, turn counts, byte
   size, the redaction count, and every directory each session visited
   (`session.cwds`). This is the consent surface — a session that wandered
   outside the workspace is visible here before anything leaves the machine.
   The skill must not proceed without the candidate's agreement; a preview they
   are shown after the upload is not consent. Also state the session count even
   when it is zero, so a candidate who launched `claude` from the wrong
   directory sees it (§6.1).
4. Submit per session, chunked (§9) — a second run of the same script without
   `--dry-run`.
5. Report the server's receipt verbatim — never a local claim of success.

Submission is idempotent: resubmitting an unchanged session is a no-op server-side,
and a grown session replaces its predecessor.

### 5.3 `cadra-traces`

Triggered by "show my traces", "what have I submitted", "is my work saved".

Read-only. Calls `GET /v1/traces` and renders one line per stored session —
time span, turn count, size, submitted-at. Never displays transcript content, and
never triggers an upload. If the server is unreachable it says so rather than
falling back to a local guess.

---

## 6. Collection

### 6.1 Which sessions belong to the assessment

**Rule: a session belongs to the assessment iff its *origin cwd* — the `cwd` of
the first entry that carries one — is `workspace_root` or a descendant of it. The
session is then captured whole.**

#### Why origin cwd, and not "its cwd"

`cwd` is recorded per entry and tracks the agent's *current* directory, which
moves during a session. Measured across 489 local transcripts, **27 files contain
more than one cwd**, some with ten. One session in this very design work shows:

```
161x C:\Dev\cadra-dev
490x C:\Dev\cadra-dev\cadra-prototype
167x C:\Dev\cadra-dev\Cadra-Claude-Code-Plugin
```

So "the session's cwd" is not a single value, and a naive reading of the rule is
undefined for roughly one session in eighteen. Three candidate rules were
considered:

| Rule | Rejected because |
|---|---|
| Any entry in scope → include session | Over-inclusive: one stray command inside the workspace pulls in an entire unrelated session |
| Per-entry filtering | Splits `tool_use` from its `tool_result`, producing a transcript that breaks attestation pairing and reads as corrupt |
| **Origin cwd decides; capture whole** | **Chosen** |

Origin cwd is the directory `claude` was launched from — the candidate's own
statement of what they were working on. It is cheap to read (present on the very
first line in 91% of files), deterministic, and matches the mental model
"the session I started in my workspace".

#### The two consequences, both handled rather than hidden

**A session may leave the workspace after starting in it.** The envelope's
`session.cwds` lists every distinct cwd observed, so the server sees the
excursion. Safety is already structural: §7.1 leaves out-of-workspace tool paths
absolute, so `_GLOBAL_PATH_RE` discards them and nothing outside the workspace is
ever attested. The pre-send preview shows `session.cwds`, so a candidate whose
session wandered somewhere private can decline before anything is sent.

**A session started outside the workspace is excluded, even if most of its work
was inside.** This is the deliberate inverse of the old hook's
walk-six-ancestors bug.

Such sessions are **not** reported, and that is a decision rather than an
oversight. Reporting them would require looking in directories outside the
workspace — and because a parent's encoded name is a prefix of the workspace's,
"outside" climbs all the way to `C--Users-<name>`, the candidate's home
directory. Probing personal transcripts and printing their paths into the
pre-send preview is a worse outcome than a missing warning. It would also not
work: whether an outside session ever *touched* the workspace cannot be decided
without reading it whole, which §6.1.1 forbids, and a session started in an
unrelated directory would be missed regardless.

Prevention replaces the warning. `cadra-connect` tells the candidate to launch
`claude` from the workspace root, and `cadra-submit` states the number of
sessions found — including zero — before sending anything, so a candidate who
launched from the wrong directory sees it immediately. Sessions in directories
we *do* open and then reject (a sibling such as `C:\Dev\ws-other`) are still
reported individually.

Git worktrees fall out correctly: a cwd under `<workspace>/.claude/worktrees/…`
is a descendant, so it is included.

### 6.1.1 Finding the files — the enumeration algorithm

§6.1 is the predicate; this is how candidate files are found without reopening
G3's wounds. Every clause below exists because its absence was a real bug.

**Step 1 — candidate directories.** With
`enc(p) = re.sub(r"[^A-Za-z0-9]", "-", str(p))`, consider only directories
directly under `~/.claude/projects` whose name is `enc(workspace_root)` or begins
with `enc(workspace_root) + "-"`.

This is a *sound superset*: if a session's origin cwd is the workspace or a
descendant, then `str(cwd)` starts with `str(workspace_root)`, so `enc(cwd)`
starts with `enc(workspace_root)` — and for a descendant the next character is a
path separator, which encodes to `-`. No in-scope session can be missed. It is
deliberately not exact — a sibling `C:\Dev\ws-other` also matches — which Step 3
resolves.

Compare case-insensitively on Windows, and normalise `workspace_root` with
`Path.resolve()` at connect time so the encoding is stable.

**Step 2 — files within a candidate directory.** Exactly two globs, never a
recursive walk:

| Glob | Meaning |
|---|---|
| `<dir>/*.jsonl` | main session transcripts, one per session |
| `<dir>/*/subagents/agent-*.jsonl` | subagent transcripts, attributed to the parent `<session-id>` taken from the path |

Subagent files are **never** treated as sessions in their own right — that was the
old plugin's bug, which swept them up as separate anonymous sessions with wrong
IDs. Nothing else in the directory is read; in particular `tool-results/` is not,
per the offload spike.

**Step 3 — confirm the origin cwd per file.** Read lines only until the first
entry carrying a `cwd` (capped at 50 lines or 256 KB, then treat as unreadable and
report). Include the file only if that cwd satisfies §6.1, compared as **resolved
paths** — `Path.is_relative_to` semantics, not string prefix, so `C:\Dev\ws` never
matches `C:\Dev\ws-other`. Only in-scope files are then read in full.

This bounds reading: files outside candidate directories are never opened at all,
and files in candidate directories are read one line deep unless they qualify.

**Explicitly forbidden.** Each of these was a real defect in the current plugin:

- **No content substring matching.** The old harvester included any transcript
  merely *containing* the workspace path as a string, uploading unrelated personal
  sessions whole.
- **No `rglob`.** It sweeps subagent files up as top-level sessions and reads
  every transcript on the machine.
- **No ancestor walking.** The old gate walked six levels up and one down.
- **No other storage roots.** `local-agent-mode-sessions` under `%APPDATA%` /
  `%LOCALAPPDATA%` is the Claude *desktop* app, not Claude Code.

### 6.2 Subagent transcripts

For each selected session, also read:

```
~/.claude/projects/<encoded-cwd>/<session-id>/subagents/agent-<id>.jsonl
                                              agent-<id>.meta.json
```

**This is not optional.** Across the projects surveyed there were 452 `Agent`
tool calls and **zero** `isSidechain` entries in any main transcript — subagent
work is entirely absent from it. For one RDI session that is 38.6 MB of subagent
transcript against 11 MB of main session. Including it moved `agent_share` from
0.169 to 0.414 and flipped the integrity rollup from `caution` to `ok`.

`meta.json` supplies `toolUseId`, `agentType`, `description`, `spawnDepth` and
`model` — the linkage back to the parent `Agent` call.

The old plugin's `rglob("*.jsonl")` would sweep these up as *separate anonymous
sessions with wrong IDs*, which is worse than skipping them.

### 6.3 Incremental submission

`state.json` records, per session, the byte offset and entry count last submitted.
A session is resubmitted only if it has grown. This mirrors the server's
upsert-on-growth semantics.

---

## 7. Transformation

| Claude Code | Canonical message | Notes |
|---|---|---|
| `content[].tool_use` | `assistant.tool_calls[]` → `{id, type:"function", function:{name, arguments}}` | Name preserved as emitted (`Write`); the extractor lowercases. `arguments` stays a dict. |
| `content[].tool_result` | `{role:"tool", tool_call_id, content}` | Paired by `tool_use_id`. |
| `content[].text` (assistant) | `assistant.content` | Needed for grading and message-trace. |
| `content` string (user) | `{role:"user", content}` | Candidate turns. |
| `content[].thinking` | **dropped entirely** | See §8 — the block is discarded including its `signature`. |
| images, anywhere | placeholder text | Including inside `tool_result` parts. |
| `mode`, `permission-mode`, `ai-title`, `file-history-*`, `attachment`, `queue-operation`, `agent-name`, `last-prompt` | dropped | Not conversation. |
| subagent entries | merged into `messages` by timestamp | Tagged `cadra_agent:{parent_tool_use_id, agent_type}`. |
| entry `timestamp` | `client_ts` on every emitted message | Mandatory — see §7.3. |

Subagent messages are **interleaved into the same array**, not held separately, so
`attestation_items_from_payload` picks up their file writes with no server change.
The extractor reads only `role` and `tool_calls`; the `cadra_agent` key is ignored
by it and preserved in storage for grading.

### 7.1 Path rebasing

Absolute tool paths under `workspace_root` become workspace-relative with forward
slashes. Paths **outside** the workspace are deliberately left absolute, so the
server's `_GLOBAL_PATH_RE` discards them — writes outside the project should not
be attested, and this achieves it without a special case.

This is load-bearing, not cosmetic. `_GLOBAL_PATH_RE` (`line_attest.py:192`)
discards drive-letter paths, so without rebasing **every** `Write` and `Edit` is
dropped: the spike measured 50 writes and 58 edits reduced to a single attestation
item, `agent_share` 0.0, and flags `low_agent_share` + `unverified_authorship`. A
candidate whose agent wrote the entire repo would be automatically flagged for
cheating.

### 7.2 Line-number normalisation

Claude Code prefixes read results with `<n>\t`. The server's `_strip_line_numbers`
recognises `|`, `:` and `→` but not tab, so unstripped read text never hash-matches
and **zero** read events are produced — starving `foreign_code_rewritten`. The
plugin strips it, per D5. The server's own stripper is a safe no-op on
already-stripped text because its 80% threshold will not match.

### 7.3 Per-message timestamps are mandatory

Every emitted message carries `client_ts`, taken from the source entry's
`timestamp`:

```jsonc
{ "role": "user", "content": "…", "client_ts": "2026-08-11T09:14:22.118Z" }
```

**This is not metadata garnish — omitting it mass-flags the candidate.** The
server's velocity check measures elapsed time between an assistant row and the
next user row. Its default clock is `first_seen_at`, stamped when the row is
inserted; because a BYO session's rows are all inserted in one burst at
submission, every gap collapses to milliseconds and every long user message
trips the paste threshold. The companion spec's §11.1 makes the velocity clock
prefer `client_ts`, which only works if the plugin supplies it.

`client_ts` is passed through verbatim, never synthesised or interpolated. A
message whose source entry has no timestamp omits the field rather than guessing.

### 7.4 Envelope

```jsonc
{
  "capture_version": "3.0.0",
  "agent":   { "name": "claude-code", "version": "2.0.14" },
  "session": { "session_id": "…", "started_at": "…", "ended_at": "…",
               "cwds": ["C:/Dev/ws", "C:/Dev/ws/backend"] },
  "binding": { "workspace_root": "C:/Dev/ws",
               "git_remote": "…", "git_branch": "main" },
  "chunk":   { "index": 0, "total": 2, "prefix_hash": "…", "chunk_hash": "…" },
  "redaction": { "rules_version": "1", "redacted_count": 12 },
  "truncated_paths": ["data/huge.csv"],
  "messages": [ /* each carrying client_ts — §7.3 */ ]
}
```

---

## 8. Redaction

Claude Code transcripts carry the content of every `Read` and the stdout of every
`Bash` — `.env` bodies, connection strings, tokens in tool arguments. The existing
plugin redacts nothing but images, and does that incorrectly.

### 8.0 Shared module — sync discipline

The rule set is implemented once. Canonical source of truth is
`cadra-prototype/proxy/redact.py` (companion §8.1). Copies:

| Location | Role |
|---|---|
| `cadra-prototype/proxy/redact.py` | Canonical |
| `cadra-prototype/backend/services/redact.py` | Byte-identical twin (in-repo parity test) |
| This plugin: `scripts/cadra/redact.py` | Vendored **rule engine** plus plugin-only `redact_messages` |

Edit order on every rules change: proxy → backend → plugin, then bump
`rules_version` in all three. The plugin does **not** invent a parallel regex
table.

**The plugin copy is deliberately not byte-identical**, so a whole-file hash
would report drift forever. Only the rule engine is shared — `RULES_VERSION`,
the pattern tables, `_shannon_bits`, `_is_safe_shape`, `redact_text`. The
plugin's `redact_messages` differs by design: it walks `tool_calls` and returns
the redacted **write paths** for §8.1, which the server has no use for.

So the shared region is delimited in all three files by literal sentinel lines:

```python
# --- BEGIN SHARED REDACTION RULES (see byo-trace-capture-design §8.0) ---
...
# --- END SHARED REDACTION RULES ---
```

The release check hashes **only the bytes between the sentinels** and compares
across the three files. Anything outside them is each file's own. A file
missing its sentinels fails the check rather than being skipped. Envelope field
`redaction.rules_version` must match the module constant.

**Rules, versioned as `rules_version`:**

| Class | Action |
|---|---|
| `.env`-family files (`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`) — **read, written, or named in a shell command** | Drop the body entirely, keep the path |
| Token shapes (`sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, `xox[baprs]-…`, JWT triples) | Replace with `[redacted:<class>]` |
| Assignments matching `(?i)(api[_-]?key|secret|token|password|passwd|credential)\s*[=:]\s*\S+` | Redact the value, keep the key |
| High-entropy strings: ≥32 chars, single token (no whitespace), drawn from `[A-Za-z0-9+/=_-]`, Shannon entropy ≥ 4.0 bits/char, and **not** matching a known-safe shape (git SHA, UUID, content hash, data-URI payload, file path) | Replace with `[redacted:entropy]` |
| Images | `[image removed before upload]` — **including inside `tool_result` parts** |

Redaction runs **after** transformation and **before** chunking, so
`last-preview.json` is byte-identical to what is sent.

Every redaction increments `redacted_count`, surfaced in the pre-send summary. The
candidate sees "12 secrets redacted" and can inspect `last-preview.json`.

**Why the first rule matches on identity, not content.** Every other rule is a
pattern matcher, and a pattern matcher catches only what it recognises. Measured
against a realistic `.env`, six of seven lines survived the other four rules:
`SMTP_PASS=` is missed because the assignment keyword list has `password` and
`passwd` but not `pass`, and `postgres://app:pw@host` and `redis://:pw@host` are
missed because the secret sits inside a URL rather than after the `=`. Extending
the keyword list is an arms race against a filename that already tells you the
answer. So these files are dropped whole, unexamined.

Three deliberate details:

- **Writes count, not only reads.** Writing a secrets file puts its contents in
  the transcript exactly as reading one does. Because a dropped write body
  changes the lines attestation hashes, that path is reported as truncated
  (§8.1) — the file leaves the coverage denominator. A read costs nothing, since
  tool results are never hashed.
- **Shell commands count.** `cat .env` has no path argument and is at least as
  common as a `Read`. Matched on a filename token in the command text, which is
  coarse on purpose: a false positive costs one tool result, a false negative
  costs a live credential.
- **Templates are kept.** `.env.example`, `.env.sample`, `.env.template`,
  `.env.dist` hold placeholders. Reading one is orientation work worth crediting,
  and dropping it would be pure loss.

**Known limit, stated rather than papered over:** pattern-based redaction is
best-effort. A secret in an unrecognised format, in a file with an unremarkable
name, survives. This is why the proxy re-scans (D6), and why the candidate
preview exists.

### 8.1 Redaction vs attestation — a real conflict

Attestation works by hashing the lines the agent wrote and matching them against
the lines in the submitted repo. **Redacting content inside a `tool_use`
argument changes those lines, so they stop matching** — the file silently loses
coverage and can drag `agent_share` toward the `low_agent_share` threshold. The
candidate is then penalised for our redaction.

Rules, by location:

| Location | Redaction | Rationale |
|---|---|---|
| `tool_result` bodies | Full rules | Read output and command stdout; never hashed against the repo |
| user / assistant text | Full rules | Pasted secrets; not attestation input |
| `tool_use` write arguments (`content`, `new_string`, `edits[].new_string`) | Full rules, **and** the target path is added to `truncated_paths` | Redaction is non-negotiable, so the file is excluded from coverage instead of scoring as unattested |

Reusing `truncated_paths` is deliberate — `coding_assessment_truncated_paths` and
the `attest_truncated` bucket already exist for exactly this situation, so a
redacted file is excluded from the coverage denominator rather than counted
against the candidate.

The entropy rule is the main false-positive risk here (base64 fixtures, minified
assets), which is a further reason redaction must never silently degrade a score.

---

## 9. Size management

### 9.1 Measured

Two real projects, transcripts including subagents:

| | RDI | Aurora |
|---|---|---|
| On-disk JSONL (what today's plugin uploads) | 50.1 MB | 55.9 MB |
| Canonical envelope, no controls | ~22.2 MB | ~21.0 MB |
| Canonical envelope, controls applied | **6.34 MB** | **13.04 MB** |
| Reduction vs on-disk | 87% | 77% |
| Largest single session | 6.34 MB | 6.83 MB |

**Per-control contribution:**

| Control | RDI | Aurora | Assessment |
|---|---|---|---|
| Images → placeholder | 13.7 MB | 1.0 MB | Dominant, highly variable |
| Drop thinking blocks | 2.14 MB | 6.86 MB | Dominant |
| Tool-result 64 KB cap | 0.03 MB | 0 MB | Insurance only |
| Write 2000-line truncation | 0 MB | 0 MB | Insurance only |

### 9.2 What the numbers mean

**Most of the reduction is the transformation itself**, not any policy — dropping
non-conversation entry types and per-entry metadata takes 50 MB to 22 MB before a
single control applies.

**Thinking blocks must be dropped whole, including `signature`.** The reasoning
text measures **0.00 MB** — Claude Code does not persist it. The entire cost is
the `signature` blob, roughly 2 KB per block: 2.14 MB over 1,023 blocks in RDI and
6.86 MB over 1,698 in Aurora. An adapter that kept `signature` "for provenance"
would ship 6.9 MB of useless padding.

**Images are the largest and least predictable control.** RDI carried 89 base64
images inside `tool_result` parts — 13.7 MB, 68% of that session. The current
`strip_images()` walks `message.content` only, so every one of them would have
been uploaded. Aurora had 4.

**The two caps never fired on real data.** Zero writes exceeded 2000 lines; three
tool results exceeded 64 KB. They stay as bounded-cost guards against a
pathological session, and the spec does not claim savings for them. The
"2000-line truncation is lossless" property is therefore **asserted, not
demonstrated** — it aligns with `MAX_LINES_PER_EVENT`, the extractor's own hashing
cap, but no observed session exercised it.

Extraction equivalence *was* demonstrated for the controls that fired: attestation
output was identical with and without them across all five sessions tested
(RDI 75 items / 6,554 hashes unchanged; Aurora 82, 44 and 1 items unchanged).

### 9.3 Chunking

4 MB per chunk, one submission per session. The largest real session needs two
chunks. Each chunk carries `prefix_hash` — the hash of all preceding chunks — so
the server can reject reordered or altered sequences.

An interrupted upload is retried whole. At this size resumable multipart is not
justified; the state cursor makes a retry cheap.

---

## 10. Error handling

| Condition | Behaviour |
|---|---|
| No `config.json` for the cwd | Point at `cadra-connect`; do not guess a workspace |
| Token expired / invalid (401) | Report plainly; suggest re-connect with a fresh token |
| Session revoked (403) | Report the server's message; do not retry |
| Binding mismatch (409) | Report which remote the server expected; do not silently rebind |
| Network failure mid-chunk | Keep `state.json` unchanged; the session is retried whole next run |
| Transcript unreadable / malformed line | Skip the line, count it, report the count. Never abort a submission for one bad line |
| Nothing new to submit | Say so explicitly — silence reads as failure |

The plugin never reports success it has not been told about by the server.

---

## 11. Testing

No test infrastructure exists in this repo today; it is added with the rewrite.

- **Fixtures:** small synthetic transcript trees under `tests/fixtures/`, covering
  a main session, a subagent pair, absolute paths, tab-numbered reads, an embedded
  image, and a secret-bearing `.env` read.
- **Scoping predicate (§6.1):** origin cwd equal to the workspace included; origin
  cwd in a subdirectory included; origin cwd in a sibling directory whose *encoded
  name shares the prefix* (`C:/Dev/ws-other`) excluded; worktree cwd included;
  a session that starts in the workspace and later moves outside it is included
  whole, with every cwd listed in `session.cwds`; a session that starts outside
  and later enters the workspace is excluded, and its project folder is never
  opened at all (§6.1 — the reporting promise was dropped deliberately).
- **Enumeration (§6.1.1):** only prefix-matching directories are opened — assert
  by instrumenting file opens that an unrelated project directory is never
  touched; a subagent file is attributed to its parent session and never emitted
  as a session; `tool-results/` is not read; a file whose first 50 lines carry no
  `cwd` is reported rather than silently dropped.
- **Transformation:** each row of §7's table; path rebasing inside vs outside the
  workspace; subagent interleaving order and tagging.
- **Extraction parity — the load-bearing test:** feed the transformed envelope to
  the real `attestation_items_from_payload` from `cadra-prototype` and assert a
  non-zero, expected attestation count. This is what would have caught the
  absolute-path defect.
- **Redaction:** each rule class; assert `last-preview.json` is byte-identical to
  the submitted payload.
- **Timestamps:** every emitted message carries `client_ts` matching its source
  entry verbatim; an entry without a timestamp omits the field rather than
  substituting a value. This pairs with the server-side velocity guard in the
  companion spec §14 — together they are what stop BYO candidates being
  mass-flagged for pasting.
- **Filesystem invariant:** run a submission against a temp workspace and assert
  no write occurred outside `.cadra/` and `.gitignore`.
- **Chunking:** boundary and hash-chain correctness.

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| Claude Code's transcript format is not a stable public contract, and already moved subagents out of the main file | `agent.version` and `capture_version` travel in every envelope; the server tolerates unknown keys; the extraction-parity test fails loudly on a format change |
| Client-side redaction is defeatable by editing the plugin | Proxy re-scans (D6); this is accepted as tamper-*evident*, not tamper-proof |
| A candidate can point `workspace_root` at unrelated work | Server-side binding check; and attestations only score against the submitted repo, so foreign material cannot inflate a score |
| A candidate never invokes `cadra-submit` | **Resolved** — submission is an explicit dashboard deliverable (§13.1). Non-submission is a visible outcome, like an unpushed repo |
| Envelope omits `client_ts`, collapsing the server's velocity clock | Mandatory field (§7.3) with a dedicated test; the server-side guard test in the companion spec §14 fails loudly if it is missing |
| `tool-results/` offload hides write bodies | **Resolved — not a risk for attestation.** Spike (`docs/spike/tool_results_offload_spike.py`, 2026-08-12): 489 JSONL files, 3,421 Write/Edit-family calls, **0** missing bodies; 18 `<persisted-output>` stubs were Bash (13) / Grep (4) / PowerShell (1) only. Collector does **not** read `tool-results/` for attestation. Stubs stay as placeholder text (size control already caps tool results). Re-check if Claude Code starts offloading `tool_use` inputs. |

---

## 13. Resolved questions

### 13.1 A candidate who never submits

**Decision (2026-08-12): trace submission is an explicit deliverable** on the
assessment dashboard checklist, alongside the repo URL and the deployed endpoint.

No nag mechanism and no background capture. A candidate who does not submit has
no trace, exactly as a candidate who does not push has no repo — a visible,
attributable outcome rather than a silent one. This costs nothing to build,
matches how the other two deliverables already work, and keeps D1 intact: capture
remains an explicit act.

`candidate-onboarding.md` gains the corresponding checklist line.

### 13.2 Multiple machines

**Decision: supported, no special handling.** A candidate connects on each machine
and submits from each. Claude Code `sessionId`s are globally unique, so sessions
from different machines cannot collide, and each lands as its own row under the
same assessment. `workspace_root` may legitimately differ between machines while
`git_remote` stays constant, which the server's binding rule already permits
(companion §7, rule 5).

Covered by a test in the companion spec's §14 rather than by design machinery.

---

## 14. Sequencing

1. Config, filesystem contract, `cadra-connect` — establishes the ingest contract
   end to end against the companion spec's route.
2. Collection and scoping (§6), with the parity test from §11.
3. Transformation (§7).
4. Redaction (§8) and the preview surface.
5. Size controls and chunking (§9).
6. `cadra-submit` and `cadra-traces` wiring, error handling (§10).
7. Delete the superseded hooks, PowerShell scripts and Supabase code (§3).

Step 7 is last deliberately: the old path stays intact until the new one is
proven, so there is no window in which neither works.
