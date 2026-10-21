import json
from types import SimpleNamespace

from click.testing import CliRunner
from spec_cli.cli import cli
from spec_cli.commands import agents


def test_cli_preserves_explicit_contract_identity_and_cursor(monkeypatch):
    observed = []
    class Client:
        def __init__(self, creds): pass
        def resolve_project(self, handle, slug):
            assert (handle, slug) == ("team", "billing")
            return {"id": 17}
        def publish_agent_message(self, project_id, payload):
            observed.append((project_id, payload)); return {"id": 9}
        def agent_messages(self, **kwargs):
            observed.append(kwargs); return {"messages": [], "next_cursor": 9}
    monkeypatch.setattr(agents, "load_credentials", lambda: SimpleNamespace(access_token="test", user_handle="team"))
    monkeypatch.setattr(agents, "CloudClient", Client)
    runner = CliRunner()
    sent = runner.invoke(cli, ["agents", "send", "--project", "team/billing", "--session", "backend", "--team", "--kind", "contract", "--subject", "API v2", "--body", "-", "--artifact", "git:sha:api/schema.json", "--idempotency-key", "api-v2"], input="Use subscription_id")
    assert sent.exit_code == 0, sent.output
    assert json.loads(sent.output)["idempotency_key"] == "api-v2"
    assert observed[0][0] == 17
    assert observed[0][1]["body"] == "Use subscription_id"
    assert observed[0][1]["audience"] == "team"
    inbox = runner.invoke(cli, ["agents", "inbox", "--team", "4", "--session", "frontend", "--after", "9"])
    assert inbox.exit_code == 0
    assert json.loads(inbox.output)["next_cursor"] == 9
    assert observed[-1] == {"project_id": None, "team_id": 4, "after_id": 9, "recipient": "frontend", "limit": 50}


def test_cli_keeps_missing_authentication_and_invalid_cursor_visible(monkeypatch):
    monkeypatch.setattr(agents, "load_credentials", lambda: None)
    runner = CliRunner()
    assert runner.invoke(cli, ["agents", "inbox", "--team", "4"]).exit_code != 0
    assert runner.invoke(cli, ["agents", "inbox", "--after", "-1"]).exit_code != 0
