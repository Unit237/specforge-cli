"""Discover watched pull requests and request cloud or teammate review."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from ..api import CloudClient
from ..config import (
    BundleNotFoundError,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
)
from ..git import read_git_context
from ..ui import dim, fatal, info, ok, pointer, warn
from .push import collect_push_all_roots


@dataclass(frozen=True)
class OpenPullRequest:
    root: Path
    repository: str
    number: int
    url: str
    head_sha: str
    title: str
    author: str
    state: str
    draft: bool
    labels: tuple[str, ...]

    @classmethod
    def from_gh(cls, root: Path, repository: str, value: dict[str, Any]) -> "OpenPullRequest":
        author = value.get("author") if isinstance(value.get("author"), dict) else {}
        return cls(
            root=root,
            repository=repository,
            number=int(value.get("number") or 0),
            url=str(value.get("url") or ""),
            head_sha=str(value.get("headRefOid") or ""),
            title=str(value.get("title") or ""),
            author=str(author.get("login") or ""),
            state=str(value.get("state") or "").casefold(),
            draft=bool(value.get("isDraft")),
            labels=tuple(
                str(item.get("name") or "").strip()
                for item in value.get("labels") or []
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
            "head_sha": self.head_sha,
            "title": self.title,
            "author": self.author,
            "state": self.state,
            "draft": self.draft,
            "labels": list(self.labels),
        }


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


_PR_JSON_FIELDS = "number,url,headRefOid,isDraft,state,labels,title,author"


def _pull_request(
    root: Path,
    *,
    repository: str,
    number: int | None,
) -> dict[str, Any]:
    command = ["pr", "view"]
    if number is not None:
        command.append(str(number))
    command.extend(["--repo", repository, "--json", _PR_JSON_FIELDS])
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


def _watched_repositories() -> list[tuple[Path, str]]:
    seen: set[str] = set()
    repositories: list[tuple[Path, str]] = []
    for root in collect_push_all_roots():
        repository = read_git_context(root).github_repository
        if not repository or repository.casefold() in seen:
            continue
        seen.add(repository.casefold())
        repositories.append((root, repository))
    return repositories


def discover_open_pull_requests(*, all_authors: bool = False) -> list[OpenPullRequest]:
    """List open PRs across every repository registered with Spec watch."""
    pulls: list[OpenPullRequest] = []
    for root, repository in _watched_repositories():
        command = [
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            _PR_JSON_FIELDS,
        ]
        if not all_authors:
            command.extend(["--author", "@me"])
        result = _run_gh(command, cwd=root)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "GitHub query failed").strip()
            warn(f"Could not inspect {repository}: {detail[:300]}")
            continue
        try:
            values = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            warn(f"Could not inspect {repository}: GitHub returned invalid JSON")
            continue
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict):
                continue
            pull = OpenPullRequest.from_gh(root, repository, value)
            if pull.number > 0:
                pulls.append(pull)
    return sorted(pulls, key=lambda pull: (pull.repository.casefold(), pull.number))


def _print_pull_requests(pulls: list[OpenPullRequest]) -> None:
    if not pulls:
        dim("No open pull requests found across watched Spec projects.")
        return
    info(f"Open pull requests across Spec watch ({len(pulls)})")
    for index, pull in enumerate(pulls, start=1):
        state = "draft" if pull.draft else "ready"
        author = f" · @{pull.author}" if pull.author else ""
        info(f"  [{index}] {pull.repository}#{pull.number} · {state}{author} · {pull.title}")


def _ensure_review_label(root: Path, *, repository: str, label: str) -> None:
    listed = _run_gh(
        ["label", "list", "--repo", repository, "--search", label, "--limit", "100", "--json", "name"],
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


def _team_reviewers(root: Path) -> list[dict[str, Any]]:
    """Return the root bundle's Spec teammates, including verified GitHub ids."""
    creds = load_credentials()
    if not creds or not creds.access_token:
        return []
    try:
        manifest = load_manifest(root)
        handle, slug = parse_cloud_project(
            manifest.cloud_project or "",
            default_handle=creds.user_handle,
        )
        client = CloudClient(creds)
        project = client.resolve_project(handle, slug)
        team_id = int(project.get("team_id") or 0)
        if team_id <= 0:
            return []
        members = client.list_team_members(team_id)
    except Exception:  # noqa: BLE001 - teammate suggestions are optional
        return []
    own_email = str(getattr(creds, "user_email", "") or "").casefold()
    return [
        member
        for member in members
        if isinstance(member, dict)
        and str(member.get("email") or "").casefold() != own_email
    ]


