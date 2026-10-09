"""Exception types shared by the Tigo API clients."""

from __future__ import annotations


class TigoApiError(Exception):
    """Generic Tigo API failure (network, bad status, unparseable body).

    Carries the HTTP ``status`` when known so the coordinator can treat
    403 (not entitled), 404/422 (absent hardware) and 5xx differently.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TigoAuthError(TigoApiError):
    """Authentication failed (bad credentials or expired/invalid token)."""


class TigoThrottleError(TigoApiError):
    """Server asked us to back off (HTTP 429, or 503 with Retry-After).

    ``retry_after`` is seconds to wait before the next request, parsed from
    the ``Retry-After`` header (delta-seconds or HTTP-date). Always honored.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after
