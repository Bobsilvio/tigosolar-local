"""Tigo API clients.

This package isolates all Tigo HTTP access behind async clients that take an
injected ``aiohttp.ClientSession`` (Home Assistant's shared session):

- ``TigoV3Client``  -> legacy api2.tigoenergy.com (fallback)
- ``TigoV4Client``  -> current mapi.tigoenergy.com (the app's API; default)

``async_create_client`` selects/authenticates the right one with auto-fallback
and token lifecycle handling.
"""

from .base import BaseTigoClient
from .client import API_AUTO, API_V3, API_V4, VALID_API_PREFS, async_create_client
from .errors import TigoApiError, TigoAuthError
from .v3 import TigoV3Client
from .v4 import TigoV4Client

__all__ = [
    "API_AUTO",
    "API_V3",
    "API_V4",
    "VALID_API_PREFS",
    "BaseTigoClient",
    "TigoApiError",
    "TigoAuthError",
    "TigoV3Client",
    "TigoV4Client",
    "async_create_client",
]
