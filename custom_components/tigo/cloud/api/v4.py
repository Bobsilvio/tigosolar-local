"""Async client for the current Tigo v4 cloud API (mapi.tigoenergy.com).

This is the API the official Tigo mobile app uses. It exposes per-minute,
per-panel telemetry (power/voltage/current/rssi) and per-panel daily energy.

This commit only implements the client surface; the auto-fallback factory and
token-refresh lifecycle are added in the next commit, and the
interpretation/mapping of the returned payloads lives in topology.py /
coordinator.py.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import aiohttp

from .base import BaseTigoClient, log_raw, parse_retry_after
from .errors import TigoApiError, TigoAuthError, TigoThrottleError

_LOGGER = logging.getLogger(__name__)

API_HOST = "https://mapi.tigoenergy.com"

# temp= values for the per-panel summary endpoint.
METRIC_PIN = "pin"
METRIC_VIN = "vin"
METRIC_IIN = "iin"
METRIC_RSSI = "rssi"
METRIC_RECLAIMED = "reclaimedPower"
PANEL_METRICS = (METRIC_PIN, METRIC_VIN, METRIC_IIN, METRIC_RSSI, METRIC_RECLAIMED)


def _cache_buster() -> int:
    """Millisecond epoch, used as the `_` param to defeat 304 caching."""
    return int(time.time() * 1000)


class TigoV4Client(BaseTigoClient):
    """Async wrapper over the Tigo v4 endpoints used by the integration.

    Credential storage and the re-login/expiry lifecycle come from
    ``BaseTigoClient``; ``_do_login`` authenticates and records the token,
    refresh token and expiry.
    """

    api_version = "v4"

    def __init__(self, session: aiohttp.ClientSession, token: str | None = None) -> None:
        super().__init__(session, token)
        self.user_id: int | None = None
        self.user_type: str | None = None

    # ------------------------------------------------------------------ #
    # low-level helpers
    # ------------------------------------------------------------------ #
    @property
    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        auth_required: bool = True,
    ) -> Any:
        hdrs = dict(headers or {})
        if auth_required:
            hdrs.setdefault("Authorization", f"Bearer {self.token}")
            hdrs.setdefault("Accept", "application/json")
        try:
            async with self._session.request(
                method, url, headers=hdrs, json=json
            ) as resp:
                body = await resp.text()
                if resp.status == 401:
                    raise TigoAuthError(
                        f"Tigo v4 unauthorized: {method} {url}", status=401
                    )
                if resp.status == 429 or (
                    resp.status == 503 and "Retry-After" in resp.headers
                ):
                    raise TigoThrottleError(
                        f"Tigo v4 throttled {resp.status}: {method} {url}",
                        status=resp.status,
                        retry_after=parse_retry_after(
                            resp.headers.get("Retry-After")
                        ),
                    )
                if resp.status != 200:
                    # Surface status + a short body snippet; callers (coordinator)
                    # decide how to treat 403/404/422/5xx.
                    raise TigoApiError(
                        f"Tigo v4 error {resp.status}: {method} {url} :: {body[:200]}",
                        status=resp.status,
                    )
                try:
                    parsed = await resp.json(content_type=None)
                except Exception as err:  # noqa: BLE001 - non-JSON 200
                    raise TigoApiError(
                        f"Tigo v4 non-JSON body: {method} {url}: {err}"
                    ) from err
                log_raw(f"{method} (v4)", url, parsed)
                return parsed
        except aiohttp.ClientError as err:
            raise TigoApiError(
                f"Tigo v4 request failed: {method} {url}: {err}"
            ) from err

    # ------------------------------------------------------------------ #
    # auth
    # ------------------------------------------------------------------ #
    async def _do_login(self, email: str, password: str) -> str:
        """POST /api/v3/user/login?type=8 -> store and return bearer token."""
        url = f"{API_HOST}/api/v3/user/login?type=8"
        try:
            async with self._session.post(
                url,
                json={"username": email, "password": password},
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status in (401, 403):
                    raise TigoAuthError("Tigo login failed: invalid credentials")
                if resp.status != 200:
                    raise TigoApiError(f"Tigo login error {resp.status}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise TigoApiError(f"Tigo login request failed: {err}") from err

        user = data.get("user") if isinstance(data, dict) else None
        if not user or "auth" not in user:
            raise TigoApiError(f"Tigo login response missing token: {data!r}")

        self.token = user["auth"]
        self.refresh_token = user.get("refresh_token")
        self.user_id = user.get("user_id")
        self.user_type = user.get("user_type")
        self.expires = self._parse_expires(user.get("expires"))
        return self.token

    @staticmethod
    def _parse_expires(value: Any) -> datetime | None:
        if not value or not isinstance(value, str):
            return None
        try:
            # e.g. "2026-11-10T22:48:15-08:00"
            return datetime.fromisoformat(value)
        except ValueError:
            _LOGGER.debug("Unparseable Tigo token expiry: %r", value)
            return None

    # ------------------------------------------------------------------ #
    # topology / capabilities
    # ------------------------------------------------------------------ #
    async def get_systems(self) -> list[dict]:
        """GET /api/v3/systems/query -> list of systems for this account."""
        data = await self._request_json(
            "GET",
            f"{API_HOST}/api/v3/systems/query?limit=100&page=1&sort=-id",
        )
        if isinstance(data, dict):
            return data.get("systems") or data.get("data") or []
        return data if isinstance(data, list) else []

    async def get_topology(self, system_id: int) -> dict:
        """GET /api/v3/systems/full/{id} -> sources/strings/mppts/inverters."""
        return await self._request_json(
            "GET",
            f"{API_HOST}/api/v3/systems/full/{system_id}?system_id={system_id}",
        )

    async def get_system_layout(self, system_id: int) -> dict:
        """GET /api/v3/systems/layout -> inverter/mppt/string/panel tree.

        Authoritative per-panel mapping: each panel carries object_id, label,
        serial and type. (mapi serves the /api/v3/* paths too.)
        """
        return await self._request_json(
            "GET",
            f"{API_HOST}/api/v3/systems/layout?id={system_id}",
        )

    async def get_capabilities(self, system_id: int) -> dict:
        """GET /api/v4/systems/view/{id}?includes=details -> has_* flags."""
        data = await self._request_json(
            "GET",
            f"{API_HOST}/api/v4/systems/view/{system_id}?includes=details",
        )
        if isinstance(data, dict):
            return data.get("details", data) if "details" in data else data
        return {}

    async def get_system_info(self, system_id: int, date: str) -> dict:
        """GET /api/v3/tigobuild/systeminfo -> has_premium, features, units…"""
        return await self._request_json(
            "GET",
            f"{API_HOST}/api/v3/tigobuild/systeminfo"
            f"?system_id={system_id}&date={date}"
            f"&resourceId=dateinfo-{date}&_={_cache_buster()}",
        )

    async def get_equipments(self, system_id: int) -> list[dict]:
        """GET /api/v4/equipments?systemId= -> ordered equipment list.

        Returned order indexes the telemetry `d[]` arrays; callers MUST keep
        it verbatim (it is stable but not alphabetical).
        """
        data = await self._request_json(
            "GET",
            f"{API_HOST}/api/v4/equipments?systemId={system_id}",
        )
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ #
    # telemetry
    # ------------------------------------------------------------------ #
    async def get_panel_summary(
        self, system_id: int, date: str, metric: str, cca_uid: str
    ) -> dict:
        """GET /api/v4/system/summary/summary for one metric.

        Returns the raw payload: ``{"dataType","dataset":[{"data":[{"t","d":[…]}…]}],
        "lastData": "YYYY-MM-DD HH:MM:SS"}``. Interpretation is the
        coordinator's job.
        """
        url = (
            f"{API_HOST}/api/v4/system/summary/summary"
            f"?system_id={system_id}&date={date}&temp={metric}&uid={cca_uid}"
            f"&resourceId=data-{date}-{metric}-{cca_uid}&_={_cache_buster()}"
        )
        return await self._request_json("GET", url)

    async def probe_extra_hardware(self, system_id: int) -> dict[str, dict]:
        """Hit hardware-gated endpoints and record status + body snippet.

        For users with monitored inverters / meters / batteries (hardware we
        lack) to capture and share so v4 support can be extended. Never
        raises; 403/404/422 are expected and informative.
        """
        sid = system_id
        endpoints = {
            "inverters_list_v3": f"{API_HOST}/api/v3/inverters/list?system_id={sid}",
            "sources_list_v3": f"{API_HOST}/api/v3/sources/list?system_id={sid}",
            "generator_list_v3": f"{API_HOST}/api/v3/generator/list?system_id={sid}",
            "equipment_status_summary": f"{API_HOST}/api/v4/equipment-status/summary?systemId={sid}",
            "equipment_status_latest": f"{API_HOST}/api/v4/equipment-status/latest?systemId={sid}",
            "batteries": f"{API_HOST}/api/v4/equipments/{sid}/batteries",
            "data_aggregate_solar": (
                f"{API_HOST}/api/v4/data/aggregate?view=solar&systemId={sid}"
                f"&type=bar&agg=hour"
            ),
            "heat_pumps": f"{API_HOST}/api/v4/heat-pumps?systemId={sid}",
        }
        result: dict[str, dict] = {}
        for name, url in endpoints.items():
            try:
                async with self._session.get(
                    url, headers=self._auth_headers
                ) as resp:
                    body = await resp.text()
                    status = resp.status
            except aiohttp.ClientError as err:
                result[name] = {"status": None, "error": str(err)}
                log_raw(f"PROBE {name}", url, f"<error {err}>")
                continue
            snippet = body[:1500]
            result[name] = {"status": status, "snippet": snippet}
            log_raw(f"PROBE {name} [{status}]", url, snippet)
        return result

    async def get_agg_energy(self, system_id: int, date: str) -> dict:
        """GET /api/v4/system/summary/aggenergy -> per-object_id daily Wh.

        Returns ``{"dataset":{object_id:Wh},"datasetLastData":{…},
        "dailyStats":{"total_agg_energy":Wh,"total_agg_reclaimed":Wh},…}``.
        """
        url = (
            f"{API_HOST}/api/v4/system/summary/aggenergy"
            f"?system_id={system_id}&date={date}&temp=energy"
            f"&resourceId=data-{date}-energy&_={_cache_buster()}"
        )
        return await self._request_json("GET", url)
