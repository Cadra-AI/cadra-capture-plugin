"""Shared canonical-message helpers: path rebasing and turn counting."""
from pathlib import Path

from cadra import adapt

WS = Path("C:/Dev/ws")


def test_dot_segments_are_collapsed_before_rebasing():
    """`src/../a.py` reaching the server unresolved matches no repo file, so the
    write silently scores as unattested."""
    assert adapt.rebase_path("C:/Dev/ws/src/../a.py", WS) == "a.py"
    assert adapt.rebase_path("C:/Dev/ws/./src/a.py", WS) == "src/a.py"


# ── turn counting: what a candidate recognises as "a message" ────────────────
# The raw count mixes their prompts with the agent's own steps — tool calls,
# tool results, assistant replies. A session the candidate experienced as 8
# exchanges reported "89 messages", which reads like the plugin is hoovering up
# far more than it is. Turns are the unit they can check against their own
# memory of the session; the full count stays available for the
# sent-vs-confirmed integrity comparison.

def test_turns_counts_only_what_the_candidate_said():
    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "ok", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "done"},
        {"role": "assistant", "content": "finished"},
        {"role": "user", "content": "now the other thing"},
    ]
    assert adapt.count_turns(messages) == 2


def test_injected_content_is_not_a_turn():
    """The isMeta re-role puts it in system, so this holds by construction —
    pinned because it is the reason the count was wrong in the first place."""
    assert adapt.count_turns([
        {"role": "system", "content": "Base directory for this skill: ..."},
        {"role": "user", "content": "real prompt"},
    ]) == 1


def test_turns_of_an_empty_or_agent_only_session_is_zero():
    assert adapt.count_turns([]) == 0
    assert adapt.count_turns([{"role": "assistant", "content": "unprompted"}]) == 0
