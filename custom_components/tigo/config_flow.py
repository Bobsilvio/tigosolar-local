from __future__ import annotations
import logging
from typing import Any
import voluptuous as vol
import ipaddress

from homeassistant import config_entries
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.data_entry_flow import FlowResult
from homeassistant.core import callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TigoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors = {}

        if user_input is not None:
            ip_input = user_input.get(CONF_IP_ADDRESS, "")
            try:
                ipaddress.ip_address(ip_input)
                unique_id = f"tigo_{ip_input.replace('.', '_')}"
                await self.async_set_unique_id(unique_id, raise_on_progress=False)
                existing_entry = await self.async_get_entry(unique_id)

                if existing_entry:
                    return self.async_abort(reason="already_configured")
                
                return self.async_create_entry(
                    title=f"Tigo @ {ip_input}",
                    data={CONF_IP_ADDRESS: ip_input},
                )
            except ValueError:
                errors[CONF_IP_ADDRESS] = "invalid_ip"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_IP_ADDRESS): str,
            }),
            errors=errors,
            description_placeholders={
                "ip_info": "Inserisci l'indirizzo IP interno del tuo Tigo CCA (es. 192.168.1.100)"
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
        errors = {}

        current_ip = self._config_entry.options.get(
            CONF_IP_ADDRESS,
            self._config_entry.data.get(CONF_IP_ADDRESS, "")
        )

        if user_input is not None:
            ip_input = user_input.get(CONF_IP_ADDRESS, "")
            try:
                ipaddress.ip_address(ip_input)
                return self.async_create_entry(title="", data={CONF_IP_ADDRESS: ip_input})
            except ValueError:
                errors[CONF_IP_ADDRESS] = "invalid_ip"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_IP_ADDRESS, default=current_ip): str,
            }),
            errors=errors,
        )
