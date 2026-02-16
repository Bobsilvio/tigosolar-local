from __future__ import annotations
import logging
from typing import Any
import voluptuous as vol
import ipaddress

from homeassistant import config_entries
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.data_entry_flow import FlowResult
from homeassistant.core import callback

from .const import DOMAIN, DATA_SOURCE, _LOGGER

class TigoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors = {}

        if user_input is not None:
            ip_input = user_input.get(CONF_IP_ADDRESS, "")
            source = user_input.get("source", "CCA")
            try:
                import ipaddress
                ipaddress.ip_address(ip_input)  # validazione IP solo per CCA/ESP32 IP
                unique_id = f"tigo_{ip_input.replace('.', '_')}_{source}"
                await self.async_set_unique_id(unique_id, raise_on_progress=False)

                for entry in self._async_current_entries():
                    if entry.unique_id == unique_id:
                        return self.async_abort(reason="already_configured")

                return self.async_create_entry(
                    title=f"Tigo @ {ip_input} ({source})",
                    data={
                        CONF_IP_ADDRESS: ip_input,
                        "source": source
                    },
                )
            except ValueError:
                errors[CONF_IP_ADDRESS] = "invalid_ip"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_IP_ADDRESS): str,
                vol.Required("source", default="CCA"): vol.In(DATA_SOURCE),
            }),
            errors=errors,
            description_placeholders={
                "ip_info": "Inserisci l'indirizzo IP interno del tuo Tigo o ESP32 (es. 192.168.1.100)"
            },
        )
    
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TigoOptionsFlow(config_entry)

class TigoOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        super().__init__()
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        from .const import (
            OPT_SCAN_INTERVAL,
            SCAN_INTERVAL_DEFAULT_SEC,
            SCAN_INTERVAL_MIN_SEC,
            SCAN_INTERVAL_MAX_SEC,
        )
        errors = {}

        current_ip = self._config_entry.options.get(
            CONF_IP_ADDRESS, self._config_entry.data.get(CONF_IP_ADDRESS, "")
        )
        current_source = self._config_entry.options.get(
            "source", self._config_entry.data.get("source", "CCA")
        )
        current_scan = int(
            self._config_entry.options.get(
                OPT_SCAN_INTERVAL,
                self._config_entry.data.get(OPT_SCAN_INTERVAL, SCAN_INTERVAL_DEFAULT_SEC),
            )
        )

        if user_input is not None:
            ip_input = user_input.get(CONF_IP_ADDRESS, "")
            source = user_input.get("source", "CCA")
            scan_sec = user_input.get(OPT_SCAN_INTERVAL, SCAN_INTERVAL_DEFAULT_SEC)

            try:
                import ipaddress
                ipaddress.ip_address(ip_input)

                # normalizza/valida scan interval
                scan_sec = int(scan_sec)
                if not (SCAN_INTERVAL_MIN_SEC <= scan_sec <= SCAN_INTERVAL_MAX_SEC):
                    raise ValueError("scan_out_of_range")

                return self.async_create_entry(
                    title="",
                    data={
                        CONF_IP_ADDRESS: ip_input,
                        "source": source,
                        OPT_SCAN_INTERVAL: scan_sec,
                    },
                )

            except ValueError as e:
                if str(e) == "scan_out_of_range":
                    errors["base"] = "invalid_scan_interval"
                else:
                    errors[CONF_IP_ADDRESS] = "invalid_ip"

        schema = vol.Schema({
            vol.Required(CONF_IP_ADDRESS, default=current_ip): str,
            vol.Required("source", default=current_source): vol.In(DATA_SOURCE),
            vol.Required(OPT_SCAN_INTERVAL, default=current_scan): vol.All(
                vol.Coerce(int),
                vol.Range(min=SCAN_INTERVAL_MIN_SEC, max=SCAN_INTERVAL_MAX_SEC),
            ),
        })

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )

    
