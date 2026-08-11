"""`spec review` — request the workspace's cloud reviewer for the current PR."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..git import read_git_context
from ..ui import dim, fatal, ok, pointer


def _run_gh(arguments: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *arguments],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"GitHub CLI failed: {exc}") from exc


def _pull_request(
    root: Path,
    *,
    repository: str,
    number: int | None,
) -> dict[str, Any]:
    command = ["pr", "view"]
    if number is not None:
        command.append(str(number))
    command.extend(
        [
            "--repo",
            repository,
            "--json",
            "number,url,headRefOid,isDraft,state,labels,title",
        ]
    )
    result = _run_gh(command, cwd=root)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Pull request not found").strip()
        raise RuntimeError(detail[:1_000])
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub CLI returned invalid pull-request data") from exc
    if not isinstance(value, dict) or int(value.get("number") or 0) <= 0:
        raise RuntimeError("GitHub CLI did not resolve an open pull request")
    return value


def _ensure_review_label(root: Path, *, repository: str, label: str) -> None:
    listed = _run_gh(
        [
            "label",
            "list",
            "--repo",
            repository,
            "--search",
            label,
            "--limit",
            "100",
            "--json",
            "name",
        ],
        cwd=root,
    )
    if listed.returncode != 0:
        raise RuntimeError((listed.stderr or "Could not inspect repository labels").strip())
    try:
        labels = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub CLI returned invalid label data") from exc
    if any(
        isinstance(item, dict)
        and str(item.get("name") or "").strip().casefold() == label.casefold()
        for item in labels
    ):
        return
    created = _run_gh(
        [
            "label",
            "create",
            label,
            "--repo",
            repository,
            "--color",
            "5319E7",
            "--description",
            "Request an Actionairy-authorized Compress cloud review",
        ],
        cwd=root,
    )
    if created.returncode != 0:
        raise RuntimeError((created.stderr or "Could not create the review label").strip())


@click.command("review")
@click.option("--pr", "pull_request_number", type=click.IntRange(min=1), default=None)
@click.option("--json", "as_json", is_flag=True, help="Print a machine-readable receipt.")
def review_cmd(
    pull_request_number: int | None,
    as_json: bool,
) -> None:
    """Request cloud review for the current branch's open GitHub PR.

    This adds the explicit ``agent-review`` label. GitHub delivers that event
    to Compress; Actionairy chooses and authorizes the reviewer; Compress runs
    the SHA-bound read-only review. Re-pushing while the label remains present
    requests a new review for the new head automatically.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as exc:
        fatal(str(exc))
        return
    if shutil.which("gh") is None:
        fatal("GitHub CLI (`gh`) is required. Install it and run `gh auth login` once.")
        return
    context = read_git_context(root)
    if not context.github_repository:
        fatal("The bundle's `origin` must be a github.com repository.")
        return
    try:
        pull = _pull_request(
            root,
            repository=context.github_repository,
            number=pull_request_number,
        )
    except RuntimeError as exc:
        fatal(str(exc))
        return
    if bool(pull.get("isDraft")):
        fatal("Mark the pull request ready for review before requesting cloud review.")
        return
    if str(pull.get("state") or "").casefold() != "open":
        fatal("Only an open pull request can request cloud review.")
        return
    label = "agent-review"
    labels = {
        str(item.get("name") or "").strip().casefold()
        for item in pull.get("labels") or []
        if isinstance(item, dict)
    }
    already_requested = label.casefold() in labels
    if not already_requested:
        try:
            _ensure_review_label(
                root,
                repository=context.github_repository,
                label=label,
            )
        except RuntimeError as exc:
            fatal(str(exc)[:1_000])
            return
        result = _run_gh(
            [
                "pr",
                "edit",
                str(pull["number"]),
                "--repo",
                context.github_repository,
                "--add-label",
                label,
            ],
            cwd=root,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "GitHub rejected the label").strip()
            fatal(detail[:1_000])
            return
    receipt = {
        "requested": not already_requested,
        "already_requested": already_requested,
        "repository": context.github_repository,
        "pull_request_number": int(pull["number"]),
        "head_sha": str(pull.get("headRefOid") or ""),
        "label": label,
        "url": str(pull.get("url") or ""),
        "trigger": "github_label",
        "pass_threshold": 9.0,
    }
    if as_json:
        click.echo(json.dumps(receipt, sort_keys=True))
        return
    if already_requested:
        ok(f"cloud review is already requested for PR #{pull['number']}")
        dim("Push a new commit to trigger a review of the new head SHA.")
    else:
        ok(f"cloud review requested for PR #{pull['number']} at {receipt['head_sha'][:12]}")
    if receipt["url"]:
        pointer("GitHub", receipt["url"])
    dim("Actionairy selects the reviewer; Compress posts the durable scored review on GitHub.")


__all__ = ["review_cmd"]
