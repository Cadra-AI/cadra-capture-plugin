"""cadra-connect writes config and proves the token works (§5.1)."""
import json
from pathlib import Path

import pytest

import cadra_connect
from cadra import config


def _connect(workspace: Path, token: str):
    _paste(workspace, token)
    return cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "https://proxy.test"])


def _paste(workspace: Path, token: str) -> None:
    """What the candidate does by hand: put the token in the file. It never
    reaches a command line, so it never lands in a transcript."""
    cadra_connect.main(["--workspace", str(workspace), "--init"])
    cadra_connect.token_path(workspace).write_text(token, encoding="utf-8")


def _token(exp: int = 99999999999) -> str:
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return (f"{seg({'alg': 'HS256'})}."
            f"{seg({'coding_assessment_id': 'a-1', 'exp': exp})}.sig")


def test_connect_writes_config_and_gitignore(workspace: Path, monkeypatch):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    code = _connect(workspace, _token())
    assert code == 0
    cfg = config.load(workspace)
    assert cfg["token"] == _token()
    assert cfg["proxy_base_url"] == "https://proxy.test"
    assert ".cadra/" in (workspace / ".gitignore").read_text()


def test_connect_records_the_git_remote_when_present(workspace: Path, monkeypatch):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    monkeypatch.setattr(cadra_connect, "git_remote",
                        lambda ws: "https://github.com/c/s.git")
    _connect(workspace, _token())
    assert config.load(workspace)["git_remote"] == "https://github.com/c/s.git"


def test_offline_capture_token_is_accepted(workspace: Path, monkeypatch):
    """Offline capture tokens carry capture_id, never coding_assessment_id."""
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    token = (f"{seg({'alg': 'HS256'})}."
             f"{seg({'type': 'f18-offline-capture', 'capture_id': 'c-1', 'exp': 99999999999})}"
             ".sig")
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    assert _connect(workspace, token) == 0
    assert config.load(workspace)["token"] == token


def test_expired_token_is_refused_before_anything_is_written(workspace: Path):
    code = _connect(workspace, _token(exp=1))
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_malformed_token_is_refused(workspace: Path):
    code = _connect(workspace, "nonsense")
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_server_rejection_does_not_leave_a_config(workspace: Path, monkeypatch):
    """Fail fast on day zero rather than at the deadline."""
    monkeypatch.setattr(cadra_connect, "verify_token",
                        lambda **kw: (False, "token not accepted (401)"))
    code = _connect(workspace, _token())
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_token_is_never_printed(workspace: Path, monkeypatch, capsys):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _connect(workspace, _token())
    assert _token() not in capsys.readouterr().out


def test_token_never_needs_to_reach_a_command_line(workspace: Path, monkeypatch):
    """The whole point of the file: a token passed as an argument is recorded in
    the transcript by Claude Code, and cadra-submit uploads the transcript."""
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    assert _connect(workspace, _token()) == 0
    parser_args = cadra_connect.main.__doc__  # no --token option exists at all
    with pytest.raises(SystemExit):
        cadra_connect.main(["--workspace", str(workspace), "--token", _token()])
    assert parser_args is None


def test_paste_file_is_removed_once_the_token_is_stored(workspace: Path, monkeypatch):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _connect(workspace, _token())
    assert not cadra_connect.token_path(workspace).exists()
    assert config.load(workspace)["token"] == _token()


def test_init_ignores_cadra_before_the_token_can_be_pasted(workspace: Path):
    """Ordering matters: the folder must be ignored before it can hold a token."""
    cadra_connect.main(["--workspace", str(workspace), "--init"])
    assert ".cadra/" in (workspace / ".gitignore").read_text(encoding="utf-8")
    assert cadra_connect.token_path(workspace).parent.is_dir()
    assert not cadra_connect.token_path(workspace).exists()


