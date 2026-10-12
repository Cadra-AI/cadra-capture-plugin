# Cadra integration assessment — gap register & G5 spike findings

**Status:** working document. Written 2026-08-11 against plugin v2.2.3
(`Cadra-Claude-Code-Plugin@main`) and `cadra-prototype` branch `016-f5-sessions-budgets`.

**Purpose.** This plugin is being repurposed from a standalone "program tracker"
into the **bring-your-own-environment (BYO) trace submission path** for Cadra
coding assessments — the route for candidates who work in their own environment
rather than the managed OpenCode + proxy flow. This document records (a) the gaps
between what the plugin does today and what the Cadra assessment construct
requires, and (b) the results of the G5 spike, which tested whether Cadra's
existing F6 line-attestation math can run on Claude Code transcripts at all.

References to `cadra-prototype` files use repo-relative paths.

---

## 1. Target architecture (agreed)

The plugin stops talking to Supabase entirely. Submission goes through the
**existing Cadra proxy**, which already owns token validation and trace writes:

```
Assessment created  →  per-assessment JWT minted
        │                (backend/services/coding_assessment_jwt.py — unchanged)
        ▼
Candidate pastes the token into the workspace marker file
        │
        ▼
Candidate invokes the submit SKILL (explicit act — no hooks)
        │   collects scoped session data, redacts, builds envelope
        ▼
POST → Cadra proxy ingest route
        │   Depends(verify_jwt)  (proxy/auth.py:34 — unchanged)
        │   assessment_id read FROM CLAIMS, never from the request body
        ▼
Proxy writes the existing F6 ledgers + returns a server receipt
```

Two consequences that shape everything below:

- **Candidates hold no database credential.** They hold a scoped, expiring,
  per-assessment token — exactly as in the OpenCode flow. The service key stays
  server-side.
- **`coding_assessment_id` is derived from the verified token**, so identity is
  cryptographic rather than self-asserted, and every row joins to the existing
  assessment tables for free.

### Design decisions already locked

| Decision | Rationale |
|---|---|
| **No hooks.** Submission is a candidate-invoked skill only. | Consent must be an explicit act, not a background upload from a personal machine. |
| **Ingest via proxy, not Supabase.** | Reuses `verify_jwt`; keeps credentials server-side; single write path for both OpenCode and BYO traces. |
| **Group B (scope + data model + redaction) lands with Group A.** | The ingest contract cannot be defined without knowing exactly what is captured and what is stripped. |
| **One token — the existing `f1-coding` assessment JWT**, with its lifetime extended to cover the submission grace. | A submission-only `f1-trace` type was specced and then rejected (2026-08-12): fmcg-family problems require the candidate's *deployed app* to call an LLM through the proxy, so BYO candidates need model access anyway, and a second token would only double the secrets to manage and leak. Token lifetime is not the control — `session_state.check()` enforces the window and prompt cap server-side, so a longer `exp` grants no extra chat access. |
| **Submission survives a finished session.** | `active` and `exhausted` always accepted; `expired` accepted within a grace window; `revoked` refused. Refusing a late submission destroys evidence of work already done. |

### Open questions

- **No-hooks trade-off:** a candidate who never invokes the skill submits
  nothing. Accepted as a visible outcome, or does a nag surface exist?
- Whether Node-C-style "missing submission" is a graded state or a park state.
- Whether BYO scores are directly comparable to managed-flow scores, or carry a
  provenance caveat (drives how much of G7 must be built).

---

## 2. What the Cadra construct requires

Everything in the assessment path is keyed on `coding_assessment_id` (UUID),
established by the HS256 JWT the Setup page mints and the proxy verifies. The
proxy writes six ledgers under that key:

