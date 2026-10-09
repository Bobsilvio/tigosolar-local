"""Client factory: pick v4 (default) or v3, with auto-fallback.

``api_pref``:
- ``"v4"``   : v4 only.
- ``"v3"``   : v3 only (legacy).
- ``"auto"`` : try v4; if v4 *login or topology* fails with a non-auth error
  (network / unexpected 4xx / 5xx), fall back to v3. An auth failure
  (bad credentials) is NOT a fallback trigger -- it is surfaced so the
  config entry can drive a reauth flow.

The factory performs the initial login and, if a ``token_store`` callback is
given, persists the token state so a restart can reuse a still-valid token
instead of re-authenticating.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .base import BaseTigoClient
from .errors import TigoApiError, TigoAuthError
from .v3 import TigoV3Client
from .v4 import TigoV4Client

_LOGGER = logging.getLogger(__name__)

API_V4 = "v4"
API_V3 = "v3"
API_AUTO = "auto"
VALID_API_PREFS = (API_AUTO, API_V4, API_V3)


async def async_create_client(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    api_pref: str = API_AUTO,
    *,
    token_store: Callable[[dict[str, Any]], None] | None = None,
) -> BaseTigoClient:
    """Build, authenticate and return the appropriate Tigo client."""
    if api_pref not in VALID_API_PREFS:
        api_pref = API_AUTO

    if api_pref in (API_AUTO, API_V4):
        v4 = TigoV4Client(session)
        try:
            await v4.login(email, password)
            # Probe topology so a v4 account that can log in but cannot serve
            # the data we need still falls back (auto only).
            await v4.get_systems()
            _LOGGER.debug("Using Tigo v4 API client")
            if token_store:
                token_store(v4.token_state())
            return v4
        except TigoAuthError:
            # Bad credentials -> never silently fall back; let it surface.
            raise
        except TigoApiError as err:
            if api_pref == API_V4:
                raise
            _LOGGER.warning(
                "Tigo v4 API unavailable (%s); falling back to v3", err
            )

    v3 = TigoV3Client(session)
    await v3.login(email, password)
    _LOGGER.debug("Using Tigo v3 API client")
    if token_store:
        token_store(v3.token_state())
    return v3
