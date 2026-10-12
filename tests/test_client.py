"""The token travels in a header, so where that header can end up is the risk."""
from cadra import client


def test_a_non_https_url_never_leaves_the_process():
    """No socket is opened at all — the check is before the request is built."""
    status, payload = client.post_chunk(base_url="http://proxy.test",
                                        token="tok", body={})
    assert status == 0
    assert payload["error"]["code"] == "insecure_url"


def test_redirects_are_refused_rather_than_followed():
    """urllib's default handler copies every header, Authorization included, into
    the redirected request — cross-host. One 302 would hand over the token."""
    handler = client._NoRedirects()
    assert handler.redirect_request(None, None, 302, "Found", {},
                                    "https://attacker.example/") is None
    assert client._NoRedirects in [type(h) for h in client._OPENER.handlers]


def test_the_token_appears_in_no_error_payload():
    status, payload = client.get_traces(base_url="https://127.0.0.1:1",
                                        token="s3cr3t-token-value")
    assert status == 0
    assert "s3cr3t" not in repr(payload)


# ── loopback carve-out: local testing without weakening the real guarantee ────
# https is required because urllib carries Authorization across hosts and plain
# http is readable on the path. Loopback has neither problem: the bytes never
# leave the machine. Everything else stays refused.

import pytest


@pytest.mark.parametrize("url", [
    "https://proxy.cadra.info",
    "https://proxy.cadra.info/v1",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://[::1]:8080",
])
def test_allowed_base_urls(url):
    assert client.is_allowed_base_url(url) is True


@pytest.mark.parametrize("url", [
    # Plain http to anywhere but loopback: readable by anyone on the path.
    "http://proxy.test",
    "http://proxy.cadra.info",
    # A LAN address is still a network other people can sit on.
    "http://192.168.1.50:8080",
    "http://10.0.0.5:8080",
    # THE TRAP: a prefix check on "http://localhost:" passes this, but
    # "localhost:8080" here is USERINFO — the real host is evil.example. This is
    # why the check must parse the URL and compare .hostname, never startswith.
    "http://localhost:8080@evil.example/",
    "http://127.0.0.1:8080@evil.example/",
    # Suffix games on a hostname that merely starts with the allowed name.
    "http://localhost.evil.example:8080",
    "http://127.0.0.1.evil.example:8080",
    # No port: ambiguous, and nothing legitimate needs it.
    "http://localhost",
    "http://127.0.0.1",
    # Non-HTTP schemes must never reach urlopen.
    "file:///etc/passwd",
    "ftp://localhost:21",
    "",
])
def test_refused_base_urls(url):
    assert client.is_allowed_base_url(url) is False


def test_a_userinfo_spoofed_loopback_url_never_leaves_the_process():
    """End to end through the real call path, not just the predicate."""
    status, payload = client.post_chunk(
        base_url="http://localhost:8080@evil.example", token="s3cr3t", body={})
    assert status == 0
    assert payload["error"]["code"] == "insecure_url"
    assert "s3cr3t" not in repr(payload)