| Table | Written by | Feeds |
|---|---|---|
| `coding_assessment_message_trace` (hash-deduped, `seq`, `role`, `channel`) | `proxy/capture.py:117` | paste / velocity / outsourced-prompt evidence |
| `coding_assessment_line_attestations` (`path`, `line_hash`, `source`) | `record_line_attestations` RPC | `agent_share`, `low_agent_share`, `unverified_authorship` |
| `coding_assessment_path_events` (read/write, `unattested_lines`) | F6 batch 2 | `foreign_code_rewritten` |
| `coding_assessment_truncated_paths` | F6 | coverage math |
| `coding_assessment_session_transcripts` (per `session_id`) | F6 §6.2 | grading transcript |
| `coding_assessment_proxy_requests` + `session_state` / `usage_requests` | `proxy/main.py` | 48h window, 40-prompt cap, model forcing |

The plugin today produces **one flat row** — `{roll_no, project_name,
session_id, title, turn_count, transcript blob}` — in a **different Supabase
project** (`pyrpzlppjmejlohiqoyc`). Nothing joins to any of the above.

---

## 3. Gap register

Status legend: **OPEN** · **SPIKED** (investigated, findings below) · **DECIDED**
(approach agreed, not built).

### Group A — Identity & credentials

#### G1 — Identity is self-asserted, not bound to an assessment · DECIDED
Identity is a **5-digit integer** typed into `.claude-project`. No token, no
signature. `roll_no` + `project_name` (literally the workspace folder name) are
the only keys on the row; neither joins to `coding_assessments`. No `run_id`
linkage, and no way to tell whether a session fell inside the 48h window.

**Fix:** `.claude-project` holds the per-assessment JWT. Submission sends it as
`Authorization: Bearer`. The proxy's `verify_jwt` validates it and derives
`assessment_id` from claims. Claude Code's own `sessionId` maps onto
`coding_assessment_session_transcripts.session_id`.

#### G2 — Supabase credential shipped to every candidate machine, and published · DECIDED
`SUPABASE_URL` + an anon JWT are hardcoded at `submit_traces.py:28-29` and
`submit-traces.ps1:22-23`, in a **public GitHub repo** — the clone used for this
assessment required no authentication. The key decodes to `role: anon`,
`exp ≈ 2035`. Consequences:

- attack surface is whatever the anon role reaches via RLS/RPC on that project,
  exposed to the whole internet rather than to candidates;
- not revocable per candidate; not rotatable without reshipping the plugin;
- `tracker_my_sessions_staging` is called with only `p_code` + the shared key.
  **Unverified:** if there is no server-side check, a 90,000-key brute force
  enumerates every candidate's session metadata, titles included. This needs
  confirming against the RPC definition.

**Fix:** candidates receive **zero** database credentials — the proxy ingest
route replaces direct Supabase access entirely. Interim, regardless of this
project: audit the anon role's grants, confirm whether `p_code` is auth-gated,
rotate the key.

**Also:** trace data currently lands in a Supabase project separate from the
grading database. Ingest must target the Cadra project.

### Group B — Scope, capture model, redaction

#### G3 — Captures far beyond the candidate's assessment session · OPEN
Three independent over-captures:

1. `submit_traces.py:222-240` sweeps **all** of `~/.claude/projects` and includes
   any transcript that merely *contains the workspace path as a substring*. A
   personal session that mentions the folder path is uploaded in full. Same
   fallback at `submit-traces.ps1:142`.
2. `trace_hook.py:29-44` walks **6 ancestors and one level down**, so a marker in
   the assessment folder also traces sessions started from the parent.
3. The PowerShell path additionally scans
   `%APPDATA%`/`%LOCALAPPDATA%\Claude\local-agent-mode-sessions` — Claude
   *desktop* sessions, not Claude Code.

**Fix:** match on the encoded-cwd directory only (session cwd == workspace or a
descendant); delete the substring fallback and the desktop roots; filter to
sessions starting inside the assessment window. **See §4.6 — the spike changed
what "the session's files" means.**

