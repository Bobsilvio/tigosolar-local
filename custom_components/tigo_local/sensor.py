from __future__ import annotations
import logging
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import async_get
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .tigo_api import fetch_tigo_data_from_ip, fetch_tigo_layout_from_ip, fetch_daily_energy

from homeassistant.const import (
    UnitOfPower,
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
)

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)

_LOGGER = logging.getLogger(__name__)

PANEL_PROPERTIES = {
    "Pin": {
        "name": "Power",
        "native_unit_of_measurement": UnitOfPower.WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:solar-power",
    },
    "Vin": {
        "name": "Voltage",
        "native_unit_of_measurement": UnitOfElectricPotential.VOLT,
        "device_class": SensorDeviceClass.VOLTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:flash-triangle",
    },
    "Rssi": {
        "name": "Signal Strength",
        "native_unit_of_measurement": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:signal-variant",
    },
    "Iin": {
        "name": "Current",
        "native_unit_of_measurement": UnitOfElectricCurrent.AMPERE,
        "device_class": SensorDeviceClass.CURRENT,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:current-ac",
    },
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    _LOGGER.debug("Setting up Tigo local-only sensors")

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    panel_data = coordinator.data
    ip_address = hass.data[DOMAIN][entry.entry_id]["ip"]

    layout = await hass.async_add_executor_job(fetch_tigo_layout_from_ip, ip_address)
    layout_map = {}
    for inverter in layout.get("system", {}).get("inverters", []):
        layout_map[inverter.get("object_id")] = inverter
        for mppt in inverter.get("mppts", []):
            layout_map[mppt.get("object_id")] = mppt
            for panel in mppt.get("panels", []):
                layout_map[panel["object_id"]] = panel
    
    

    def resolve_parents(panel_id: str, layout_map: dict) -> dict:
        string_label = None
        inverter_label = None
        current_id = panel_id

        for _ in range(2):
            current = layout_map.get(current_id)
            if not current or "parent" not in current:
                break
            parent_id = current["parent"]
            parent = layout_map.get(parent_id)
            if not parent:
                break
            if parent.get("tl") == "String":
                string_label = parent.get("label")
            elif parent.get("tl") == "Inverter":
                inverter_label = parent.get("label")
            current_id = parent_id

        return {
            "string": string_label,
            "inverter": inverter_label,
        }
    

    entities = []

    for panel_id, data in panel_data.items():
        layout_info = layout_map.get(panel_id, {})
        parent_info = resolve_parents(panel_id, layout_map)
        

        for param, prop in PANEL_PROPERTIES.items():
            if param in data:
                entities.append(
                    TigoPanelSensor(
                        coordinator,
                        panel_id,
                        param,
                        layout_info,
                        parent_info,
                        **prop
                    )
                )

    async def fetch_energy_data():
        raw = await hass.async_add_executor_job(fetch_daily_energy, ip_address)
        history_list = raw.get("history", [])
        last_day = history_list[-1][1] if history_list else None
        last_7_days = sum(val for _, val in history_list[-7:]) if history_list else None
        return {
            "yesterday_energy": last_day,
            "weekly_energy": last_7_days,
            "history": history_list,
        }
    

    system_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Tigo Local System Energy",
        update_method=fetch_energy_data,
        update_interval=timedelta(minutes=10),
    )

    await system_coordinator.async_config_entry_first_refresh()

    entities.append(
        TigoSystemSensor(
            "Tigo Yesterday Energy",
            "yesterday_energy",
            UnitOfEnergy.WATT_HOUR,
            "tigo_system_yesterday",
            system_coordinator,
        )
    )

    entities.append(
        TigoSystemSensor(
            "Tigo Last 7 Days Energy",
            "weekly_energy",
            UnitOfEnergy.WATT_HOUR,
            "tigo_system_weekly",
            system_coordinator,
        )
    )

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "tigo_system")},
        manufacturer="Tigo",
        name="Tigo Local System",
        model="Gateway",
    )

    async_add_entities(entities)


class TigoPanelSensor(CoordinatorEntity, SensorEntity):
    def __init__(
        self,
        coordinator,
        panel_id,
        param,
        layout_info,
        parent_info,
        name,
        native_unit_of_measurement,
        device_class,
        state_class,
        icon,
    ):
        super().__init__(coordinator)
        self._panel_id = panel_id
        self._param = param
        self._layout = layout_info or {}
        self._parent_info = parent_info or {}
        self._attr_name = f"Tigo {panel_id} {name}"
        self._attr_unique_id = f"tigo_{panel_id}_{param.lower()}"
        self._attr_native_unit_of_measurement = native_unit_of_measurement
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_icon = icon
        
        
        serial = self._layout.get("serial", panel_id)
        channel = self._layout.get("channel", "unknown")
        #_LOGGER.debug("Seriale: %s- Channel: %s", serial, channel)

        connections = None
        if "." in channel:
            mac = channel.split(".")[0].lower()
            if len(mac) == 12:
                connections = {("mac", ":".join([mac[i:i+2] for i in range(0, 12, 2)]))}

        self._attr_device_info = {
            "identifiers": {(DOMAIN, panel_id)},
            "name": f"Panel {panel_id}",
            "manufacturer": "Tigo",
            "model": self._layout.get("type", "Tigo Panel"),
            "sw_version": serial,
            "hw_version": channel,
            "via_device": (DOMAIN, "tigo_system"),
        }

#        if connections:
#            self._attr_device_info["connections"] = connections

        area = parent_info.get("string") or parent_info.get("inverter")
        if area:
            self._attr_device_info["suggested_area"] = area
        
        _LOGGER.debug("Creating sensor: %s | ID: %s | Param: %s", self._attr_name, panel_id, param)
        _LOGGER.debug("Device identifiers: %s", self._attr_device_info["identifiers"])

    @property
    def native_value(self):
        value = self.coordinator.data.get(self._panel_id, {}).get(self._param)
        try:
            return round(float(value), 2) if value is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self):
        return {
            "label": self._layout.get("label"),
            "serial": self._layout.get("serial"),
            "channel": self._layout.get("channel"),
            "param": self._param,
#            "inverter": self._parent_info.get("inverter"),
#            "string": self._parent_info.get("string"),
            "mp": self._layout.get("MP"),
        }


class TigoSystemSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, name, key, unit, unique_id, coordinator):
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_native_unit_of_measurement = unit
        self._key = key
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "tigo_system")},
            "name": "Tigo Local System",
            "manufacturer": "Tigo",
        }

    @property
    def native_value(self):
        return self.coordinator.data.get(self._key)

    @property
    def extra_state_attributes(self):
        return self.coordinator.data.get("history", {})
