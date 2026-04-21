"""End-to-end tests for the prompt review workflow.

Walks the full lifecycle:
  1. A captured prompt exists under `prompts/captured/`.
  2. `spec prompts submit` moves it into `prompts/curated/_pending/`.
  3. `spec prompts check --ci` exits non-zero (merge would be blocked).
  4. `spec prompts review --accept` moves it into `prompts/curated/`.
  5. `spec prompts check --ci` exits zero (merge unblocked).

Separately exercises the reject path: `--reject` deletes the pending file
from the worktree, which is how a rejected prompt is prevented from ever
reaching the default branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from spec_cli.commands.prompts import prompts_group


PROMPT_TEMPLATE = (
    'schema = "spec.prompts/v0.1"\n'
    "[commit]\n"
    'branch = "main"\n'
    'author_name = "Test"\n'
    'author_email = "test@example.com"\n'
    "\n"
    "[[sessions]]\n"
    'id = "{sid}"\n'
    'source = "manual"\n'
    'title = "{title}"\n'
    "\n"
    "  [[sessions.turns]]\n"
    '  role = "user"\n'
    '  text = "hello"\n'
)


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal valid bundle and cd into it for the duration."""
    (tmp_path / "spec.yaml").write_text(
        "schema: spec/v0.1\nname: demo\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "product.md").write_text("# Product\n", encoding="utf-8")
    (tmp_path / "prompts" / "captured").mkdir(parents=True)
    (tmp_path / "prompts" / "captured" / "a.prompts").write_text(
        PROMPT_TEMPLATE.format(sid="a", title="A"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _invoke(args: list[str]) -> tuple[int, str]:
    # Click 8.2 removed `mix_stderr`; stderr is always merged unless an
    # explicit `err` stream is passed. We want combined output for assertions.
    runner = CliRunner()
    result = runner.invoke(prompts_group, args, catch_exceptions=False)
    return result.exit_code, result.output


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def test_submit_moves_captured_to_pending(bundle: Path) -> None:
    exit_code, output = _invoke(["submit", "prompts/captured/a.prompts"])
    assert exit_code == 0, output
    assert not (bundle / "prompts" / "captured" / "a.prompts").exists()
    assert (bundle / "prompts" / "curated" / "_pending" / "a.prompts").is_file()


def test_submit_all_captured(bundle: Path) -> None:
    # Drop a second captured prompt so --all-captured has something to batch.
    (bundle / "prompts" / "captured" / "b.prompts").write_text(
        PROMPT_TEMPLATE.format(sid="b", title="B"), encoding="utf-8"
    )
    exit_code, output = _invoke(["submit", "--all-captured"])
    assert exit_code == 0, output
    pending = list((bundle / "prompts" / "curated" / "_pending").glob("*.prompts"))
    assert {p.name for p in pending} == {"a.prompts", "b.prompts"}
    assert list((bundle / "prompts" / "captured").glob("*.prompts")) == []


def test_submit_refuses_already_curated(bundle: Path) -> None:
    # Seed a curated file and try to submit it.
    curated = bundle / "prompts" / "curated" / "done.prompts"
    curated.parent.mkdir(parents=True, exist_ok=True)
    curated.write_text(PROMPT_TEMPLATE.format(sid="done", title="D"), encoding="utf-8")
    exit_code, output = _invoke(["submit", "prompts/curated/done.prompts"])
    # Curated files are skipped; no successful moves → exit 1.
    assert exit_code == 1, output
    assert curated.is_file()  # untouched


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


def test_check_passes_when_no_pending(bundle: Path) -> None:
    exit_code, output = _invoke(["check", "--ci"])
    assert exit_code == 0
    assert "ok" in output.lower()


def test_check_fails_when_pending_present(bundle: Path) -> None:
    _invoke(["submit", "prompts/captured/a.prompts"])
    exit_code, output = _invoke(["check", "--ci"])
    assert exit_code == 1
    assert "pending" in output.lower()
    assert "prompts/curated/_pending/a.prompts" in output


# ---------------------------------------------------------------------------
# Review — non-interactive accept/reject
# ---------------------------------------------------------------------------


def test_review_accept_moves_to_curated_and_unblocks_check(bundle: Path) -> None:
    _invoke(["submit", "prompts/captured/a.prompts"])
    assert (bundle / "prompts" / "curated" / "_pending" / "a.prompts").is_file()

    exit_code, output = _invoke(
        ["review", "--accept", "prompts/curated/_pending/a.prompts"]
    )
    assert exit_code == 0, output
    assert (bundle / "prompts" / "curated" / "a.prompts").is_file()
    assert not (bundle / "prompts" / "curated" / "_pending" / "a.prompts").exists()

    # Now check should be green.
    exit_code, _ = _invoke(["check", "--ci"])
    assert exit_code == 0


def test_review_reject_deletes_pending(bundle: Path) -> None:
    _invoke(["submit", "prompts/captured/a.prompts"])
    pending = bundle / "prompts" / "curated" / "_pending" / "a.prompts"
    assert pending.is_file()

    exit_code, output = _invoke(
        ["review", "--reject", "prompts/curated/_pending/a.prompts"]
    )
    assert exit_code == 0, output
    # Rejection = the prompt never lands on main: deleted from the worktree.
    assert not pending.exists()
    assert not (bundle / "prompts" / "curated" / "a.prompts").exists()

    exit_code, _ = _invoke(["check", "--ci"])
    assert exit_code == 0


def test_review_accept_and_reject_are_mutually_exclusive(bundle: Path) -> None:
    _invoke(["submit", "prompts/captured/a.prompts"])
    rel = "prompts/curated/_pending/a.prompts"
    exit_code, output = _invoke(["review", "--accept", rel, "--reject", rel])
    assert exit_code != 0
    assert "both" in output.lower() or "cannot" in output.lower()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_reports_counts(bundle: Path) -> None:
    _invoke(["submit", "prompts/captured/a.prompts"])
    exit_code, output = _invoke(["status"])
    assert exit_code == 0
    assert "pending:  1" in output
    assert "curated:  0" in output
    assert "captured: 0" in output