#### G4 — No secret redaction · OPEN · *cross-cutting, see §5*
Claude Code transcripts carry the content of every `Read` and the stdout of every
`Bash` — `.env` bodies, connection strings, keys in tool arguments. The only
redaction is images, and `strip_images()` (`submit_traces.py:80-87`) walks
`message.content` only, so images inside `tool_result` blocks survive. Even the
one stated guarantee does not hold.

**Fix:** client-side redaction before the envelope is written (`sk-`, `ghp_`,
`AKIA`, JWT shapes, `*_KEY|SECRET|TOKEN|PASSWORD=`, high-entropy strings, drop
`.env`-path read results wholesale); a server-side second pass at ingest; a
candidate-visible preview of exactly what will be sent.

### Group C — Grading signal

#### G5 — Line attestation extraction · SPIKED — see §4
Without extraction, `attestations` is empty for every BYO candidate →
`no_attest_activity` → rollup **`unknown`**, `agent_share` `None`. Provenance
becomes unscoreable. **The spike proves extraction works, and that a naive
implementation is actively harmful.**

#### G6 — No path events · OPEN
`foreign_code_rewritten` can never fire. Derive read/write + `unattested_lines`
into `coding_assessment_path_events` in the same ingest pass. The spike's
line-number fix (§4.5) is a prerequisite — without it, zero read events are
produced.

#### G7 — Session window and prompt budget unenforceable · OPEN
No proxy in the model loop means no `coding_assessment_proxy_requests`, no
`session_state`, no `usage_requests`. The 48h window and 40-prompt cap do not
exist for BYO; candidates run unbounded on their own key. Also `turn_count` is a
raw message-id count that includes subagent traffic.

**Decision needed:** derive a comparable turn/token metric from the transcript
and enforce post-hoc, or accept unbounded and mark it on the report (G11). A BYO
score is not comparable to a capped OpenCode score unless one of these happens.

#### G8 — No `config_tampered` analogue · CLOSED — out of scope (2026-08-12)
There is no `opencode.json` to diff. Real equivalents exist and are currently
invisible: `.claude/settings.json` (hook and permission changes), a
candidate-authored `CLAUDE.md`, edits to the plugin itself. The proposed fix was
to hash and submit these alongside the transcript and diff against the shipped
baseline.

**Decided against, for three reasons.**

*The hash is self-reported by the thing being checked.* The plugin computes it,
and edits to the plugin are one of the three things G8 wants to detect. A
candidate who changes `settings.json` can change the hasher in the same sitting.
That makes it evidence against carelessness only — the same tier as the binding
check (§7 of the ingest design), but without binding's redeeming property of
catching a common honest mistake.

*It cannot protect the score.* `.claude` is already in `_IGNORED_DIR_PARTS` and
`claude.md` / `agents.md` / `opencode.json` in `_IGNORED_FILENAMES`
(`proxy/line_attest.py:25-34`), so none of these files enters the coverage math on
either path. A configuration change has no route to inflate `agent_share`.

*It argues against the stated non-goal.* BYO is deliberately unbounded — no prompt
cap, no time window, no forced model — and the report marks BYO provenance rather
than pretending the constraints applied (design §1). A config-tamper flag is a
constraint check reintroduced by the back door, and a weak one.

**What was separated out rather than dropped.** `CLAUDE.md` is interesting as
*positive* evidence: writing good agent instructions is a real AI-engineering
skill, and on the BYO path it is the candidate's most direct statement of how they
wanted the agent to behave. That is a grading feature needing its own rubric line,
not a tamper hash, and it is not tracked here.

Reopen if the threat model changes — specifically, if BYO ever becomes a *capped*
path, at which point knowing the configuration was altered starts to matter.

### Group D — Channel integrity

#### G9 — Tamper posture is effectively zero · OPEN
The BYO threat model inverts the proxy's: the candidate owns the entire pipeline.

- Transcripts are plain JSONL on their disk; edit before submit and the doctored
  version is what uploads. Nothing is signed, chained, or timestamped by anything
  they do not control.
- `_traces/state.json` is candidate-writable, and the RPC upserts on session
  *growth* — so a larger doctored transcript wins.
