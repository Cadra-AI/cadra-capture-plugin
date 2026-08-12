"""Client-side redaction (§8). Vendored copy of the canonical proxy module."""
from cadra.redact import RULES_VERSION, redact_messages, redact_text


def test_rules_version_is_declared():
    assert RULES_VERSION == "1"


def test_tokens_are_redacted():
    for secret in ("sk-abcdefghijklmnopqrstuvwxyz012345",
                   "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                   "AKIAIOSFODNN7EXAMPLE"):
        out, n = redact_text(f"value {secret} end")
        assert secret not in out and n == 1


def test_assignment_keeps_key_drops_value():
    out, n = redact_text("DATABASE_PASSWORD=hunter2supersecret")
    assert out.startswith("DATABASE_PASSWORD=") and "hunter2" not in out and n == 1


def test_git_sha_and_uuid_survive():
    """False positives here would damage attestation coverage."""
    for safe in ("9ee8167a1b2c3d4e5f60718293a4b5c6d7e8f900",
                 "62cda7a2-399b-462b-8b92-11472cd32750"):
        out, n = redact_text(f"id {safe}")
        assert safe in out and n == 0


def test_tool_result_bodies_are_redacted():
    messages = [{"role": "tool", "tool_call_id": "t1",
                 "content": "AWS_SECRET=abcdef123456789"}]
    out, paths, n = redact_messages(messages)
    assert n == 1 and paths == set()
    assert "abcdef123456789" not in out[0]["content"]


def test_redacted_write_content_reports_its_path():
    """Redacting a write changes the lines attestation hashes, so the file must be
    excluded from coverage rather than scored as unattested (§8.1)."""
    messages = [{"role": "assistant", "tool_calls": [{
        "id": "t1", "type": "function", "function": {
            "name": "Write", "arguments": {
                "file_path": "src/config.py",
                "content": "API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\n"}}}]}]
    out, paths, n = redact_messages(messages)
    assert n >= 1
    assert paths == {"src/config.py"}
    assert "sk-abcdefghij" not in str(out)


def test_clean_write_content_reports_no_path():
    messages = [{"role": "assistant", "tool_calls": [{
        "id": "t1", "type": "function", "function": {
            "name": "Write", "arguments": {"file_path": "src/a.py",
                                           "content": "x = 1\n"}}}]}]
    _out, paths, n = redact_messages(messages)
    assert paths == set() and n == 0


def test_shared_region_sentinels_are_present_and_well_formed():
    """§8.0's cross-repo drift check hashes the bytes between these markers.
    Without them the check has nothing to hash and would silently pass."""
    from pathlib import Path

    from cadra import redact as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    begin = "# --- BEGIN SHARED REDACTION RULES"
    end = "# --- END SHARED REDACTION RULES ---"
    assert source.count(begin) == 1 and source.count(end) == 1
    shared = source[source.index(begin):source.index(end)]
    assert "def redact_text" in shared, "the rule engine must be inside the region"
    assert "def redact_messages" not in shared, "plugin-only code must be outside"


def _jwt() -> str:
    return ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJhYmNkZWZnaGlqIiwidHlwZSI6ImYxLWNvZGluZyJ9."
            "s3cr3tsignaturevalue12345")


def _bash(command: str) -> list[dict]:
    return [{"role": "assistant", "tool_calls": [
        {"id": "t1", "type": "function",
         "function": {"name": "Bash", "arguments": {"command": command}}}]}]


def test_secrets_in_command_arguments_are_redacted():
    """The leak this rule exists for: a token on a command line is recorded in
    the transcript as a tool argument, and the transcript is what gets uploaded."""
    messages, _paths, count = redact_messages(_bash(f'connect.py --token "{_jwt()}"'))
    command = messages[0]["tool_calls"][0]["function"]["arguments"]["command"]
    assert count == 1
    assert _jwt() not in command
    assert "[redacted:token]" in command


def test_environment_secrets_in_commands_are_redacted():
    messages, _paths, count = redact_messages(
        _bash("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"))
    command = messages[0]["tool_calls"][0]["function"]["arguments"]["command"]
    assert count >= 1
    assert "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY" not in command


def test_redacting_a_command_does_not_truncate_the_file_it_names():
    """Only write content is hashed for attestation. Rewriting a command changes
    nothing that is hashed, so it must not cost the candidate a file's coverage."""
    messages = _bash(f'cat src/app.py --token "{_jwt()}"')
    messages[0]["tool_calls"][0]["function"]["arguments"]["file_path"] = "src/app.py"
    _messages, paths, count = redact_messages(messages)
    assert count == 1
    assert paths == set()


