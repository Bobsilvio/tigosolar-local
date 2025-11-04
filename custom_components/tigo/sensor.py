from __future__ import annotations
import logging
from datetime import timedelta, datetime
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import async_get
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

import calendar

from .const import DOMAIN
from .tigo_api import fetch_tigo_data_from_ip, fetch_tigo_layout_from_ip, fetch_daily_energy, fetch_device_info

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
    "Temp": {
        "name": "Temperature",
        "native_unit_of_measurement": "°C",
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:thermometer",
    },
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    _LOGGER.debug("Setting up Tigo local-only sensors")

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    panel_data = coordinator.data
    ip_address = hass.data[DOMAIN][entry.entry_id]["ip"]

    source = getattr(coordinator, "data_source", entry.options.get("source", "CCA"))
    _LOGGER.debug("Detected Tigo source: %s", source)

    safe_ip = ip_address.replace(".", "")
    cca_prefix = f"{source[:3].lower()}_{safe_ip}"
    _LOGGER.debug("Using stable prefix based on IP: %s", cca_prefix)
    

    if source == "CCA":
        try:
            layout = await hass.async_add_executor_job(fetch_tigo_layout_from_ip, ip_address)
        except Exception as e:
            _LOGGER.warning("Errore fetch_tigo_layout_from_ip: %s", e)
            layout = {}
    else:
        layout = {"system": {"inverters": []}}
        _LOGGER.debug("Skipping layout fetch for ESP32 (using empty layout)")
    
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
    panel_data = coordinator.data or {}
    source = getattr(coordinator, "data_source", entry.data.get("source", "CCA"))
    energy_entities_by_panel = {}

    for panel_id, data in panel_data.items():
        if not isinstance(data, dict):
            continue

        layout_info = layout_map.get(panel_id, {})
        parent_info = resolve_parents(panel_id, layout_map)

        # sensori standard
        for param, prop in PANEL_PROPERTIES.items():
            if param == "Temp" and source != "ESP32_WS":
                continue
            if param in data:
                entities.append(
                    TigoPanelSensor(
                        coordinator,
                        panel_id,
                        param,
                        layout_info,
                        parent_info,
                        cca_prefix,
                        **prop
                    )
                )

        total_energy = TigoPanelEnergy(coordinator, panel_id, layout_info, parent_info, cca_prefix)
        entities.append(total_energy)
        entities.append(TigoPanelPeriodEnergy(coordinator, "day", total_energy, panel_id, cca_prefix))
        entities.append(TigoPanelPeriodEnergy(coordinator, "month", total_energy, panel_id, cca_prefix))

    device_registry = dr.async_get(hass)

    if "esp" in cca_prefix.lower():
        device_model = "ESP32"
        device_name = f"Tigo ESP32 System ({ip_address})"
    elif "cca" in cca_prefix.lower():
        device_model = "Tigo CCA"
        device_name = f"Tigo CCA System ({ip_address})"
    else:
        device_model = "Unknown"
        device_name = f"Tigo Local System ({ip_address})"

    system_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{cca_prefix}_tigo_system")},
        manufacturer="Tigo",
        name=device_name,
        model=device_model,
        suggested_area="Solar",
    )

    if source == "CCA":
        async def fetch_energy_data():
            raw = await hass.async_add_executor_job(fetch_daily_energy, ip_address)
            device_info = await hass.async_add_executor_job(fetch_device_info, ip_address)

            _LOGGER.debug(f"Risultato da fetch_daily_energy: {raw}")

            history = raw.get("history", [])
            today_energy = raw.get("today_energy", 0)
            yesterday_energy = raw.get("yesterday_energy", 0)
            weekly_energy = raw.get("weekly_energy", 0)

            history_weekly_named = {
                f"{d} ({calendar.day_name[datetime.strptime(d, '%Y-%m-%d').weekday()]})": v
                for d, v in history[-7:]
            }

            return {
                "today_energy": today_energy,
                "yesterday_energy": yesterday_energy,
                "weekly_energy": weekly_energy,
                "history": history,
                "history_weekly_named": history_weekly_named,
                **device_info,
            }

        system_coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="Tigo Local System Energy",
            update_method=fetch_energy_data,
            update_interval=timedelta(minutes=10),
        )

        await system_coordinator.async_config_entry_first_refresh()

        entities += [
            TigoSystemSensor("Tigo Today Production", "today_energy",
                             UnitOfEnergy.KILO_WATT_HOUR, "tigo_system_today",
                             system_coordinator, cca_prefix,
                             device_class=SensorDeviceClass.ENERGY),

            TigoSystemSensor("Tigo Yesterday Production", "yesterday_energy",
                             UnitOfEnergy.KILO_WATT_HOUR, "tigo_system_yesterday",
                             system_coordinator, cca_prefix,
                             device_class=SensorDeviceClass.ENERGY),

            TigoSystemSensor("Tigo Last 7 Days Production", "weekly_energy",
                             UnitOfEnergy.KILO_WATT_HOUR, "tigo_system_weekly",
                             system_coordinator, cca_prefix,
                             device_class=SensorDeviceClass.ENERGY),
        ]

        system_keys = [
            ("serial", "Tigo Serial", None, "mdi:identifier"),
            ("software", "Tigo Software", None, "mdi:chip"),
            ("kernel", "Tigo Kernel", None, "mdi:linux"),
            ("discovery", "Tigo Discovery", None, "mdi:radar"),
            ("last_data_sync", "Tigo Last Sync", None, "mdi:clock-sync"),
        ]

        for key, name, device_class, icon in system_keys:
            entities.append(
                TigoSystemSensor(
                    name=name,
                    key=key,
                    unit=None,
                    unique_id=f"{cca_prefix}_tigo_system_{key}",
                    coordinator=system_coordinator,
                    cca_prefix=cca_prefix,
                    device_class=device_class,
                )
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
        cca_prefix,
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
        self._attr_name = f"Panel {panel_id} {name}"
        self._attr_unique_id = f"{cca_prefix}_tigo_{panel_id}_{param.lower()}"
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
            "identifiers": {(DOMAIN, f"{cca_prefix}_{panel_id}")},
            "name": f"Panel {panel_id}",
            "manufacturer": "Tigo",
            "model": self._layout.get("type", "Tigo Panel"),
            "sw_version": serial,
            "hw_version": channel,
            "via_device": (DOMAIN, f"{cca_prefix}_tigo_system"),
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
        data = self.coordinator.data
        if not data:
            _LOGGER.debug("No data yet for panel — coordinator is None")
            return None
        

        panel_data = data.get(self._panel_id)
        if not panel_data:
            return None

        value = panel_data.get(self._param)

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

class _EnergyIntegrator:
    """Integrazione trapezoidale semplice da W a kWh."""
    def __init__(self):
        self.last_ts = None
        self.last_w = None
        self.kwh = 0.0

    def update(self, w: float, now=None) -> float:
        if w is None:
            return self.kwh
        now = now or dt_util.utcnow()
        if self.last_ts is None:
            self.last_ts = now
            self.last_w = float(w)
            return self.kwh
        dt_s = (now - self.last_ts).total_seconds()
        if dt_s > 0:
            w0 = float(self.last_w)
            w1 = float(w)
            self.kwh += ((w0 + w1) / 2.0) * dt_s / 3600.0 / 1000.0
            self.last_ts = now
            self.last_w = w1
        return self.kwh


class TigoPanelEnergy(CoordinatorEntity, SensorEntity, RestoreEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, panel_id, layout_info, parent_info, cca_prefix):
        super().__init__(coordinator)
        self._panel_id = panel_id
        self._layout = layout_info or {}
        self._parent_info = parent_info or {}
        self._attr_name = f"Panel {panel_id} Energy"
        self._attr_unique_id = f"{cca_prefix}_tigo_{panel_id}_energy"
        self._integrator = _EnergyIntegrator()
        self._kwh = 0.0

        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{cca_prefix}_{panel_id}")},
            "name": f"Panel {panel_id}",
            "manufacturer": "Tigo",
            "model": self._layout.get("type", "Tigo Panel"),
            "sw_version": self._layout.get("serial", panel_id),
            "hw_version": self._layout.get("channel", "unknown"),
            "via_device": (DOMAIN, f"{cca_prefix}_tigo_system"),
        }

    async def async_added_to_hass(self):
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._integrator.kwh = float(last.state)
                self._kwh = self._integrator.kwh
            except (TypeError, ValueError):
                pass
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_coordinator_update))
        await self._async_integrate_once()
        self.async_write_ha_state()
    

    async def _async_integrate_once(self):
        coord_data = self.coordinator.data or {}
        pd = coord_data.get(self._panel_id) or {}

        w_raw = pd.get("Pin")
        w = float(w_raw) if w_raw is not None else None
        if w is not None and w < 0:
            w = 0.0

        now = dt_util.utcnow()
        self._kwh = round(self._integrator.update(w, now), 3)

    def _handle_coordinator_update(self):
        self.hass.async_create_task(self._async_integrate_then_write())

    async def _async_integrate_then_write(self):
        await self._async_integrate_once()
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self._kwh

    @property
    def extra_state_attributes(self):
        return {
            "serial": self._layout.get("serial"),
            "channel": self._layout.get("channel"),
            "source": "Pin (power) trapezoidal integration on coordinator updates",
        }

