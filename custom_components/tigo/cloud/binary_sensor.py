"""Tigo cloud connectivity binary sensor.

Reflects the coordinator's ``last_update_success`` so users can see (and
automate on) Tigo cloud outages at a glance. Diagnostic, attached to the
system device. Forwarded as a platform by the entity-model commit.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    system_id = data["system_id"]
    async_add_entities([TigoApiConnectivity(coordinator, system_id)])


class TigoApiConnectivity(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Tigo API"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, system_id) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"tigo_{system_id}_api_connectivity"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(system_id))},
            "name": f"System {system_id}",
            "manufacturer": "Tigo",
            "model": "System",
        }

    @property
    def available(self) -> bool:
        # The connectivity sensor must stay available to report "off".
        return True

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.last_update_success)
