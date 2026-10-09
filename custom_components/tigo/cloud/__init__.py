"""Sorgente Cloud di Tigo (motore ex tigosolar-online).

API cloud v4 (``mapi.tigoenergy.com``, la stessa dell'app) con fallback v3,
gestione token, opzione Premium, backoff/throttle e Repair in caso di outage.
Usata per CCA con firmware >= 4.0.4 (endpoint locali protetti) o per chi non
ha accesso locale al CCA.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import SOURCE_CLOUD
from .api import API_AUTO, TigoAuthError, async_create_client
from .const import CONF_API_VERSION, CONF_VERBOSE_LOGGING, DOMAIN
from .coordinator import TigoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_cloud_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Login, scelta impianto, primo refresh. Popola hass.data per le piattaforme."""
    opts = {**entry.data, **entry.options}
    if opts.get(CONF_VERBOSE_LOGGING):
        logging.getLogger("custom_components.tigo").setLevel(logging.DEBUG)

    session = async_get_clientsession(hass)

    def _store_token(state: dict[str, Any]) -> None:
        if state.get("token") and state.get("token") != entry.data.get("token"):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, **state}
            )

    try:
        client = await async_create_client(
            session,
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
            api_pref=opts.get(CONF_API_VERSION, API_AUTO),
            token_store=_store_token,
        )
        systems = await async_resolve_systems(client)
        system_id = _pick_system(entry, systems)
    except TigoAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Tigo setup failed: {err}") from err

    coordinator = TigoDataUpdateCoordinator(hass, entry, client, system_id)
    coordinator.seed_from_restore()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "source": SOURCE_CLOUD,
        "client": client,
        "coordinator": coordinator,
        "system_id": system_id,
    }


async def async_resolve_systems(client) -> list[dict]:
    """Return [{system_id,...}] across v3/v4 client shapes."""
    if hasattr(client, "get_systems"):
        try:
            systems = await client.get_systems()
            if systems:
                return systems
        except Exception:  # noqa: BLE001
            pass
    sid = await client.get_system_id()
    return [{"system_id": sid}]


def _pick_system(entry: ConfigEntry, systems: list[dict]) -> int:
    want = entry.data.get("system_id")
    if want is not None:
        return int(want)
    if not systems:
        raise ConfigEntryNotReady("No Tigo systems on this account")
    first = systems[0]
    return int(first.get("system_id") or first.get("id"))
