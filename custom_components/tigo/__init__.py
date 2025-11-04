from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.const import CONF_IP_ADDRESS

from .const import DOMAIN
from .tigo_api import fetch_tigo_data_from_ip, fetch_tigo_data_from_ws

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)

def _with_retries(fn: Callable[[], dict], label: str, attempts: int = 5, base_sleep: int = 15) -> dict:
    last_err = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            wait = min(base_sleep * i, 60)
            _LOGGER.debug("[%s] tentativo %d/%d fallito: %s → ritento tra %ss", label, i, attempts, e, wait)
            if i < attempts:
                import time as _t
                _t.sleep(wait)
    _LOGGER.warning("[%s] ancora non raggiungibile dopo %d tentativi: %s", label, attempts, last_err)
    raise last_err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ip_address = (
        entry.options.get(CONF_IP_ADDRESS)
        or entry.data.get(CONF_IP_ADDRESS)
        or entry.data.get("host")
        or entry.title  # fallback
    )

    source = entry.options.get("source") or entry.data.get("source") or "CCA"

    if source == "ESP32_WS":
        _LOGGER.debug("Using WebSocket source for Tigo at %s", ip_address)
        def _sync_fetch() -> dict:
            return fetch_tigo_data_from_ws(f"ws://{ip_address}/ws")
        label = f"ESP32_WS {ip_address}"
    else:
        _LOGGER.debug("Using CCA IP source for Tigo at %s", ip_address)
        def _sync_fetch() -> dict:
            return fetch_tigo_data_from_ip(ip_address)
        label = f"CCA {ip_address}"

    async def _async_update_method() -> dict:
        return await hass.async_add_executor_job(_with_retries, _sync_fetch, label)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Tigo Panel Data ({ip_address})",
        update_method=_async_update_method,
        update_interval=SCAN_INTERVAL,
    )
    coordinator.data_source = source

    max_first_attempts = 6
    first_delay = 10
    success = False
    for i in range(1, max_first_attempts + 1):
        await coordinator.async_refresh()
        if coordinator.last_update_success:
            success = True
            _LOGGER.info("✅ Tigo primo refresh riuscito (tentativo %d/%d)", i, max_first_attempts)
            break
        wait = min(first_delay * i, 60)
        _LOGGER.warning(
            "Tigo non raggiungibile al primo avvio (tentativo %d/%d). "
            "Probabile standby notturno. Riprovo tra %ss…",
            i, max_first_attempts, wait
        )
        await asyncio.sleep(wait)

    if not success:
        _LOGGER.warning(
            "⚠️ Proseguo il setup di Tigo anche senza dati iniziali. "
            "I sensori verranno creati e si aggiorneranno automaticamente appena il CCA/ESP si sveglia."
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "ip": ip_address,
    }
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