def test_a_plain_http_proxy_is_refused(workspace: Path, monkeypatch):
    """urllib carries the Authorization header across a redirect, so an http://
    proxy hands the token to anyone on the path."""
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token())
    code = cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "http://proxy.test"])
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_the_home_directory_is_refused_as_a_workspace(monkeypatch, tmp_path: Path):
    """Connecting at ~ would put every project folder beneath it in scope."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    code = cadra_connect.main(["--workspace", str(home), "--init"])
    assert code == 1
    assert not (home / ".cadra").exists()


def test_credentials_are_stripped_from_the_remote(workspace: Path, monkeypatch):
    """A repo cloned with an embedded PAT yields it verbatim from git, and the
    server's binding comparison discards credentials before comparing anyway."""
    from cadra import repo
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    monkeypatch.setattr(repo.subprocess, "run", lambda *a, **kw: type(
        "R", (), {"stdout": "https://u:ghp_liveTokenValue@github.com/o/r.git\n"})())
    _connect(workspace, _token())
    remote = config.load(workspace)["git_remote"]
    assert remote == "https://github.com/o/r.git"
    assert "ghp_" not in remote


def test_remote_forms_without_credentials_are_left_alone():
    for remote in ("https://github.com/o/r.git", "git@github.com:o/r.git",
                   "ssh://git@github.com/o/r.git", "/local/path/repo.git"):
        assert cadra_connect.strip_credentials(remote) == remote
    # A bare token as the username is the other form GitHub accepts.
    assert (cadra_connect.strip_credentials("https://ghp_tok@github.com/o/r.git")
            == "https://github.com/o/r.git")


def test_connect_claims_the_folder_rather_than_only_proving_the_token(
    workspace: Path, monkeypatch
):
    """Binding at connect closes the trust-on-first-use window: the first
    SUBMISSION could be days later from anywhere, connect is day zero."""
    calls: list[dict] = []
    monkeypatch.setattr(cadra_connect.client, "bind_workspace",
                        lambda **kw: (calls.append(kw), (200, {}))[1])
    assert _connect(workspace, _token()) == 0
    assert calls[0]["workspace_root"] == str(workspace).replace("\\", "/")
    assert calls[0]["token"] == _token()


def test_a_folder_already_bound_elsewhere_is_refused_at_connect(
    workspace: Path, monkeypatch
):
    """The whole point: the candidate finds out now, not after days of work."""
    monkeypatch.setattr(cadra_connect.client, "bind_workspace",
                        lambda **kw: (409, {"error": {
                            "code": "binding_mismatch",
                            "expected_workspace": "solution"}}))
    _paste(workspace, _token())
    code = cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "https://proxy.test"])
    assert code == 1
    assert not config.config_path(workspace).exists()


def test_the_expected_folder_is_named_so_the_candidate_can_self_diagnose(
    workspace: Path, monkeypatch, capsys
):
    monkeypatch.setattr(cadra_connect.client, "bind_workspace",
                        lambda **kw: (409, {"error": {
                            "expected_workspace": "my-solution"}}))
    _paste(workspace, _token())
    cadra_connect.main(["--workspace", str(workspace), "--proxy", "https://proxy.test"])
    out = capsys.readouterr().out
    assert "my-solution" in out
    assert _token() not in out


def test_a_bad_token_still_reads_as_a_token_problem_not_a_folder_one(
    workspace: Path, monkeypatch, capsys
):
    monkeypatch.setattr(cadra_connect.client, "bind_workspace",
                        lambda **kw: (401, {"error": {"code": "invalid_token"}}))
    _paste(workspace, _token())
    assert cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "https://proxy.test"]) == 1
    assert "did not accept this token" in capsys.readouterr().out


def test_an_unreachable_server_never_leaves_a_config(workspace: Path, monkeypatch):
    monkeypatch.setattr(cadra_connect.client, "bind_workspace",
                        lambda **kw: (0, {"error": {"code": "network_error",
                                                    "message": "URLError"}}))
    _paste(workspace, _token())
    assert cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "https://proxy.test"]) == 1
    assert not config.config_path(workspace).exists()


# ── the proxy address comes from the client, never from the token ─────────────