- `plugin_hash: "harvester-py"` is a hardcoded literal, not a hash. The manifest
  claims an integrity property it does not implement.
- The anti-tamper enforcement is **prompt text injected into a model the
  candidate controls**, from a config file they can edit. That is not a control.

**Fix — target tamper-*evident*, not tamper-proof:** server-stamped monotonic
`seq`; append-only hash-chained ingest that **rejects** a submission whose prefix
hashes disagree with stored state; flag breaks in Claude Code's own `parentUuid`
chain; cross-check against git commit timestamps and GitHub commit logins
(`classify_commits`, `integrity.py:117`, already does this).

#### G11 — Report does not distinguish provenance grade · OPEN
Add a `byo_environment` marker feeding the F6 rollup and the hiring report, so a
BYO score is not read as equivalent to a proxy-captured one.

### Group E — Consent

#### G10 — Consent is a model-spoken sentence · DECIDED
Disclosure depends on the SessionStart context being honoured by the model.

**Fix:** consent captured and logged on the dashboard at assessment start, with
the G4 preview shown there. The plugin ships **skills only** — no `hooks.json`.
With skill-only submission the hook layer disappears entirely: the `SessionEnd`
and `Stop` upload hooks are unnecessary, the `Stop` snapshot hook is redundant
(Claude Code already retains transcripts under `~/.claude/projects`), and
`SessionStart` context injection is replaced by dashboard consent.

### Group F — Engineering

#### G12 — Ingest will not scale · OPEN, upgraded by the spike
No size cap, no chunking; a whole transcript is POSTed in one body. The spike
measured **~49 MB of transcript for a single project** once subagent files are
included (§4.3). Chunked, resumable submission with explicit caps is mandatory,
not a nicety. Same failure class as the PostgREST 1000-row cap fixed at
`ddcfcc5`.

#### G13 — Platform defects · OPEN
- `submit-traces.ps1` has **no verification step at all**, yet the `save-trace`
  skill instructs the model to report `VERIFIED n/m` — Windows candidates get an
  unfulfillable instruction.
- `submit-traces.ps1:148` builds `$entries` with `+=` in a loop (O(n²)) and then
  `ConvertTo-Json -Depth 100` over the whole transcript; long sessions hang or
  exhaust memory on PowerShell 5.1.
- `hooks.json` chains `python3 … || powershell …`; a Windows Store stub `python3`
  exits 0 silently, so the fallback never runs and capture fails silently.
- `title` is taken from the first user message, which in Claude Code is usually
  system-reminder or CLAUDE.md injection — titles are junk.
- `submit-traces.ps1:243` exits on `ScanOnly`/`List` before removing the lock
  file, blocking uploads for two minutes.

**Fix:** collapse to a single implementation. The dual Python/PowerShell split is
the root cause of roughly half of these.

---

## 4. G5 spike — can F6 attestation run on Claude Code transcripts?

**Method.** A Claude Code JSONL → OpenAI-envelope adapter feeding the *real*
`services/line_attest.py` and `integrity.build_integrity_summary()` — no
reimplementation — then compared against the final on-disk repo. Two projects
with substantial agent-authored code were used as assessment analogues. Script:
`docs/spike/g5_line_attestation_spike.py`.

**Verdict: viable.** Cadra's attestation math runs unmodified on Claude Code
transcripts — but only behind an adapter that corrects three format mismatches.
Without them the pipeline yields effectively zero, and **flags the candidate**.

### 4.1 Results

| Stage | RDI | Aurora |
|---|---|---|
| Naive adapt | 1 item · share **0.000** · `caution` | — |
| + path rebase + line-number fix | 50 items · 5,013 hashes · 0.169 | 86 items · 14,210 · 0.082 |
| + subagent transcripts | 75 items · 6,554 hashes · **0.414** · **`ok`** | 127 items · 18,534 · 0.107 |
| Restricted to attested files | **0.651** | **0.737** |

### 4.2 Claude Code transcript shape (measured)

