"""REST warm-up history for ``spec watch`` on connect."""

from __future__ import annotations

from unittest.mock import MagicMock

from spec_cli.realtime.watcher import build_watch_bootstrap_events


def test_build_watch_bootstrap_skips_transport_rows_and_sorts_ascending() -> None:
    client = MagicMock()
    client.list_my_prompt_events.return_value = [
        {"id": 3, "role": "presence", "project_id": 1, "session_id": "s"},
        {
            "id": 4,
            "role": "assistant_closed",
            "project_id": 1,
            "session_id": "s",
            "source": "codex",
            "author": {"user_id": 1, "handle": "a", "name": "A"},
        },
        {
            "id": 2,
            "role": "assistant",
            "project_id": 1,
            "session_id": "s",
            "source": "cursor",
            "text": "hi",
            "author": {"user_id": 1, "handle": "a", "name": "A"},
        },
        {
            "id": 1,
            "role": "user",
            "project_id": 1,
            "session_id": "s",
            "source": "cursor",
            "text": "hey",
            "author": {"user_id": 1, "handle": "a", "name": "A"},
        },
    ]
    events = build_watch_bootstrap_events(client, limit=10)
    assert [e.id for e in events] == [1, 2]
    assert all(e.role not in {"presence", "assistant_closed"} for e in events)
    client.list_my_prompt_events.assert_called_once_with(limit=20)