def _token_with(claims: dict) -> str:
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    base = {"coding_assessment_id": "a-1", "exp": 99999999999}
    return f"{seg({'alg': 'HS256'})}.{seg({**base, **claims})}.sig"


def test_connect_without_a_proxy_flag_uses_the_built_in_default(
    workspace: Path, monkeypatch
):
    """The whole point: no flag, no env, no claim — and it still resolves.

    Previously this fell through to "" and died on the scheme check, so connect
    only worked when the agent recalled a URL out of SKILL.md.
    """
    monkeypatch.delenv(config.PROXY_ENV_VAR, raising=False)
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token())
    assert cadra_connect.main(["--workspace", str(workspace)]) == 0
    cfg = config.load(workspace)
    assert cfg["proxy_base_url"] == config.DEFAULT_PROXY_BASE_URL


def test_connect_honours_the_environment_override(workspace: Path, monkeypatch):
    monkeypatch.setenv(config.PROXY_ENV_VAR, "https://staging.example")
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token())
    assert cadra_connect.main(["--workspace", str(workspace)]) == 0
    assert config.load(workspace)["proxy_base_url"] == "https://staging.example"


def test_a_proxy_base_url_claim_in_the_token_is_ignored(workspace: Path, monkeypatch):
    """SECURITY. Claims are decoded without verifying the signature, so a claim
    naming the destination would let unauthenticated input walk the bearer token
    to an attacker's host — the exfiltration client._NoRedirects prevents for
    redirects. The token must not be able to influence routing at all.
    """
    monkeypatch.delenv(config.PROXY_ENV_VAR, raising=False)
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token_with({"proxy_base_url": "https://attacker.example"}))
    assert cadra_connect.main(["--workspace", str(workspace)]) == 0
    cfg = config.load(workspace)
    assert cfg["proxy_base_url"] == config.DEFAULT_PROXY_BASE_URL
    assert "attacker" not in cfg["proxy_base_url"]


def test_the_verified_destination_is_the_resolved_one(workspace: Path, monkeypatch):
    """A claim must not reach verify_token either — otherwise the token is sent
    to the attacker's host before anything is written to disk."""
    seen: list[str] = []
    monkeypatch.delenv(config.PROXY_ENV_VAR, raising=False)
    monkeypatch.setattr(
        cadra_connect, "verify_token",
        lambda **kw: (seen.append(kw["proxy_base_url"]), (True, ""))[1],
    )
    _paste(workspace, _token_with({"proxy_base_url": "https://attacker.example"}))
    cadra_connect.main(["--workspace", str(workspace)])
    assert seen == [config.DEFAULT_PROXY_BASE_URL]


def test_connect_accepts_a_loopback_proxy_for_local_testing(workspace: Path, monkeypatch):
    """Connect and submit must agree on what is allowed. If connect kept a
    stricter https-only rule, a local proxy would be unreachable at connect even
    though client.is_allowed_base_url would later permit the submit."""
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token())
    code = cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "http://localhost:8080"])
    assert code == 0
    assert config.load(workspace)["proxy_base_url"] == "http://localhost:8080"


def test_connect_refuses_a_userinfo_spoofed_loopback_proxy(workspace: Path, monkeypatch):
    """"localhost:8080" before an @ is userinfo; the real host is evil.example."""
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token())
    code = cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "http://localhost:8080@evil.example"])
    assert code == 1
    assert not config.config_path(workspace).exists()


# ── connecting twice must not read as "you lost your token" ───────────────────
# A successful connect DELETES token.txt, so a second connect hit the
# no-token-file branch and told the candidate to run --init and paste a token
# again — for a credential they had already used successfully, and which they
# would have to go back to the Setup page to re-copy. The README had to warn
# that this message "only means you are done", which is docs compensating for a
# wrong message. The states are distinguishable from config.json, locally.