Entry types observed: `user`, `assistant`, `system`, `attachment`,
`file-history-snapshot`, `file-history-delta`, `last-prompt`, `mode`,
`permission-mode`, `ai-title`, `queue-operation`, `agent-name`.

Content blocks: `thinking`, `text`, `tool_use`, `tool_result`. Tool inputs use
`file_path` / `new_string` / `old_string` / `content` / `command` — key names
that already match F6's `_PATH_KEYS`, `_NEW_STRING_KEYS`, `_CONTENT_KEYS`. Tool
names are capitalised (`Write`, `Edit`, `Bash`, `Read`) and F6 lowercases before
matching, so the tool-name sets need no change.

### 4.3 Finding 1 — absolute paths silently void every attestation (fatal)

Claude Code emits **absolute** tool paths (`C:\Dev\RDI\x.py`). F6's
`_GLOBAL_PATH_RE` (`line_attest.py:192`) deliberately discards drive-letter
paths, because under OpenCode they can never map to a submitted repo file.

Result: all 50 `Write` and 58 `Edit` calls were dropped. One attestation item
survived, `agent_share` `0.0`, flags `low_agent_share` + `unverified_authorship`,
rollup `caution`.

> **A candidate whose agent demonstrably wrote the entire repo would be
> automatically flagged for cheating.**

**Fix:** rebase tool paths to workspace-relative at ingest — 137 rebases in RDI,
1,017 in Aurora. The workspace root must therefore be part of the capture
envelope (see §6).

### 4.4 Finding 2 — subagent work lives outside the main transcript (structural)

452 `Agent` tool calls across the local projects surveyed, and **zero**
`isSidechain` entries in any main transcript. Subagent work is stored separately:

```
~/.claude/projects/<encoded-cwd>/<session-id>/subagents/agent-<id>.jsonl
                                              agent-<id>.meta.json
```

`meta.json` carries `toolUseId`, `agentType`, `description`, `spawnDepth`, `model`
— a clean linkage back to the parent `Agent` call. For the RDI session that is
**38.6 MB of subagent transcript against 11 MB of main session**.

Including them: `Write` 50 → 114, `Edit` 58 → 144, `agent_share` 0.169 → 0.414,
and the rollup flips **`caution` → `ok`**.

> Omitting subagent transcripts wrongly flags the candidate. This also revises an
> earlier assumption in this analysis: Claude Code `tool_use` blocks are richer
> than the proxy's view, but the **main transcript alone is substantially
> poorer** — most agent file writes are not in it.

There is also a sibling `tool-results/` directory holding offloaded large tool
results (a 43 KB `.txt` in RDI). **Resolved (2026-08-12 spike
`docs/spike/tool_results_offload_spike.py`):** offload is `<persisted-output>` for
oversized **tool results** only (Bash / Grep / PowerShell). Across 489 JSONL
files and 3,421 Write/Edit-family calls, every write body remained in the
transcript — **0** Write/Edit offloads. The BYO collector does not need to read
`tool-results/` for attestation; re-check if Claude Code later offloads
`tool_use` inputs.

### 4.5 Finding 3 — tab-numbered read results defeat read matching

`_strip_line_numbers` (`line_attest.py:335`) recognises `|`, `:` and `→` as
line-number separators. Claude Code uses `<n>\t`. Unstripped read text never
hash-matches file content, so **zero** read events are produced. After the fix:
30 (RDI) and 133 (Aurora) read events — the input G6 and
`foreign_code_rewritten` require.

### 4.6 Interpreting the two `agent_share` figures

Whole-repo share is depressed because both repos contain many files these
sessions never touched (RDI 91 `never_touched`, Aurora 936 — a large pre-existing
monorepo). A real assessment repo *is* the session's output, so the restricted
figure — **0.65–0.74** — is the honest analogue, comfortably above
`LOW_AGENT_SHARE_THRESHOLD` (0.20).

