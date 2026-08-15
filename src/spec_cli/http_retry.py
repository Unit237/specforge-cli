"""Shared HTTP retry policy for Spec Cloud clients."""

from __future__ import annotations


TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_transient_http_status(status: int) -> bool:
    """Whether an HTTP response is safe to retry after a short delay."""
    return status in TRANSIENT_HTTP_STATUSES


def short_retry_delay(attempt: int) -> float:
    """Bounded exponential delay for short request-level retries."""
    return min(2.0, 0.25 * (2**max(0, attempt)))


__all__ = [
    "TRANSIENT_HTTP_STATUSES",
    "is_transient_http_status",
    "short_retry_delay",
]
