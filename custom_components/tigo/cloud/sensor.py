"""Tigo sensor entities (v4 data model).

Device tree: System -> Inverter -> String -> Panel (via_device chain).
Unmonitored inverters get a device for organization but no sensors.

Default per-panel entities: Power + Energy. Voltage/Current/RSSI are created
only when enabled in options (the coordinator also only fetches those then).

Energy sensors are RestoreSensor (so HA stores their state on shutdown) and
expose ``today_kwh``: on startup the coordinator seeds its monotonic
accumulators from the last stored state, before the first refresh, so the
Energy Dashboard ``total_increasing`` series survives restarts.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..compat import via_device_kwargs
from .const import (
    DOMAIN,
    METRIC_IIN,
    METRIC_PIN,
    METRIC_RECLAIMED,
    METRIC_RSSI,
    METRIC_VIN,
)

_LOGGER = logging.getLogger(__name__)

# metric -> (suffix, name, unit, device_class, opt-in)
PANEL_METRICS = {
    METRIC_PIN: ("power", "Power", UnitOfPower.WATT,
                 SensorDeviceClass.POWER, False),
    METRIC_VIN: ("voltage", "Voltage", UnitOfElectricPotential.VOLT,
                 SensorDeviceClass.VOLTAGE, True),
    METRIC_IIN: ("current", "Current", UnitOfElectricCurrent.AMPERE,
                 SensorDeviceClass.CURRENT, True),
    METRIC_RSSI: ("rssi", "Signal Strength", SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                  None, True),
    METRIC_RECLAIMED: ("reclaimed", "Reclaimed Power", UnitOfPower.WATT,
                       SensorDeviceClass.POWER, True),
}


def _sys_device(system_id) -> dict:
    return {
        "identifiers": {(DOMAIN, str(system_id))},
        "name": f"System {system_id}",
        "manufacturer": "Tigo",
        "model": "System",
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    system_id = data["system_id"]
    topo = coordinator.topology

    entities: list[SensorEntity] = [
        TigoSystemPower(coordinator, system_id),
        TigoSystemProductionToday(coordinator, system_id),
        TigoSystemProduction(coordinator, system_id),
    ]

    # The system and inverter devices carry no entities of their own, and Home
    # Assistant only creates a device when an entity declares it. Register them
    # up front, parents first, so the System -> Inverter -> Panel chain resolves.
    device_registry = dr.async_get(hass)
    sys_ident = (DOMAIN, str(system_id))
    sys_dev = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, **_sys_device(system_id)
    )

    # Inverter devices (incl. unmonitored: organization only, no sensors).
    inv_parent: dict[object, tuple] = {}
    for inv in (topo.inverters if topo else []):
        ident = (DOMAIN, f"inv_{inv.inverter_id}")
        inv_dev = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={ident},
            name=inv.label,
            manufacturer="Tigo",
            model="Inverter" + ("" if inv.is_monitored else " (unmonitored)"),
            **via_device_kwargs(sys_dev, sys_ident),
        )
        inv_parent[inv.inverter_id] = (inv_dev, ident)

    for meta in (topo.panels if topo else []):
        parent_dev, parent_ident = inv_parent.get(meta.inverter_id, (sys_dev, sys_ident))
        device = {
            "identifiers": {(DOMAIN, str(meta.object_id or meta.equipment_id))},
            "name": f"Panel {meta.label}",
            "manufacturer": "Tigo",
            "model": meta.model,
            "suggested_area": meta.string_label,
            **via_device_kwargs(parent_dev, parent_ident),
        }
        for metric, (suffix, mname, unit, dclass, optin) in PANEL_METRICS.items():
            # All metrics are always registered. Power is enabled by
            # default; Voltage/Current/RSSI are registered but
            # disabled-by-default (optin=True) so they are discoverable
            # and can be enabled per-entity in the UI. The coordinator
            # starts fetching a metric as soon as one of its entities is
            # enabled (or the matching Options toggle is set).
            entities.append(
                TigoPanelSensor(
                    coordinator,
                    meta,
                    metric,
                    suffix,
                    mname,
                    unit,
                    dclass,
                    device,
                    enabled_default=not optin,
                )
            )
        if meta.object_id:
            entities.append(TigoPanelEnergy(coordinator, meta, device))

    _LOGGER.debug("Adding %d Tigo entities", len(entities))
    async_add_entities(entities)


class _Base(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, system_id) -> None:
        super().__init__(coordinator)
        self._system_id = system_id


class TigoSystemPower(_Base):
    _attr_name = "Current Power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, system_id) -> None:
        super().__init__(coordinator, system_id)
        self._attr_unique_id = f"tigo_{system_id}_current_power"
        self._attr_device_info = _sys_device(system_id)

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("system", {}).get("power_w")


class TigoSystemProductionToday(_Base):
    _attr_name = "Production Today"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, system_id) -> None:
        super().__init__(coordinator, system_id)
        self._attr_unique_id = f"tigo_{system_id}_production_today"
        self._attr_device_info = _sys_device(system_id)

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("system", {}).get(
            "today_energy_kwh"
        )


class TigoSystemProduction(_Base, RestoreSensor):
    """Monotonic lifetime kWh -> the Energy Dashboard 'Solar production' source."""

    _attr_name = "Production"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, system_id) -> None:
        super().__init__(coordinator, system_id)
        self._attr_unique_id = f"tigo_{system_id}_production"
        self._attr_device_info = _sys_device(system_id)

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("system", {}).get(
            "lifetime_energy_kwh"
        )

    @property
    def extra_state_attributes(self):
        # Usato al riavvio per riprendere il contatore senza doppio conteggio.
        return {
            "today_kwh": (self.coordinator.data or {}).get("system", {}).get(
                "today_energy_kwh"
            )
        }


class TigoPanelSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator,
        meta,
        metric,
        suffix,
        name,
        unit,
        dclass,
        device,
        enabled_default: bool = True,
    ) -> None:
        super().__init__(coordinator)
        self._meta = meta
        self._metric = metric
        self._attr_name = name
        # Preserve history continuity with the v1 unique_id scheme where
        # possible: tigo_{object_id}_{param}.
        base = meta.object_id or meta.equipment_id
        self._attr_unique_id = f"tigo_{base}_{suffix}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = dclass
        self._attr_device_info = device
        self._attr_entity_registry_enabled_default = enabled_default
        if metric == METRIC_RSSI:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        panels = (self.coordinator.data or {}).get("panels", {})
        return panels.get(self._meta.equipment_id, {}).get(self._metric)

    @property
    def extra_state_attributes(self):
        return {
            "equipment_id": self._meta.equipment_id,
            "serial": self._meta.serial,
            "model": self._meta.model,
            "inverter": self._meta.inverter_label,
            "string": self._meta.string_label,
            "full_label": self._meta.full_label,
        }


class TigoPanelEnergy(CoordinatorEntity, RestoreSensor):
    _attr_has_entity_name = True
    _attr_name = "Energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, meta, device) -> None:
        super().__init__(coordinator)
        self._meta = meta
        self._attr_unique_id = f"tigo_{meta.object_id}_energy"
        self._attr_device_info = device

    @property
    def native_value(self):
        panels = (self.coordinator.data or {}).get("panels", {})
        return panels.get(self._meta.equipment_id, {}).get(
            "energy_kwh_lifetime"
        )

    @property
    def extra_state_attributes(self):
        # Usato al riavvio per riprendere il contatore senza doppio conteggio.
        panels = (self.coordinator.data or {}).get("panels", {})
        return {
            "today_kwh": panels.get(self._meta.equipment_id, {}).get(
                "energy_kwh_today"
            )
        }
