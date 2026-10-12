"""Config lives in <workspace>/.cadra/ and the token never reaches git (§4)."""
import base64
import json
from pathlib import Path

import pytest

from cadra import config


def _token(claims: dict) -> str:
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{seg({'alg': 'HS256'})}.{seg(claims)}.signature"


def test_save_then_load_roundtrip(workspace: Path):
    config.save(workspace, {"token": "t", "workspace_root": str(workspace)})
    assert config.load(workspace)["token"] == "t"


def test_find_workspace_from_a_subdirectory(workspace: Path):
    config.save(workspace, {"token": "t"})
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    assert config.find_workspace(nested) == workspace


def test_find_workspace_returns_none_when_unconfigured(tmp_path: Path):
    assert config.find_workspace(tmp_path) is None


def test_nested_workspace_resolves_to_the_innermost(workspace: Path):
    inner = workspace / "inner"
    inner.mkdir()
    config.save(workspace, {"token": "outer"})
    config.save(inner, {"token": "inner"})
    assert config.find_workspace(inner) == inner


def test_gitignore_is_created_with_the_entry(workspace: Path):
    assert config.ensure_gitignored(workspace) is True
    assert ".cadra/" in (workspace / ".gitignore").read_text()


def test_gitignore_append_is_idempotent(workspace: Path):
    (workspace / ".gitignore").write_text("node_modules/\n")
    config.ensure_gitignored(workspace)
    config.ensure_gitignored(workspace)
    text = (workspace / ".gitignore").read_text()
    assert text.count(".cadra/") == 1
    assert "node_modules/" in text  # existing entries preserved


def test_decode_claims_reads_the_payload_without_verifying():
    claims = config.decode_claims(_token({"coding_assessment_id": "a-1", "exp": 99}))
    assert claims["coding_assessment_id"] == "a-1"


def test_decode_claims_rejects_malformed_tokens():
    for bad in ("", "notatoken", "only.two"):
        with pytest.raises(ValueError):
            config.decode_claims(bad)


# ── proxy resolution (the server address is CLIENT config, not candidate data) ──
# Before this, cadra_connect resolved `args.proxy or claims["proxy_base_url"] or ""`
# and fell through to the empty string, so the only place the real hostname existed
# was prose in SKILL.md for the agent to recall. Nothing mints that claim, and a
# claim is read WITHOUT signature verification — so honouring it would mean sending
# a bearer token to a host named by an unauthenticated field, defeating the
# same-host guarantee client._NoRedirects exists to keep. The address is identical
# for every candidate in an environment, so it belongs in the client.

def test_default_proxy_is_a_usable_https_url():
    assert config.DEFAULT_PROXY_BASE_URL.startswith("https://")
    # client builds f"{base}/v1/traces"; a trailing slash would double the slash.
    assert not config.DEFAULT_PROXY_BASE_URL.endswith("/")


def test_resolve_proxy_falls_back_to_the_built_in_default(monkeypatch):
    monkeypatch.delenv(config.PROXY_ENV_VAR, raising=False)
    assert config.resolve_proxy("") == config.DEFAULT_PROXY_BASE_URL


def test_resolve_proxy_prefers_the_environment_over_the_default(monkeypatch):
    monkeypatch.setenv(config.PROXY_ENV_VAR, "https://staging.example")
    assert config.resolve_proxy("") == "https://staging.example"


def test_resolve_proxy_prefers_the_explicit_flag_over_everything(monkeypatch):
    monkeypatch.setenv(config.PROXY_ENV_VAR, "https://staging.example")
    assert config.resolve_proxy("https://flag.example") == "https://flag.example"


def test_resolve_proxy_ignores_a_blank_environment_value(monkeypatch):
    # An exported-but-empty var is the common shell accident. Treating "" as a
    # value would resolve to the empty URL and fail the scheme check.
    monkeypatch.setenv(config.PROXY_ENV_VAR, "   ")
    assert config.resolve_proxy("") == config.DEFAULT_PROXY_BASE_URL


def test_resolve_proxy_strips_a_trailing_slash(monkeypatch):
    monkeypatch.delenv(config.PROXY_ENV_VAR, raising=False)
    assert config.resolve_proxy("https://p.example/") == "https://p.example"


def test_resolve_proxy_never_consults_the_token(monkeypatch):
    """Regression guard: resolve_proxy takes no token and must not grow one.

    A future "per-environment routing" change would naturally reach for a
    proxy_base_url claim. That is unverified input deciding where a credential
    is sent, so the signature cannot be the resolution source.
    """
    import inspect
    params = list(inspect.signature(config.resolve_proxy).parameters)
    assert params == ["cli_value"], params
