from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.const import CONF_IP_ADDRESS

from .const import (
    DOMAIN,
    SCAN_INTERVAL,                 # compat
    OPT_SCAN_INTERVAL,
    SCAN_INTERVAL_DEFAULT_SEC,
    CLOUD_SCAN_INTERVAL_DEFAULT_SEC,
    CLOUD_SCAN_INTERVAL_MIN_SEC,
    SOURCE_CLOUD,
    SOURCE_ESP,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SYSTEM_ID,
    _LOGGER,
)
from .tigo_api import fetch_tigo_data_from_ip, fetch_tigo_data_from_ws


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
        or entry.title
    )
    source = entry.options.get("source") or entry.data.get("source") or "CCA"

    cloud_client = None
    cloud_layout: dict = {}

    if source == SOURCE_CLOUD:
        from .tigo_cloud import TigoCloudClient

        username = entry.options.get(CONF_USERNAME) or entry.data.get(CONF_USERNAME)
        password = entry.options.get(CONF_PASSWORD) or entry.data.get(CONF_PASSWORD)
        system_id = entry.data.get(CONF_SYSTEM_ID)
        _LOGGER.debug("Using CLOUD source for Tigo system %s", system_id)

        cloud_client = TigoCloudClient(username, password, system_id)
        # Layout statico: letto una volta al setup (login incluso automaticamente)
        try:
            cloud_layout = await hass.async_add_executor_job(cloud_client.fetch_layout)
        except Exception as e:
            _LOGGER.warning("Layout cloud non disponibile al setup: %s", e)
            cloud_layout = {}

        def _sync_fetch() -> dict:
            return cloud_client.fetch_all(cloud_layout)
        label = f"CLOUD {system_id}"
    elif source == SOURCE_ESP:
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

    # Default e minimo dipendono dalla sorgente: il cloud usa un intervallo più
    # ampio (dati non realtime, ~15 min) e un floor anti-throttle.
    is_cloud = source == SOURCE_CLOUD
    default_scan = CLOUD_SCAN_INTERVAL_DEFAULT_SEC if is_cloud else SCAN_INTERVAL_DEFAULT_SEC

    def _clamp_scan(value: int) -> int:
        return max(value, CLOUD_SCAN_INTERVAL_MIN_SEC) if is_cloud else value

    # --- nuovo: leggi lo scan interval dalle opzioni (fallback al default per sorgente) ---
    scan_seconds = _clamp_scan(int(
        entry.options.get(
            OPT_SCAN_INTERVAL,
            entry.data.get(OPT_SCAN_INTERVAL, default_scan),
        )
    ))
    update_interval = timedelta(seconds=scan_seconds) if scan_seconds > 0 else SCAN_INTERVAL

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Tigo Panel Data ({ip_address})",
        update_method=_async_update_method,
        update_interval=update_interval,
    )
    coordinator.data_source = source

    # --- nuovo: listener delle opzioni per applicare subito il nuovo intervallo ---
    async def _options_updated(hass: HomeAssistant, updated_entry: ConfigEntry) -> None:
        new_scan = _clamp_scan(int(
            updated_entry.options.get(
                OPT_SCAN_INTERVAL,
                updated_entry.data.get(OPT_SCAN_INTERVAL, default_scan),
            )
        ))
        coordinator.update_interval = timedelta(seconds=new_scan)
        _LOGGER.info("⏱️ Tigo scan interval aggiornato a %ss", new_scan)
        # opzionale: triggera un refresh immediato
        await coordinator.async_request_refresh()

    entry.async_on_unload(entry.add_update_listener(_options_updated))

    # Primo bootstrap con retry progressivo (come tua versione)
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
        "source": source,
        "system_id": entry.data.get(CONF_SYSTEM_ID),
        "cloud_client": cloud_client,
        "cloud_layout": cloud_layout,
    }
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
