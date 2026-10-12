---
name: cadra-traces
description: Show which of the user's work sessions Cadra has stored. Use when the user says "show my traces", "what have I submitted", "is my work saved", or "list my Cadra sessions".
---
<!-- plugins/cadra-trace-tracker/skills/cadra-traces/SKILL.md -->

# Skill: cadra-traces
**Plugin:** cadra-trace-tracker

## Purpose

Read-only view of what the server actually holds. Never uploads anything.

## Plugin root

`<PLUGIN_ROOT>` is the absolute path two directories above this SKILL.md (the
plugin folder). Hosts that set `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT` fill it in
for you; otherwise substitute it. If this SKILL.md was reached through a
symlink (OpenCode installs), resolve it first with `realpath`.

The commands below say `python3`. Use `python3` if it exists, else `python`, else
`py -3` (typical on Windows). The script paths and arguments stay the same.

## Steps

1. Run:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-<PLUGIN_ROOT>}}/scripts/cadra_traces.py" --workspace "<WORKSPACE>"
   ```

2. Render one compact line per stored session — date, message count, size. Keep it
   a status glance, not a report. Never show transcript content.

   Describe each session by its **span and size** — when it began, when it ended,
   how many messages — never by its opening message. A long session that starts
   with "hi" is not a session about "hi", and a candidate shown only that will
   reasonably conclude their work was lost. This wording is carried over from the
   plugin it replaces, where it was there for exactly that reason.
3. If the output says `UNAVAILABLE`, say the server could not be reached and do
   not guess from local files.
4. If nothing is stored, say so and suggest "submit my trace".

## Rules

- Read-only. Never trigger a submission from this skill — point at cadra-submit.
- Show only what the command returned; never invent entries.
- Never print the token.
