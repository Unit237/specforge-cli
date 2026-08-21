"""
Post-push presence broadcast.

When a developer runs ``spec push`` and the upload succeeds, their
working tree typically becomes clean (or at least: the files that just
landed on the cloud bundle no longer represent "in-flight" edits a
teammate should avoid). We want two signals to propagate to peers as
fast as possible:

1. **Clean state** — drop the user's row from
   ``.spec/team-presence.json`` on every teammate's machine so the AI
   IDE hooks stop treating those files as locked.
2. **New head commit** — propagate the *new* ``head_commit`` (the
   commit the push delivered to the bundle) so peers' briefs can flag
   "this teammate is ahead of you — run `git pull`".

The ``spec watch`` daemon already broadcasts presence on a 15 s tick,
so peers eventually learn both facts on their own. This module short-
circuits that 15 s lag: ``spec push`` calls :func:`announce_push`
right after a successful upload, which POSTs one fresh presence event
over the user's existing credentials. Teammates' watchers receive it
over their SSE stream within an RTT.

We deliberately fail open. If the announce POST fails for any reason
(network blip, server slow, watcher not running for the user), the
regular 15 s tick will pick up the new state. We never want the push
itself to be considered failed because of an announce side-effect, so
all errors are logged at ``info`` and swallowed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
import requests

from ..config import Credentials
from .broadcast_identity import load_or_create_broadcast_client_id
from .events import OutgoingEvent
from .presence import LocalPresence, compute_local_presence

_USER_AGENT = "spec-cli/push-announce"

log = logging.getLogger(__name__)

# Tight timeout: the push has already succeeded and we don't want to
# stall the CLI's exit on a slow server. If the announce fails, the
# watcher's regular tick will mop it up — we lose latency, not data.
_ANNOUNCE_TIMEOUT_SECS = 5.0


def announce_push(
    creds: Credentials,
    project_id: int,
    bundle_root: Path,
    *,
    branch: str | None,
) -> bool:
    """POST one fresh presence event reflecting the post-push state.

    Returns ``True`` when the server accepted the event. ``False``
    means the announce was skipped or the server rejected it; in both
    cases callers should treat the broadcast as best-effort. The
    next ``spec watch`` tick will reconcile state.

    The event we send is the same shape ``spec watch`` would send on
    its next tick: ``role=presence``, ``source=git``, with the
    current dirty file set (usually empty post-push) and the new
    ``head_commit``. The receiver-side presence mirror has no special
    case for "post-push" — it just persists the fresh row, which is
    exactly what we want.
    """
    if not creds.access_token or not creds.api_base:
        return False

    try:
        local = compute_local_presence(bundle_root)
    except Exception as e:  # noqa: BLE001
        log.info("spec-live: post-push announce skipped: %s", e)
        return False

    event = _build_event(local, project_id=project_id, branch=branch,
                         bundle_root=bundle_root)
    url = f"{creds.api_base.rstrip('/')}/api/projects/{project_id}/prompt-events"
    try:
        r = requests.post(
            url,
            json=event.to_json(),
            timeout=_ANNOUNCE_TIMEOUT_SECS,
            headers={
                "Authorization": f"Bearer {creds.access_token}",
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    except requests.RequestException as e:
        log.info("spec-live: post-push announce network error: %s", e)
        return False
    if r.status_code >= 400:
        log.info(
            "spec-live: post-push announce rejected (%s): %s",
            r.status_code,
            r.text[:200],
        )
        return False
    return True


def _build_event(
    local: LocalPresence,
    *,
    project_id: int,
    branch: str | None,
    bundle_root: Path,
) -> OutgoingEvent:
    """Mirror of :func:`spec_cli.realtime.watcher._broadcast_presence`.

    Kept in sync by shape — we don't import the watcher's helper to
    avoid pulling the daemon module into the push CLI startup path.
    """
    broadcast_client_id = load_or_create_broadcast_client_id(bundle_root)
    payload = local.to_payload()
    # Force-clean: even if the working tree still has dirty files
    # (unlikely after a successful push, but possible if the user
    # made edits during the push), we want peers to know that the
    # files we just uploaded are no longer in-flight. The watcher's
    # next regular tick will refresh with the real dirty set.
    payload.is_clean = True

    file_count = len(payload.files)
    total_lines = sum(f.lines_added + f.lines_removed for f in payload.files)
    if not payload.files:
        summary = "pushed — working tree clean"
    elif file_count == 1:
        f = payload.files[0]
        summary = (
            f"pushed — {f.path} (+{f.lines_added}/-{f.lines_removed}) "
            f"still dirty"
        )
    else:
        summary = f"pushed — {file_count} files still dirty (+{total_lines})"

    return OutgoingEvent(
        # Same stable session id the watcher uses so server-side
        # dedupe (session + role + turn_at) treats this as part of
        # the same presence stream.
        session_id=f"presence:{project_id}",
        source="git",
        role="presence",
        branch=branch,
        commit_sha=payload.head_commit,
        summary=summary,
        text=None,
        title=None,
        cwd=str(bundle_root),
        paths_touched=[f.path for f in payload.files][:64],
        presence=payload,
        turn_at=datetime.now(timezone.utc),
        broadcast_client_id=broadcast_client_id,
    )


__all__ = ["announce_push"]
