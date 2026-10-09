"""Binary sensor: solo la sorgente Cloud (connettività API Tigo)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SOURCE_CLOUD


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if hass.data[DOMAIN][entry.entry_id].get("source") != SOURCE_CLOUD:
        return
    from .cloud.binary_sensor import async_setup_entry as async_setup_cloud

    await async_setup_cloud(hass, entry, async_add_entities)
