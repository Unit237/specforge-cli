"""Client-side normalization for the Spec Live wire contract."""

from spec_cli.realtime.events import IncomingEvent, OutgoingEvent, ToolCallPayload


def test_outgoing_event_clips_untrusted_adapter_metadata() -> None:
    event = OutgoingEvent(
        session_id="s" * 300,
        source="codex",
        role="user",
        branch="b" * 300,
        commit_sha="c" * 300,
        model="m" * 300,
        phase="final_answer",
        summary="u" * 3000,
        text="t" * (512 * 1024 + 10),
        title="A Codex prompt title " + "x" * 400,
        cwd="/" + "d" * 2000,
        paths_touched=["p" * 2000] * 100,
        tool_calls=[
            ToolCallPayload(name="n" * 100, status="z" * 100)
            for _ in range(300)
        ],
    )

    payload = event.to_json()

    assert len(payload["session_id"]) == 128
    assert len(payload["branch"]) == 255
    assert len(payload["commit_sha"]) == 128
    assert len(payload["model"]) == 128
    assert payload["phase"] == "final_answer"
    assert len(payload["summary"]) == 2000
    assert len(payload["text"]) == 512 * 1024
    assert len(payload["title"]) == 200
    assert len(payload["cwd"]) == 1024
    assert len(payload["paths_touched"]) == 64
    assert all(len(path) == 1024 for path in payload["paths_touched"])
    assert len(payload["tool_calls"]) == 256
    assert len(payload["tool_calls"][0]["name"]) == 64
    assert len(payload["tool_calls"][0]["status"]) == 32


def test_incoming_workspace_event_accepts_null_project_id() -> None:
    event = IncomingEvent.from_json(
        {
            "id": 41,
            "project_id": None,
            "session_id": "workspace-session",
            "source": "codex",
            "role": "user",
            "bundle_label": "workspace",
            "author": {"user_id": 7, "name": "Maya"},
        }
    )

    assert event.project_id == 0
    assert event.bundle_label == "workspace"


def test_incoming_event_preserves_assistant_phase() -> None:
    event = IncomingEvent.from_json(
        {
            "id": 42,
            "project_id": 1,
            "session_id": "codex-session",
            "source": "codex",
            "role": "assistant",
            "phase": "commentary",
            "author": {"user_id": 7, "name": "Maya"},
        }
    )

    assert event.phase == "commentary"