def test_path_arguments_are_left_alone():
    """A path is not a secret, and rewriting one would break scoping downstream."""
    messages = [{"role": "assistant", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "Read", "arguments": {
            "file_path": "src/aVeryLongGeneratedModuleNameThatLooksHighEntropy_x1.py"}}}]}]
    out, paths, _count = redact_messages(messages)
    args = out[0]["tool_calls"][0]["function"]["arguments"]
    assert args["file_path"].endswith("_x1.py")
    assert paths == set()


from cadra.redact import SECRET_FILE_MARKER, is_secret_file  # noqa: E402


def _read_pair(name: str, body: str, tool: str = "Read") -> list[dict]:
    return [
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": tool, "arguments": {"file_path": name}}}]},
        {"role": "tool", "tool_call_id": "c1", "content": body},
    ]


ENV_BODY = """DATABASE_URL=postgres://app:hunter2@db.internal:5432/prod
SMTP_PASS=Tr0ub4dor&3
REDIS_URL=redis://:c4ch3p455@10.0.1.7:6379"""


def test_reading_an_env_file_drops_the_whole_body():
    """None of these three lines is caught by the pattern rules: SMTP_PASS is not
    `password`, and the other two hide the secret inside a URL."""
    messages, _paths, count = redact_messages(_read_pair(".env", ENV_BODY))
    body = messages[1]["content"]
    assert count == 1
    assert body.startswith(SECRET_FILE_MARKER)
    for secret in ("hunter2", "Tr0ub4dor&3", "c4ch3p455"):
        assert secret not in body


def test_catting_an_env_file_drops_the_body_too():
    """`cat .env` has no path argument, and is at least as common as a Read."""
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "Bash", "arguments": {"command": "cat .env"}}}]},
        {"role": "tool", "tool_call_id": "c1", "content": ENV_BODY},
    ]
    messages, _paths, _count = redact_messages(messages)
    assert messages[1]["content"].startswith(SECRET_FILE_MARKER)


def test_writing_a_secrets_file_is_dropped_and_reported_as_truncated():
    """A write puts the contents in the transcript just as a read does — but it
    also changes what attestation hashes, so the path must be reported (§8.1)."""
    pair = _read_pair(".env.local", "", tool="Write")
    pair[0]["tool_calls"][0]["function"]["arguments"]["content"] = ENV_BODY
    messages, paths, _count = redact_messages(pair)
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args["content"] == SECRET_FILE_MARKER
    assert paths == {".env.local"}


def test_nested_env_file_edits_are_dropped_and_reported():
    """MultiEdit into .env must drop bodies whole — SMTP_PASS= slips past patterns."""
    body = "SMTP_PASS=Tr0ub4dor&3\nDATABASE_URL=postgres://app:hunter2@db/prod\n"
    messages = [{"role": "assistant", "tool_calls": [{
        "id": "t1", "type": "function", "function": {
            "name": "MultiEdit", "arguments": {
                "file_path": ".env",
                "edits": [{"old_string": "x", "new_string": body}],
            }}}]}]
    out, paths, count = redact_messages(messages)
    edits = out[0]["tool_calls"][0]["function"]["arguments"]["edits"]
    assert count >= 1
    assert edits[0]["new_string"] == SECRET_FILE_MARKER
    assert paths == {".env"}
    assert "SMTP_PASS=" not in edits[0]["new_string"]


def test_env_templates_are_kept():
    """Placeholders, not secrets. Reading .env.example is orientation work worth
    crediting, and dropping it would be pure loss."""
    body = "DATABASE_URL=postgres://user:password@localhost:5432/dbname"
    messages, _paths, _count = redact_messages(_read_pair(".env.example", body))
    assert SECRET_FILE_MARKER not in messages[1]["content"]


def test_ordinary_source_files_are_untouched_by_the_file_rule():
    body = "def main():\n    return 1\n"
    messages, _paths, _count = redact_messages(_read_pair("src/main.py", body))
    assert messages[1]["content"] == body


def test_which_paths_count_as_secret_files():
    for path in (".env", ".env.local", ".env.production", "C:/w/.env",
                 "certs/server.pem", "deploy.key", "~/.ssh/id_rsa",
                 "id_ed25519", "app.p12"):
        assert is_secret_file(path), path
    for path in (".env.example", ".env.sample", ".env.template", "keys.md",
                 "src/environment.ts", "monkey.py", "README.md", ""):
        assert not is_secret_file(path), path