class TigoPanelPeriodEnergy(CoordinatorEntity, SensorEntity, RestoreEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator, period: str, total_entity: TigoPanelEnergy,
                 panel_id: str, cca_prefix: str):
        super().__init__(coordinator)
        self._period = period
        self._total_entity = total_entity
        self._panel_id = panel_id
        self._attr_name = f"Panel {panel_id} Energy {period.capitalize()}"
        self._attr_unique_id = f"{cca_prefix}_tigo_{panel_id}_energy_{period}"
        self._baseline = 0.0
        self._period_key = None
        self._value = 0.0

        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{cca_prefix}_{panel_id}")},
            "via_device": (DOMAIN, f"{cca_prefix}_tigo_system"),
        }

    def _current_period_key(self):
        now = dt_util.now()
        return now.strftime("%Y-%m-%d") if self._period == "day" else now.strftime("%Y-%m")

    async def async_added_to_hass(self):
        last = await self.async_get_last_state()
        if last:
            if last.attributes and "baseline" in last.attributes:
                try:
                    self._baseline = float(last.attributes["baseline"])
                except (TypeError, ValueError):
                    pass
            if last.attributes and "period_key" in last.attributes:
                self._period_key = last.attributes["period_key"]
            if last.state not in (None, "unknown", "unavailable"):
                try:
                    self._value = float(last.state)
                except (TypeError, ValueError):
                    pass

        self.async_on_remove(self.coordinator.async_add_listener(self._handle_update))
        await self._async_recompute()
        self.async_write_ha_state()
    

    async def _async_recompute(self):
        total = self._total_entity.native_value
        if total is None:
            return
        key_now = self._current_period_key()
        if self._period_key != key_now:
            self._baseline = float(total)
            self._period_key = key_now
        self._value = round(max(0.0, float(total) - float(self._baseline)), 3)

    def _handle_update(self):
        self.hass.async_create_task(self._async_recompute_then_write())

    async def _async_recompute_then_write(self):
        await self._async_recompute()
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self._value

    @property
    def extra_state_attributes(self):
        return {
            "baseline": round(self._baseline, 3),
            "period_key": self._period_key,
            "period": self._period,
            "source_total_entity": self._total_entity.entity_id if self._total_entity.entity_id else None,
        }

class TigoSystemSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, name, key, unit, unique_id, coordinator, cca_prefix, device_class=None, icon=None):
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_native_unit_of_measurement = unit
        self._key = key
        self._attr_device_class = device_class
        self._attr_icon = icon

        if device_class == SensorDeviceClass.ENERGY:
            self._attr_state_class = SensorStateClass.TOTAL

        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{cca_prefix}_tigo_system")},
            "name": "Tigo Local System",
            "manufacturer": "Tigo",
        }


    @property
    def native_value(self):
        value = self.coordinator.data.get(self._key)

        if isinstance(value, dict):
            try:
                value = list(value.values())[-1]
            except Exception as e:
                _LOGGER.warning("Invalid dict structure for %s: %s", self._key, e)
                value = None

        if self.device_class == SensorDeviceClass.ENERGY:
            try:
                return round(float(value), 2) if value is not None else None
            except (ValueError, TypeError):
                _LOGGER.warning("Non-numeric value for energy sensor %s: %s", self._key, value)
                return None
        return value

    @property
    def extra_state_attributes(self):
        raw_history = self.coordinator.data.get("history") or []
        weekly_energy = self.coordinator.data.get("weekly_energy", 0)

        history_weekly_named = {
            f"{d} ({calendar.day_name[datetime.strptime(d, '%Y-%m-%d').weekday()]})": v
            for d, v in raw_history[-7:]
        }

        return {
            "weekly_energy": weekly_energy,
            "history_weekly_named": history_weekly_named,
        }