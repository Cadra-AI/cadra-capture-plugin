"""Size controls and chunking (§9)."""
from cadra import envelope


def _write(content: str, path: str = "src/a.py") -> dict:
    return {"role": "assistant", "tool_calls": [{
        "id": "t1", "type": "function",
        "function": {"name": "Write",
                     "arguments": {"file_path": path, "content": content}}}]}


def test_oversize_write_is_truncated_and_reported():
    body = "\n".join(f"line{i} = {i}" for i in range(envelope.MAX_WRITE_LINES + 500))
    out, truncated = envelope.apply_size_controls([_write(body)])
    content = out[0]["tool_calls"][0]["function"]["arguments"]["content"]
    assert content.count("\n") < envelope.MAX_WRITE_LINES + 10
    assert truncated == {"src/a.py"}


def test_write_within_the_cap_is_untouched():
    body = "\n".join(f"line{i} = {i}" for i in range(10))
    out, truncated = envelope.apply_size_controls([_write(body)])
    assert out[0]["tool_calls"][0]["function"]["arguments"]["content"] == body
    assert truncated == set()


def test_oversize_tool_result_is_capped_with_a_marker():
    out, _ = envelope.apply_size_controls(
        [{"role": "tool", "tool_call_id": "t1", "content": "x" * (70 * 1024)}])
    assert len(out[0]["content"].encode()) < envelope.TOOL_RESULT_CAP + 200
    assert "[truncated by cadra capture]" in out[0]["content"]


def test_single_small_session_is_one_chunk():
    chunks = envelope.build_chunks([{"role": "user", "content": "hi"}])
    assert len(chunks) == 1
    meta, _messages = chunks[0]
    assert meta == {"index": 0, "total": 1, "prefix_hash": "",
                    "chunk_hash": envelope.chunk_hash([{"role": "user",
                                                        "content": "hi"}])}


def test_large_session_splits_and_chains():
    big = [{"role": "user", "content": "x" * 200_000} for _ in range(30)]
    chunks = envelope.build_chunks(big)
    assert len(chunks) > 1
    assert chunks[0][0]["prefix_hash"] == ""
    seen: list[dict] = []
    for meta, messages in chunks:
        assert meta["prefix_hash"] == (envelope.chunk_hash(seen) if seen else "")
        seen.extend(messages)
    assert sum(len(m) for _meta, m in chunks) == len(big)


def test_a_single_message_larger_than_the_chunk_is_not_dropped():
    huge = [{"role": "user", "content": "x" * (envelope.CHUNK_BYTES + 1000)}]
    chunks = envelope.build_chunks(huge)
    assert sum(len(m) for _meta, m in chunks) == 1


def test_a_dropped_or_reordered_chunk_breaks_the_chain():
    """prefix_hash must cover every preceding chunk, not just the previous one,
    so a server replaying the sequence detects a drop or a swap (§9.3)."""
    messages = [{"role": "user", "content": f"{i}" + "x" * 200_000}
                for i in range(70)]
    chunks = envelope.build_chunks(messages)
    assert len(chunks) >= 3, "need three chunks to test a mid-sequence drop"

    def replay(sequence: list[tuple[dict, list[dict]]]) -> bool:
        seen: list[dict] = []
        for meta, group in sequence:
            if meta["prefix_hash"] != (envelope.chunk_hash(seen) if seen else ""):
                return False
            if meta["chunk_hash"] != envelope.chunk_hash(group):
                return False
            seen.extend(group)
        return True

    assert replay(chunks)
    assert not replay([chunks[0]] + chunks[2:])          # dropped chunk 1
    assert not replay([chunks[0], chunks[2], chunks[1]] + chunks[3:])  # reordered
    tampered = [(chunks[0][0], chunks[0][1][:-1])] + chunks[1:]
    assert not replay(tampered)                          # altered chunk body


def test_tool_result_cap_is_a_byte_budget_not_a_character_count():
    """A character slice of CJK text would emit ~3x the cap it enforces."""
    body = "".join("経" for _ in range(envelope.TOOL_RESULT_CAP))  # 3 bytes each
    messages = [{"role": "tool", "tool_call_id": "t1", "content": body}]
    out, _paths = envelope.apply_size_controls(messages)
    size = len(out[0]["content"].encode("utf-8"))
    assert size <= envelope.TOOL_RESULT_CAP + len(envelope.TRUNCATION_MARKER) + 2
    assert envelope.TRUNCATION_MARKER in out[0]["content"]
