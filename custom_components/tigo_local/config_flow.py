from __future__ import annotations
import logging
from typing import Any
import voluptuous as vol
import ipaddress

from homeassistant import config_entries
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.data_entry_flow import FlowResult

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
            except ValueError:
                errors[CONF_IP_ADDRESS] = "invalid_ip"
            else:
                await self.async_set_unique_id(f"tigo_{ip_input}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Tigo @ {ip_input}",
                    data={CONF_IP_ADDRESS: ip_input},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_IP_ADDRESS): str,
            }),
            errors=errors,
            description_placeholders={
                "ip_info": "Inserisci l'indirizzo IP interno del tuo Tigo CCA (es. 192.168.1.100)"
            }
        )
