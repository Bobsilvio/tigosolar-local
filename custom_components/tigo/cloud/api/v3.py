"""Async client for the legacy Tigo v3 cloud API (api2.tigoenergy.com).

This is a faithful async port of the original synchronous ``tigo_api.py`` plus
the per-panel CSV fetch/parse that previously lived in ``__init__.py``. No
endpoint, parameter or data shape has changed in this commit -- it only moves
the logic behind a client that uses Home Assistant's shared aiohttp session
instead of ``requests`` in an executor / a per-call ``aiohttp.ClientSession``.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .base import BaseTigoClient
from .errors import TigoApiError, TigoAuthError

_LOGGER = logging.getLogger(__name__)

API_BASE_URL = "https://api2.tigoenergy.com/api/v3"

# Per-panel parameters fetched from /data/aggregate (param -> unit, unit unused
# here but kept to document the contract the sensors rely on).
PARAMS = {
    "Pin": "W",
    "Vin": "V",
    "Iin": "A",
    "RSSI": "dBm",
}


def parse_param_csv(csv_text: str, param: str, latest_only: bool = False) -> dict[str, float]:
    """Parse a /data/aggregate CSV body into ``{panel_id: value}``."""
    result: dict[str, float] = {}
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if len(rows) < 2:
        return {}

    headers = rows[0][1:]  # skip the Datetime column
    values_rows = rows[1:]

    if latest_only:
        # Latest valid value per column: with sensors=true the CCA sensor
        # column has newer timestamps than panels (~15 min cloud delay),
        # so a single "latest row" only ever contains the sensor — no panels.
        for row in reversed(values_rows):
            for panel_id, value in zip(headers, row[1:]):
                if str(panel_id) in result or value.strip() in ("", "NaN"):
                    continue
                try:
                    result[str(panel_id)] = float(value)
                except Exception:
                    continue
            if len(result) == len(headers):
                break
        return result
    else:
        values = values_rows[0][1:]

    for panel_id, value in zip(headers, values):
        try:
            result[str(panel_id)] = float(value)
        except Exception:
            continue

    return result


class TigoV3Client(BaseTigoClient):
    """Minimal async wrapper over the Tigo v3 endpoints used by the integration."""

    api_version = "v3"

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    async def _get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    raise TigoAuthError(f"Tigo API unauthorized: GET {url}")
                if resp.status != 200:
                    raise TigoApiError(f"Tigo API error {resp.status}: GET {url}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise TigoApiError(f"Tigo API request failed: GET {url}: {err}") from err

    async def _do_login(self, email: str, password: str) -> str:
        """HTTP Basic-auth login; stores and returns the bearer token."""
        url = f"{API_BASE_URL}/users/login"
        try:
            async with self._session.get(
                url, auth=aiohttp.BasicAuth(email, password)
            ) as resp:
                if resp.status in (401, 403):
                    raise TigoAuthError("Tigo login failed: invalid credentials")
                if resp.status != 200:
                    raise TigoApiError(f"Tigo login error {resp.status}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise TigoApiError(f"Tigo login request failed: {err}") from err

        try:
            self.token = data["user"]["auth"]
        except (KeyError, TypeError) as err:
            raise TigoApiError(f"Tigo login response missing token: {data!r}") from err
        return self.token

    async def get_system_id(self) -> int:
        data = await self._get_json(f"{API_BASE_URL}/systems", headers=self._auth_headers)
        systems = data.get("systems", []) if isinstance(data, dict) else []
        if not systems:
            raise TigoApiError("No systems found for this account")
        return systems[0]["system_id"]

    async def get_system_layout(self, system_id: int) -> dict:
        return await self._get_json(
            f"{API_BASE_URL}/systems/layout?id={system_id}",
            headers=self._auth_headers,
        )

    async def get_systems(self) -> list[dict]:
        data = await self._get_json(
            f"{API_BASE_URL}/systems", headers=self._auth_headers
        )
        return data.get("systems", []) if isinstance(data, dict) else []

    async def get_system_info(self, system_id: int, date: str | None = None) -> dict:
        # v3 has no premium/feature/sunrise info; return empty so the
        # coordinator falls back to safe defaults (no night-skip, v3
        # metrics fetched regardless of premium flags).
        return {}

    async def get_capabilities(self, system_id: int) -> dict:
        return {}

    # -- coordinator-compatible adapters (coarse: latest value only) -- #
    _METRIC_PARAM = {
        "pin": "Pin",
        "vin": "Vin",
        "iin": "Iin",
        "rssi": "RSSI",
    }

    async def get_equipments(self, system_id: int) -> list[dict]:
        """Synthesize the v4 equipments list from the v3 layout tree.

        Order = layout panel order; equipmentSerial carries the per-panel
        object_id so topology can map aggregate values (which use header=id).
        """
        layout = await self.get_system_layout(system_id)
        system = layout.get("system", {}) if isinstance(layout, dict) else {}
        out: list[dict] = []
        for inv in system.get("inverters", []) or []:
            for mppt in inv.get("mppts", []) or []:
                for string in mppt.get("strings", []) or []:
                    for panel in string.get("panels", []) or []:
                        out.append(
                            {
                                "equipmentId": panel.get("label")
                                or str(panel.get("object_id")),
                                "equipmentType": "panel",
                                "equipmentSerial": panel.get("serial"),
                                "equipmentModel": panel.get("type"),
                                "_object_id": str(panel.get("object_id")),
                            }
                        )
        self._equip_object_ids = [e["_object_id"] for e in out]
        return out

    async def _fetch_param_latest(
        self, system_id: int, param: str
    ) -> dict[str, float]:
        today = datetime.now(timezone.utc).date()
        start = datetime.combine(
            today, datetime.min.time(), tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        end = datetime.combine(
            today, datetime.max.time(), tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        url = (
            f"{API_BASE_URL}/data/aggregate"
            f"?system_id={system_id}&start={start}&end={end}&level=min"
            f"&param={param}&header=id&sensors=true"
        )
        try:
            async with self._session.get(
                url, headers={"Authorization": f"Bearer {self.token}"}
            ) as resp:
                if resp.status == 401:
                    raise TigoAuthError(f"Tigo v3 unauthorized [{param}]")
                if resp.status != 200:
                    raise TigoApiError(
                        f"Tigo v3 error [{param}]: {resp.status}",
                        status=resp.status,
                    )
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise TigoApiError(f"Tigo v3 request failed [{param}]: {err}") from err
        return parse_param_csv(text, param, latest_only=True)

    async def get_panel_summary(
        self, system_id: int, date: str, metric: str, cca_uid: str
    ) -> dict:
        """v4-summary-shaped payload with a single latest-minute row."""
        param = self._METRIC_PARAM.get(metric, metric)
        values = await self._fetch_param_latest(system_id, param)
        if not getattr(self, "_equip_object_ids", None):
            await self.get_equipments(system_id)
        order = self._equip_object_ids
        now = datetime.now()
        row_d = [values.get(oid, "-") for oid in order]
        return {
            "dataType": metric,
            "dataset": [{"data": [{"t": now.strftime("%H:%M"), "d": row_d}]}],
            "lastData": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def get_agg_energy(self, system_id: int, date: str) -> dict:
        """System daily energy only (v3 has no per-panel daily energy here)."""
        summary = await self.get_system_summary(system_id)
        daily_kwh = (
            summary.get("daily_energy_dc")
            or summary.get("daily_energy")
            or 0.0
        )
        return {
            "dataset": {},
            "dailyStats": {"total_agg_energy": float(daily_kwh) * 1000.0},
        }

    async def get_system_summary(self, system_id: int) -> dict[str, float]:
        data = await self._get_json(
            f"{API_BASE_URL}/data/summary?system_id={system_id}",
            headers=self._auth_headers,
        )
        raw = data.get("summary", {}) if isinstance(data, dict) else {}
        clean: dict[str, float] = {}
        for key, val in raw.items():
            try:
                if isinstance(val, (int, float)):
                    if "energy" in key.lower():
                        clean[key] = round(val / 1000, 2)  # Wh -> kWh
                    else:
                        clean[key] = round(val, 2)
            except Exception as err:  # pragma: no cover - defensive, matches original
                _LOGGER.warning("Error parsing summary key %s=%s: %s", key, val, err)
                continue
        return clean

    async def fetch_panel_data(self, system_id: int) -> dict[str, dict[str, float]]:
        """Return ``{panel_id: {param: value}}`` for the current UTC day.

        Behaviour preserved from the original ``__init__.fetch_tigo_data``
        (including the UTC day window -- timezone handling is addressed later
        in the coordinator commit).
        """
        today = datetime.now(timezone.utc).date()
        start = datetime.combine(
            today, datetime.min.time(), tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        end = datetime.combine(
            today, datetime.max.time(), tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")

        combined: dict[str, dict[str, float]] = {}
        for param in PARAMS:
            url = (
                f"{API_BASE_URL}/data/aggregate"
                f"?system_id={system_id}&start={start}&end={end}&level=min"
                f"&param={param}&header=id&sensors=true"
            )
            _LOGGER.debug("Fetching v3 param %s: %s", param, url)
            try:
                async with self._session.get(
                    url, headers={"Authorization": f"Bearer {self.token}"}
                ) as resp:
                    if resp.status == 401:
                        raise TigoAuthError(f"Tigo API unauthorized [{param}]")
                    if resp.status != 200:
                        raise TigoApiError(
                            f"Tigo API error [{param}]: {resp.status}"
                        )
                    text = await resp.text()
            except aiohttp.ClientError as err:
                raise TigoApiError(f"Tigo API request failed [{param}]: {err}") from err

            for panel_id, value in parse_param_csv(
                text, param, latest_only=True
            ).items():
                combined.setdefault(panel_id, {})[param] = value

        return combined
