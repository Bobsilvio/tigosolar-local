"""Diagnostica scaricabile (dati sensibili oscurati)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, SOURCE_CLOUD


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if store.get("source") == SOURCE_CLOUD:
        from .cloud.diagnostics import async_get_config_entry_diagnostics as cloud_diag

        return await cloud_diag(hass, entry)

    coordinator = store.get("coordinator")
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), {"password", "username", "email"}),
            "options": async_redact_data(dict(entry.options), {"password", "username", "email"}),
            "version": entry.version,
        },
        "source": store.get("source"),
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "data_sample": getattr(coordinator, "data", None),
    }
