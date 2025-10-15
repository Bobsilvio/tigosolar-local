import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from datetime import timedelta
from homeassistant.const import CONF_IP_ADDRESS

from .const import DOMAIN
from .tigo_api import fetch_tigo_data_from_ip, fetch_tigo_data_from_ws

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=60)

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
        update_method = lambda: fetch_tigo_data_from_ws(f"ws://{ip_address}/ws")
    else:
        _LOGGER.debug("Using CCA IP source for Tigo at %s", ip_address)
        update_method = lambda: fetch_tigo_data_from_ip(ip_address)
    

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Tigo Panel Data ({ip_address})",
        update_method=lambda: hass.async_add_executor_job(update_method),
        update_interval=SCAN_INTERVAL,
    )
    coordinator.data_source = source
    await coordinator.async_config_entry_first_refresh()

    panel_data = coordinator.data or {}
    if not panel_data:
        _LOGGER.warning("Nessun dato disponibile dal coordinatore")
    
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