Residual imperfection, inherent rather than a defect: 11–12 files remain
`mostly_unattested` even among attested ones, because `Edit` attests only the
`new_string` fragment, so a file shaped by many small edits accumulates partial
coverage. A further 12–18 attested paths no longer exist, reflecting mid-project
file moves; F6's `_RENAME_MATCH_MIN` union matching absorbs part of this.

### 4.7 Consequences for other gaps

- **G3** gains a hard requirement: the submit skill must collect
  `<session-id>/subagents/*.jsonl` and their `.meta.json`. The current plugin's
  `rglob("*.jsonl")` would sweep subagent files up as **separate anonymous
  sessions with wrong IDs** — worse than skipping them.
- **G12** is upgraded from "nicety" to mandatory: ~49 MB for one project.
- Ingest must record the **workspace root** so paths can be rebased server-side.

---

## 5. Cross-cutting finding — redaction is not BYO-specific

The G4 redaction gap applies to the **existing OpenCode + proxy path** as well,
not only to this plugin.

`proxy/capture.py` records the full request message history via
`record_message_trace`, and that history contains whatever the agent read into
context — file contents, command output, environment values. The proxy never
touches the candidate's disk, so the *blast radius* is smaller than the plugin's,
but the class of exposure is the same: secrets that entered the agent's context
are persisted verbatim into `coding_assessment_message_trace`.

**Implication:** redaction should be built as a **shared component** applied at
both ingest points, not as a plugin-local feature. `proxy/line_attest.py` and
`backend/services/line_attest.py` already use a canonical-copy pattern (kept
byte-identical, guarded by `test_line_attest_parity.py`); a redactor should
follow the same convention.

This is worth raising as its own item against `cadra-prototype`, independent of
the BYO work.

---

## 6. Capture data model (proposed — not yet agreed)

Scoping what the skill collects and sends. Deliberately narrower than "the whole
transcript", and shaped so the proxy can write the existing F6 ledgers directly.

**Envelope, per submission:**

| Field | Source | Notes |
|---|---|---|
| `workspace_root` | marker file location | required for path rebasing (§4.3) |
| `session_id` | Claude Code `sessionId` | → `coding_assessment_session_transcripts.session_id` |
| `started_at` / `ended_at` | first/last entry timestamps | window checks (G7) |
| `client_capture_version` | plugin version | provenance |
| `chunk_index` / `chunk_total` / `prefix_hash` | chunker | G12 + G9 hash chain |

`assessment_id` is **absent by design** — the proxy derives it from the verified
token.

**Per-entry payload, after scoping and redaction:**

- assistant `tool_use` blocks (name, id, input) — the attestation source
- `tool_result` blocks paired by `tool_use_id`, line-number-normalised
- user text turns — paste/velocity/outsourced evidence
- subagent transcripts, tagged with parent `toolUseId` from `meta.json`
- **dropped:** thinking blocks, images, offloaded blobs, anything the redactor
  flags

**Written by the proxy on ingest:** `coding_assessment_session_transcripts`,
`coding_assessment_line_attestations` (via `record_line_attestations`),
`coding_assessment_path_events`, `coding_assessment_message_trace`.

Open: whether user text turns go into `message_trace` with a distinct `channel`
value (e.g. `byo_agent_chat`) to keep BYO traffic separable from proxied traffic
in the velocity and outsourced-prompt analyses.

---

## 7. Sequencing

1. **G2 + G1** — credential model and identity; the ingest contract everything
   else is built on.
2. **G3 + G4** — scope and redaction. Both are prerequisites to any real
   candidate running this, and both feed the §6 data model.
3. **G10** — remove hooks, skill-only submit, dashboard consent.
4. **G5 + G6** — server-side extraction into the existing F6 tables. *Proven
   viable (§4); `tool-results/` offload check closed (§4.4) — write bodies stay
   in the JSONL.*
5. **G9 + G11** — tamper-evidence and report provenance marking.
6. **G7**, then **G12 + G13**. (G8 closed as out of scope — see above.)