def test_connecting_twice_reports_already_connected(workspace: Path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cadra_connect, "verify_token",
                        lambda **kw: (calls.append("verify"), (True, ""))[1])
    _paste(workspace, _token())
    assert cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "https://proxy.test"]) == 0
    # Second run: token.txt is gone because the first run consumed it.
    code = cadra_connect.main(["--workspace", str(workspace),
                               "--proxy", "https://proxy.test"])
    assert code == 0, "already-connected is the desired state, not a failure"
    # And it must not re-verify: nothing changed, so nothing needs the network.
    assert calls == ["verify"], calls


def test_already_connected_does_not_ask_for_a_token(workspace: Path, monkeypatch, capsys):
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token())
    cadra_connect.main(["--workspace", str(workspace), "--proxy", "https://proxy.test"])
    capsys.readouterr()
    cadra_connect.main(["--workspace", str(workspace), "--proxy", "https://proxy.test"])
    out = capsys.readouterr().out
    assert "no token file" not in out.lower()
    assert "--init" not in out
    assert "paste" not in out.lower()


def test_an_unconnected_workspace_still_asks_for_the_token(workspace: Path, capsys):
    """The original message is correct when there is genuinely nothing yet."""
    cadra_connect.main(["--workspace", str(workspace), "--init"])
    capsys.readouterr()
    code = cadra_connect.main(["--workspace", str(workspace), "--proxy", "https://proxy.test"])
    assert code == 1
    out = capsys.readouterr().out
    assert "no token file" in out.lower()


def test_an_expired_stored_token_is_reported_as_expired_not_as_connected(
    workspace: Path, monkeypatch, capsys
):
    """"Already connected" would be a lie once the stored token has lapsed.
    exp is readable from the stored token locally — no secret, no network.

    Set up by connecting normally and then ageing the STORED token: connect
    rejects an expired token up front, so config.json can only come to hold one
    by the window closing after a successful connect — which is exactly the real
    case, a candidate returning weeks later.
    """
    monkeypatch.setattr(cadra_connect, "verify_token", lambda **kw: (True, ""))
    _paste(workspace, _token())
    cadra_connect.main(["--workspace", str(workspace), "--proxy", "https://proxy.test"])
    cfg = config.load(workspace)
    cfg["token"] = _token(exp=1)              # the window has since closed
    config.save(workspace, cfg)
    capsys.readouterr()
    code = cadra_connect.main(["--workspace", str(workspace), "--proxy", "https://proxy.test"])
    assert code == 1
    out = capsys.readouterr().out.lower()
    # Assert on the SENTENCE, not the word: pytest names its tmp dir after the
    # test, so a bare `"expired" in out` matches the path in any message and
    # passes against the old "no token file" text.
    assert "token has expired" in out
    assert "no token file" not in out
    assert "already connected" not in out


# ── failure messages must name a runnable next step ──────────────────────────
# These are read by the agent and relayed to a human. "Run with --init first"
# named an internal flag without saying run WHAT, so neither reader could act:
# the agent had no command to run, and a candidate has no business seeing a CLI
# flag at all. SKILL.md step 5 says "relay the reason and what to do about it",
# which only works if the message carries both.

def test_the_unprepared_message_gives_a_command_that_can_actually_be_run(
    workspace: Path, capsys
):
    (workspace / ".cadra").mkdir(parents=True, exist_ok=True)
    code = cadra_connect.main(["--workspace", str(workspace), "--proxy", "https://proxy.test"])
    assert code == 1
    out = capsys.readouterr().out
    assert "cadra_connect.py" in out, "must name the script, not just the flag"
    assert "--init" in out
    assert str(workspace) in out, "must name the workspace it should be run against"


def test_a_rejected_proxy_address_is_echoed_back(workspace: Path, monkeypatch, capsys):
    """Without the value, an operator with a typo'd CADRA_PROXY_BASE_URL is told
    the rule but not which address broke it."""
    monkeypatch.setenv(config.PROXY_ENV_VAR, "http://192.168.1.50:8080")
    _paste(workspace, _token())
    code = cadra_connect.main(["--workspace", str(workspace)])
    assert code == 1
    out = capsys.readouterr().out
    assert "192.168.1.50:8080" in out
