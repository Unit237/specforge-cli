import pytest

from spec_cli.config import RemoteUrlError, parse_remote_url


def test_parses_simple_url():
    t = parse_remote_url("https://spec.lightreach.io/billing")
    assert t.api_base == "https://spec.lightreach.io"
    assert t.slug == "billing"
    assert t.raw_url == "https://spec.lightreach.io/billing"


def test_strips_git_suffix():
    t = parse_remote_url("https://spec.lightreach.io/billing.git")
    assert t.slug == "billing"


def test_preserves_multi_segment_path():
    # Namespacing is a v0.2 concern on the server, but the CLI must not
    # pre-empt it by collapsing `acme/billing` to `billing`.
    t = parse_remote_url("https://spec.lightreach.io/acme/billing.git")
    assert t.api_base == "https://spec.lightreach.io"
    assert t.slug == "acme/billing"


def test_trailing_slash_ok():
    t = parse_remote_url("https://spec.lightreach.io/acme/billing/")
    assert t.slug == "acme/billing"


def test_http_and_ports_allowed():
    t = parse_remote_url("http://localhost:8000/dev-bundle")
    assert t.api_base == "http://localhost:8000"
    assert t.slug == "dev-bundle"


def test_rejects_non_http_scheme():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("ftp://host/bundle")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("git@github.com:foo/bar.git")


def test_rejects_missing_host():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https:///billing")


def test_rejects_missing_slug():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/.git")


def test_rejects_query_or_fragment():
    # These almost always mean the user pasted a web URL by mistake;
    # silently dropping them would route the push somewhere surprising.
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/billing?ref=main")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/billing#readme")


def test_rejects_empty():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("")
