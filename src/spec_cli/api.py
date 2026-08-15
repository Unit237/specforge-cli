"""
Thin HTTP client for Spec Cloud.

The cloud API surface is deliberately tiny (see §7 of PLAN.md). This module
knows nothing about argv or pretty printing — it takes credentials + a bundle
slug and returns JSON. CLI commands layer the UX on top.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import requests

from .config import Credentials
from .http_retry import is_transient_http_status, short_retry_delay

DEFAULT_TIMEOUT = 30


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: Any = None,
        *,
        transient: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.transient = transient


@dataclass
class PushResult:
    project_slug: str
    accepted: list[str]
    rejected: list[tuple[str, str]]  # (path, reason)


class CloudClient:
    def __init__(self, creds: Credentials):
        if not creds.access_token:
            raise ApiError("Not authenticated. Run `spec login`.")
        self._creds = creds
        self._s = requests.Session()
        self._s.headers.update(
            {
                "Authorization": f"Bearer {creds.access_token}",
                "User-Agent": "spec-cli/0.1",
                "Accept": "application/json",
            }
        )

    # -- helpers --------------------------------------------------------

    def _url(self, path: str) -> str:
        base = self._creds.api_base.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        kw.setdefault("timeout", DEFAULT_TIMEOUT)
        method = method.upper()
        max_attempts = 4 if method in {"GET", "HEAD", "OPTIONS"} else 1
        for attempt in range(max_attempts):
            try:
                r = self._s.request(method, self._url(path), **kw)
            except requests.RequestException as e:
                if attempt + 1 < max_attempts:
                    time.sleep(short_retry_delay(attempt))
                    continue
                raise ApiError(
                    f"Network error talking to Cloud: {e}", transient=True
                ) from e

            if (
                is_transient_http_status(r.status_code)
                and attempt + 1 < max_attempts
            ):
                time.sleep(short_retry_delay(attempt))
                continue
            break

        if r.status_code >= 400:
            try:
                body = r.json()
                detail = body.get("detail") if isinstance(body, dict) else body
            except ValueError:
                body = r.text
                detail = r.text
            raise ApiError(
                f"Cloud API {method} {path} → {r.status_code}: {detail}",
                status=r.status_code,
                body=body,
                transient=is_transient_http_status(r.status_code),
            )

        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # -- project resolution --------------------------------------------

    @staticmethod
    def _as_project_out(data: Any) -> dict[str, Any]:
        if isinstance(data, dict) and "id" in data:
            return data
        extra = ""
        if isinstance(data, str):
            lead = data.lstrip()[:300].lower()
            if lead.startswith("<!doctype") or lead.startswith("<html") or lead.startswith(
                "<head"
            ):
                extra = (
                    " The response body looks like HTML (e.g. the single-page app) — "
                    "so /api/... may not be reaching the FastAPI backend. "
                    "For self-serve: confirm `api_base` in ~/.spec/credentials "
                    "matches the host that serves /api (try the same origin you use in the "
                    "browser, or set SPEC_API for `spec login`)."
                )
            elif data:
                extra = f" First bytes: {data[:120]!r}…" if len(data) > 120 else f" Body: {data!r}"
        raise ApiError(
            "Cloud returned an unexpected project response (expected a JSON object "
            "with an `id` field). "
            "Usually the CLI got HTML or plain text instead of API JSON (wrong "
            "SPEC_API / `api_base`, or a proxy returned a web page). "
            f"Got: {type(data).__name__}.{extra}"
        )

    def resolve_project(self, handle: str, slug: str) -> dict[str, Any]:
        """Look up ``<handle>/<slug>`` on Cloud.

        Falls back to the legacy ``/api/projects/by-slug`` endpoint
        when the server doesn't know about ``by-handle`` yet — keeps
        new CLIs talking to old servers during a deploy window. The
        legacy resolver also accepts ``<handle>/<slug>`` as a single
        path segment: ``/by-slug/{encode("h/s")}`` so ``h/s`` is one
        slug, not two extra path segments.
        """
        try:
            raw = self._request(
                "GET", f"/api/projects/by-handle/{handle}/{slug}"
            )
            return self._as_project_out(raw)
        except ApiError as e:
            if e.status == 404:
                # Try the back-compat path. A pre-namespacing server
                # accepts the qualified slug as a single path segment
                # (`jon%2Fspec` not `.../by-slug/jon/spec`, which 404s).
                qualified = f"{handle}/{slug}"
                try:
                    path = f"/api/projects/by-slug/{quote(qualified, safe='')}"
                    raw = self._request("GET", path)
                    return self._as_project_out(raw)
                except ApiError:
                    pass
            raise

    def create_project(
        self, name: str, *, description: str | None = None
    ) -> dict[str, Any]:
        """Register a new bundle (``POST /api/projects``).

        The server slugifies ``name`` and appends ``-2``, ``-3``, … when
        the caller already owns a project with that slug — callers should
        read ``slug`` from the response and reconcile ``cloud.project``.
        """
        body: dict[str, Any] = {"name": name.strip()}
        if description is not None and description.strip():
            body["description"] = description.strip()
        raw = self._request("POST", "/api/projects", json=body)
        return self._as_project_out(raw)

    # -- teams ---------------------------------------------------------

    def list_team_members(self, team_id: int) -> list[dict[str, Any]]:
        """Return members of one team the signed-in user can access."""
        data = self._request("GET", f"/api/teams/{team_id}/members")
        return data or []

    # -- files ---------------------------------------------------------

    def list_files(
        self, project_id: int, *, include_content: bool = False
    ) -> list[dict[str, Any]]:
        """
        List every file in a bundle.

        Metadata-only by default (cheap, for status / drift checks). Pass
        ``include_content=True`` when you need the payload in a single
        round trip — notably for ``spec pull``, which otherwise would
        need one HTTP GET per file.
        """
        params = {"include_content": "1"} if include_content else None
        data = self._request(
            "GET", f"/api/projects/{project_id}/files", params=params
        )
        return data or []

    def get_file(self, project_id: int, path: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/projects/{project_id}/files/by-path",
            params={"path": path},
        )

    def batch_upload(
        self,
        project_id: int,
        items: Iterable[dict[str, Any]],
        *,
        bundle_id: str | None = None,
        github_repository: str | None = None,
    ) -> dict[str, Any]:
        """
        `items` are {"path": str, "content": str, "branch": str|None,
        "commit_sha": str|None}. Cloud enforces the 10-file cap
        (constants.MAX_BATCH_SIZE); callers should chunk. `branch` and
        `commit_sha` come from the caller's git worktree and are persisted
        on every file row so the Cloud UI can render per-file git history.

        ``bundle_id`` (PLAN.md §11) is the working tree's bound bundle
        identity, copied from ``cloud.bundle_id`` in ``spec.yaml``. When
        present, the server compares it against the resolved project's
        immutable id and returns ``409`` if they differ — that's the
        durable backstop against retargeting a working tree at the wrong
        bundle. Older servers ignore the field (forward-compatible).

        ``github_repository`` is the ``owner/name`` parsed from the git
        origin. A GitHub-aware server binds the bundle to its already-mirrored
        immutable repository id and refuses a conflicting or unauthorized
        origin. It is omitted outside a github.com worktree.
        """
        payload: dict[str, Any] = {"files": list(items)}
        if bundle_id is not None:
            payload["bundle_id"] = bundle_id
        if github_repository is not None:
            payload["github_repository"] = github_repository
        return self._request(
            "POST",
            f"/api/projects/{project_id}/files/batch",
            json=payload,
        )

    def file_history(self, project_id: int, path: str) -> list[dict[str, Any]]:
        """
        All revisions of a single file, newest first. Each row carries
        `version`, `content_hash`, `branch`, `commit_sha`, `created_at`.
        """
        data = self._request(
            "GET",
            f"/api/projects/{project_id}/files/by-path/history",
            params={"path": path},
        )
        return data or []

    def delete_file(self, project_id: int, path: str) -> None:
        self._request(
            "DELETE",
            f"/api/projects/{project_id}/files/by-path",
            params={"path": path},
        )

    def get_log(self, project_id: int) -> list[dict[str, Any]]:
        """
        Push/run log for a bundle. The shape isn't formalized in v0.1 — we
        just pretty-print whatever the server returns.
        """
        data = self._request("GET", f"/api/projects/{project_id}/log")
        return data or []

    # -- prompt events (Spec Live) -------------------------------------

    def list_prompt_events(
        self,
        project_id: int,
        *,
        since_id: int | None = None,
        limit: int | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Read recent prompt events for a project.

        With ``since_id``, returns ascending events with ``id > since_id``
        (catch-up replay). Optional ``session_id`` restricts to one agent
        session (exact match). Without ``since_id``, returns the most recent
        ``limit`` events in descending order. Without ``since_id``, optional
        ``session_id`` limits that descending window to one session.
        """
        params: dict[str, Any] = {}
        if since_id is not None:
            params["since_id"] = since_id
        if limit is not None:
            params["limit"] = limit
        if session_id is not None:
            params["session_id"] = session_id
        req_kw: dict[str, Any] = {}
        if timeout is not None:
            req_kw["timeout"] = timeout
        data = self._request(
            "GET",
            f"/api/projects/{project_id}/prompt-events",
            params=params or None,
            **req_kw,
        )
        return data or []

    def post_prompt_event(
        self,
        project_id: int,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one observed turn to the project's live stream.

        Used directly only by tests / one-off scripts; ``spec watch``
        bypasses this method and uses ``HTTPPoster`` so it can keep a
        long-lived ``requests.Session`` open across many POSTs without
        the JSON-decoding round trip.
        """
        return self._request(
            "POST",
            f"/api/projects/{project_id}/prompt-events",
            json=body,
        )

    def list_my_prompt_events(
        self,
        *,
        limit: int = 50,
        author_handle: str | None = None,
        role: str | None = None,
        include_presence: bool = False,
    ) -> list[dict[str, Any]]:
        """Cross-bundle Spec Live events for the signed-in user's workspace.

        Wraps ``GET /api/me/prompt-events`` — one round trip instead of
        iterating every project.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "include_presence": "true" if include_presence else "false",
        }
        if author_handle:
            params["author_handle"] = author_handle
        if role:
            params["role"] = role
        data = self._request("GET", "/api/me/prompt-events", params=params)
        return data or []

    def list_prompt_event_flags(
        self, project_id: int, event_id: int
    ) -> list[dict[str, Any]]:
        """Return the flags (reactions / warnings / acks) on one event."""
        data = self._request(
            "GET",
            f"/api/projects/{project_id}/prompt-events/{event_id}/flags",
        )
        return data or []

    def create_prompt_event_flag(
        self,
        *,
        project_id: int,
        event_id: int,
        kind: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Attach a flag to a prompt event.

        Mirrors ``POST /api/projects/{pid}/prompt-events/{eid}/flags``.
        The server fans the new flag out over SSE so every connected
        watcher (per-project ``spec watch`` and workspace-wide
        ``spec team watch``) sees it immediately.
        """
        body: dict[str, Any] = {"kind": kind}
        if note is not None:
            body["note"] = note
        return self._request(
            "POST",
            f"/api/projects/{project_id}/prompt-events/{event_id}/flags",
            json=body,
        )

    def delete_prompt_event_flag(
        self, project_id: int, flag_id: int
    ) -> None:
        """Delete one of your own flags by id."""
        self._request(
            "DELETE",
            f"/api/projects/{project_id}/prompt-events/flags/{flag_id}",
        )

    # -- branch reviews ------------------------------------------------

    def open_branch_review(
        self,
        project_id: int,
        branch: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        requested_reviewers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Open (or idempotently re-open) a review for a non-trunk branch.

        Wraps ``POST /branches/by-name/review``. The Cloud endpoint is
        idempotent: a second call on an already-open review updates
        the title / summary / reviewers when they're set, returning
        the existing row. That's the property `spec push` relies on
        — we call this on every push to a non-trunk branch and the
        review either appears (first push) or stays put (subsequent
        pushes).
        """
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if summary is not None:
            body["summary"] = summary
        if requested_reviewers is not None:
            body["requested_reviewers"] = requested_reviewers

        from urllib.parse import quote as _q

        path = (
            f"/api/projects/{project_id}/branches/by-name/review"
            f"?branch={_q(branch, safe='')}"
        )
        return self._request("POST", path, json=body)
