"""
Thin HTTP client for Spec Cloud.

The cloud API surface is deliberately tiny (see §7 of PLAN.md). This module
knows nothing about argv or pretty printing — it takes credentials + a bundle
slug and returns JSON. CLI commands layer the UX on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import requests

from .config import Credentials

DEFAULT_TIMEOUT = 30


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


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
        try:
            r = self._s.request(method, self._url(path), **kw)
        except requests.RequestException as e:
            raise ApiError(f"Network error talking to Cloud: {e}") from e

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
            )

        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # -- project resolution --------------------------------------------

    def resolve_project(self, slug: str) -> dict[str, Any]:
        return self._request("GET", f"/api/projects/by-slug/{slug}")

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
        self, project_id: int, items: Iterable[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        `items` are {"path": str, "content": str, "branch": str|None,
        "commit_sha": str|None}. Cloud enforces the 10-file cap
        (constants.MAX_BATCH_SIZE); callers should chunk. `branch` and
        `commit_sha` come from the caller's git worktree and are persisted
        on every file row so the Cloud UI can render per-file git history.
        """
        return self._request(
            "POST",
            f"/api/projects/{project_id}/files/batch",
            json={"files": list(items)},
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