def _choose_pull(pulls: list[OpenPullRequest]) -> OpenPullRequest | None:
    if not pulls:
        return None
    if len(pulls) == 1:
        return pulls[0]
    _print_pull_requests(pulls)
    if not sys.stdin.isatty():
        fatal("More than one PR matched. Pass `--pr owner/repository#number`.")
        return None
    selected = click.prompt(
        "Choose a pull request",
        type=click.IntRange(1, len(pulls)),
    )
    return pulls[selected - 1]


def _resolve_selector(selector: str, *, current_root: Path | None) -> OpenPullRequest:
    raw = selector.strip()
    repository = ""
    number = 0
    root = current_root
    if raw.isdigit():
        if root is None:
            raise RuntimeError("A bare PR number requires running inside a watched repository")
        repository = read_git_context(root).github_repository or ""
        number = int(raw)
    else:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)", raw)
        if not match:
            match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", raw)
        if not match:
            raise RuntimeError("Use `--pr owner/repository#number` or a GitHub PR URL")
        repository, number_raw = match.groups()
        number = int(number_raw)
        root = next(
            (candidate for candidate, name in _watched_repositories() if name.casefold() == repository.casefold()),
            current_root,
        )
    if not repository or root is None:
        raise RuntimeError("That pull request is not in a repository registered with Spec watch")
    value = _pull_request(root, repository=repository, number=number)
    return OpenPullRequest.from_gh(root, repository, value)


def _request_cloud_review(pull: OpenPullRequest) -> tuple[bool, bool]:
    label = "agent-review"
    already_requested = label.casefold() in {value.casefold() for value in pull.labels}
    if already_requested:
        return False, True
    _ensure_review_label(pull.root, repository=pull.repository, label=label)
    result = _run_gh(
        ["pr", "edit", str(pull.number), "--repo", pull.repository, "--add-label", label],
        cwd=pull.root,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "GitHub rejected the label").strip())
    return True, False


def _request_teammate_review(pull: OpenPullRequest, reviewer: str) -> str:
    requested = reviewer.strip().removeprefix("@").strip()
    members = _team_reviewers(pull.root)
    matched = next(
        (
            member
            for member in members
            if requested.casefold()
            in {
                str(member.get("handle") or "").casefold(),
                str(member.get("github_login") or "").casefold(),
            }
        ),
        None,
    )
    if members and matched is None:
        raise RuntimeError(f"@{requested} is not a member of this project's Spec team")
    github_login = str((matched or {}).get("github_login") or requested).strip()
    result = _run_gh(
        [
            "pr",
            "edit",
            str(pull.number),
            "--repo",
            pull.repository,
            "--add-reviewer",
            github_login,
        ],
        cwd=pull.root,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "GitHub rejected the reviewer").strip()
        raise RuntimeError(detail[:1_000])
    return github_login


@click.command("prs")
@click.option("--all-authors", is_flag=True, help="Include teammates' PRs, not only yours.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable PRs.")
def prs_cmd(all_authors: bool, as_json: bool) -> None:
    """List open GitHub PRs across every project under Spec watch."""
    if shutil.which("gh") is None:
        fatal("GitHub CLI (`gh`) is required. Install it and run `gh auth login` once.")
        return
    pulls = discover_open_pull_requests(all_authors=all_authors)
    if as_json:
        click.echo(json.dumps([pull.as_dict() for pull in pulls], sort_keys=True))
    else:
        _print_pull_requests(pulls)


