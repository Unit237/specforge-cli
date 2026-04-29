import pytest

from spec_cli.config import (
    RemoteUrlError,
    parse_cloud_project,
    parse_remote_url,
)


def test_parses_handle_and_slug():
    t = parse_remote_url("https://spec.lightreach.io/acme/billing")
    assert t.api_base == "https://spec.lightreach.io"
    assert t.handle == "acme"
    assert t.slug == "billing"
    assert t.raw_url == "https://spec.lightreach.io/acme/billing"


def test_strips_git_suffix():
    t = parse_remote_url("https://spec.lightreach.io/acme/billing.git")
    assert t.handle == "acme"
    assert t.slug == "billing"


def test_lowercases_handle():
    # Handles are case-insensitive on the server (always lowercase);
    # the URL parser folds them so a copy-pasted "Acme" still resolves.
    t = parse_remote_url("https://spec.lightreach.io/Acme/billing.git")
    assert t.handle == "acme"


def test_trailing_slash_ok():
    t = parse_remote_url("https://spec.lightreach.io/acme/billing/")
    assert t.handle == "acme"
    assert t.slug == "billing"


def test_http_and_ports_allowed():
    t = parse_remote_url("http://localhost:8000/dev/bundle")
    assert t.api_base == "http://localhost:8000"
    assert t.handle == "dev"
    assert t.slug == "bundle"


def test_rejects_non_http_scheme():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("ftp://host/foo/bar")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("git@github.com:foo/bar.git")


def test_rejects_missing_host():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https:///acme/billing")


def test_rejects_missing_handle():
    # A bare slug at the root used to be accepted; the new contract is
    # "two segments or it's malformed". The error message should
    # explicitly call out that handles are now required.
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/billing")


def test_rejects_more_than_two_segments():
    # Multi-segment paths are reserved for future namespacing schemes;
    # silently flattening them (the previous behaviour) routed the
    # push to the wrong place. Fail loudly instead.
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/acme/team/billing.git")


def test_rejects_missing_slug():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/.git")


def test_rejects_query_or_fragment():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/acme/billing?ref=main")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/acme/billing#readme")


def test_rejects_empty():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("")


def test_rejects_malformed_handle():
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/-acme/billing")
    with pytest.raises(RemoteUrlError):
        parse_remote_url("https://spec.lightreach.io/acme./billing")


def test_cloud_project_qualified():
    h, s = parse_cloud_project("acme/billing")
    assert h == "acme"
    assert s == "billing"


def test_cloud_project_bare_slug_uses_default_handle():
    # Pre-namespacing manifests had `cloud.project: billing`. The new
    # CLI keeps them working by falling back to the handle from the
    # signed-in credentials.
    h, s = parse_cloud_project("billing", default_handle="acme")
    assert h == "acme"
    assert s == "billing"


def test_cloud_project_bare_slug_without_handle_errors():
    # No saved credentials means we can't guess the handle; surface a
    # clear error instead of a confusing 404 from Cloud later on.
    with pytest.raises(RemoteUrlError):
        parse_cloud_project("billing", default_handle=None)


def test_cloud_project_lowercases_handle():
    h, _ = parse_cloud_project("Acme/billing")
    assert h == "acme"


def test_cloud_project_rejects_empty():
    with pytest.raises(RemoteUrlError):
        parse_cloud_project("")
