"""Shared Spec Cloud retry policy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spec_cli.api import ApiError, CloudClient
from spec_cli.config import Credentials


def _response(status: int, body: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.content = b"{}"
    response.json.return_value = body or {}
    response.text = ""
    return response


def test_idempotent_get_retries_transient_responses(monkeypatch) -> None:
    client = CloudClient(Credentials(api_base="https://spec.test", access_token="t"))
    client._s.request = MagicMock(  # noqa: SLF001
        side_effect=[_response(502), _response(503), _response(200, {"id": 7})]
    )
    delays: list[float] = []
    monkeypatch.setattr("spec_cli.api.time.sleep", delays.append)

    assert client._request("GET", "/api/example") == {"id": 7}  # noqa: SLF001
    assert client._s.request.call_count == 3  # noqa: SLF001
    assert delays == [0.25, 0.5]


def test_non_idempotent_request_does_not_retry(monkeypatch) -> None:
    client = CloudClient(Credentials(api_base="https://spec.test", access_token="t"))
    client._s.request = MagicMock(return_value=_response(502))  # noqa: SLF001
    sleep = MagicMock()
    monkeypatch.setattr("spec_cli.api.time.sleep", sleep)

    with pytest.raises(ApiError) as caught:
        client._request("POST", "/api/example", json={"x": 1})  # noqa: SLF001

    assert caught.value.transient is True
    assert client._s.request.call_count == 1  # noqa: SLF001
    sleep.assert_not_called()