@click.command("review")
@click.option("--pr", "pull_request_selector", default=None, metavar="OWNER/REPO#NUMBER")
@click.option(
    "--with",
    "reviewer",
    default=None,
    help="Review with `cloud` (your Compress agent) or a Spec teammate such as `@alice`.",
)
@click.option("--all-authors", is_flag=True, help="Include teammates' PRs when discovering.")
@click.option("--json", "as_json", is_flag=True, help="Print a machine-readable receipt.")
def review_cmd(
    pull_request_selector: str | None,
    reviewer: str | None,
    all_authors: bool,
    as_json: bool,
) -> None:
    """Discover a watched PR and request cloud or teammate review.

    Cloud review adds the explicit ``agent-review`` label. The Compress-owned
    GitHub App forwards normalized repository state to Actionairy, which
    authorizes the user's reviewer profile and memory grant; Compress then
    performs a SHA-bound read-only review.
    A teammate choice uses GitHub's native requested-reviewer mechanism after
    resolving the candidate from the project's Spec team.
    """
    if shutil.which("gh") is None:
        fatal("GitHub CLI (`gh`) is required. Install it and run `gh auth login` once.")
        return
    try:
        current_root = find_bundle_root()
    except BundleNotFoundError:
        current_root = None

    try:
        if pull_request_selector:
            pull = _resolve_selector(pull_request_selector, current_root=current_root)
        else:
            pull = None
            if current_root is not None:
                repository = read_git_context(current_root).github_repository
                if repository:
                    try:
                        value = _pull_request(current_root, repository=repository, number=None)
                        pull = OpenPullRequest.from_gh(current_root, repository, value)
                    except RuntimeError:
                        pull = None
            if pull is None:
                pull = _choose_pull(discover_open_pull_requests(all_authors=all_authors))
        if pull is None:
            fatal("No open pull request found across watched Spec projects.")
            return
    except RuntimeError as exc:
        fatal(str(exc))
        return

    if pull.draft:
        fatal("Mark the pull request ready for review before requesting review.")
        return
    if pull.state != "open":
        fatal("Only an open pull request can request review.")
        return

    teammates = _team_reviewers(pull.root)
    if reviewer is None and not as_json and sys.stdin.isatty():
        info("Review options")
        info("  cloud · your Actionairy-authorized Compress reviewer")
        for member in teammates:
            handle = str(member.get("handle") or member.get("github_login") or "").strip()
            github_login = str(member.get("github_login") or "").strip()
            if handle:
                suffix = f" · GitHub @{github_login}" if github_login else " · GitHub not linked"
                info(f"  @{handle}{suffix}")
        reviewer = click.prompt("Review with", default="cloud", show_default=True)
    reviewer = (reviewer or "cloud").strip()

    try:
        if reviewer.casefold() in {"cloud", "compress", "agent", "my agent"}:
            requested, already_requested = _request_cloud_review(pull)
            receipt = {
                "requested": requested,
                "already_requested": already_requested,
                **pull.as_dict(),
                "review_type": "compress_cloud",
                "reviewer": "self",
                "label": "agent-review",
                "trigger": "github_label",
                "pass_threshold": 9.0,
            }
        else:
            github_login = _request_teammate_review(pull, reviewer)
            receipt = {
                "requested": True,
                "already_requested": False,
                **pull.as_dict(),
                "review_type": "teammate",
                "reviewer": github_login,
                "trigger": "github_requested_reviewer",
            }
    except RuntimeError as exc:
        fatal(str(exc)[:1_000])
        return

    receipt["pull_request_number"] = receipt.pop("number")
    if as_json:
        click.echo(json.dumps(receipt, sort_keys=True))
        return
    if receipt["review_type"] == "compress_cloud":
        if receipt["already_requested"]:
            ok(f"Cloud review is already requested for {pull.repository}#{pull.number}.")
            dim("Push a new commit to trigger a review of the new head SHA.")
        else:
            ok(f"Cloud review requested for {pull.repository}#{pull.number} at {pull.head_sha[:12]}.")
        dim("Actionairy authorizes the profile; Compress posts the durable scored review.")
    else:
        ok(f"Review requested from @{receipt['reviewer']} for {pull.repository}#{pull.number}.")
    if pull.url:
        pointer("GitHub", pull.url)


__all__ = ["discover_open_pull_requests", "prs_cmd", "review_cmd"]
