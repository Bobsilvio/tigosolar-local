from __future__ import annotations
import logging
from typing import Any
import voluptuous as vol
import ipaddress

from homeassistant import config_entries
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.data_entry_flow import FlowResult
from homeassistant.core import callback

from .const import (
    DOMAIN,
    DATA_SOURCE,
    SOURCE_CCA,
    SOURCE_ESP,
    SOURCE_CLOUD,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SYSTEM_ID,
    CONF_FORCE_CLOUD,
    _LOGGER,
)


class TigoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._ip: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._systems: list[dict] = []
        self._probe: dict | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            ip_input = user_input.get(CONF_IP_ADDRESS, "")
            source = user_input.get("source", SOURCE_CCA)
            force_cloud = user_input.get(CONF_FORCE_CLOUD, False)
            try:
                ipaddress.ip_address(ip_input)
            except ValueError:
                errors[CONF_IP_ADDRESS] = "invalid_ip"
            else:
                self._ip = ip_input

                # Forza cloud: salta il rilevamento firmware (test/debug)
                if force_cloud:
                    return await self.async_step_cloud()

                # ESP32: sorgente locale diretta, nessun rilevamento firmware
                if source == SOURCE_ESP:
                    return await self._create_local_entry(ip_input, SOURCE_ESP)

                # CCA: sonda il firmware locale
                from .tigo_api import probe_local
                self._probe = await self.hass.async_add_executor_job(probe_local, ip_input)
                _LOGGER.debug("Probe locale %s: %s", ip_input, self._probe)

                if not self._probe.get("reachable"):
                    errors["base"] = "cannot_connect"
                elif self._probe.get("requires_cloud"):
                    # Firmware >= 4.0.4 (o locale bloccato): passa al login cloud
                    return await self.async_step_cloud()
                else:
                    # Locale accessibile: sorgente CCA classica
                    return await self._create_local_entry(ip_input, SOURCE_CCA)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_IP_ADDRESS): str,
                vol.Required("source", default=SOURCE_CCA): vol.In([SOURCE_CCA, SOURCE_ESP]),
                vol.Optional(CONF_FORCE_CLOUD, default=False): bool,
            }),
            errors=errors,
            description_placeholders={
                "ip_info": "Indirizzo IP locale del CCA/ESP32 (es. 192.168.1.100). "
                           "Con firmware >= 4.0.4 verrà richiesto il login all'account Tigo."
            },
        )

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Login all'account cloud Tigo (firmware locale protetto da password)."""
        from .tigo_cloud import TigoCloudClient, TigoAuthError, TigoCloudError

        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            def _login_and_discover() -> list[dict]:
                client = TigoCloudClient(self._username, self._password)
                client.login()
                return client.discover_systems()

            try:
                self._systems = await self.hass.async_add_executor_job(_login_and_discover)
            except TigoAuthError:
                errors["base"] = "invalid_auth"
            except TigoCloudError:
                errors["base"] = "cannot_connect"
            else:
                if not self._systems:
                    errors["base"] = "no_systems"
                elif len(self._systems) == 1:
                    return await self._create_cloud_entry(self._systems[0])
                else:
                    return await self.async_step_select_system()

        fw = (self._probe or {}).get("software")
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
            description_placeholders={
                "fw_info": f"Firmware locale {fw} rilevato: dato locale protetto, uso l'account Tigo."
                           if fw else "Dato locale non accessibile: accedi con l'account Tigo."
            },
        )

    async def async_step_select_system(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Selezione impianto quando l'account ne ha più di uno."""
        if user_input is not None:
            sid = int(user_input[CONF_SYSTEM_ID])
            chosen = next((s for s in self._systems if int(s["system_id"]) == sid), None)
            if chosen:
                return await self._create_cloud_entry(chosen)

        options = {str(s["system_id"]): f"{s['name']} ({s['system_id']})" for s in self._systems}
        return self.async_show_form(
            step_id="select_system",
            data_schema=vol.Schema({vol.Required(CONF_SYSTEM_ID): vol.In(options)}),
        )

    async def _create_local_entry(self, ip: str, source: str) -> FlowResult:
        unique_id = f"tigo_{ip.replace('.', '_')}_{source}"
        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Tigo @ {ip} ({source})",
            data={CONF_IP_ADDRESS: ip, "source": source},
        )

    async def _create_cloud_entry(self, system: dict) -> FlowResult:
        sid = int(system["system_id"])
        unique_id = f"tigo_cloud_{sid}"
        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Tigo {system['name']} (Cloud)",
            data={
                "source": SOURCE_CLOUD,
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_SYSTEM_ID: sid,
                CONF_IP_ADDRESS: self._ip,
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

        source = self._config_entry.options.get(
            "source", self._config_entry.data.get("source", SOURCE_CCA)
        )
        is_cloud = source == SOURCE_CLOUD

        current_scan = int(
            self._config_entry.options.get(
                OPT_SCAN_INTERVAL,
                self._config_entry.data.get(OPT_SCAN_INTERVAL, SCAN_INTERVAL_DEFAULT_SEC),
            )
        )

        if user_input is not None:
            scan_sec = user_input.get(OPT_SCAN_INTERVAL, SCAN_INTERVAL_DEFAULT_SEC)
            try:
                scan_sec = int(scan_sec)
                if not (SCAN_INTERVAL_MIN_SEC <= scan_sec <= SCAN_INTERVAL_MAX_SEC):
                    raise ValueError("scan_out_of_range")

                data = {"source": source, OPT_SCAN_INTERVAL: scan_sec}

                if is_cloud:
                    # Consente di aggiornare le credenziali cloud
                    data[CONF_USERNAME] = user_input.get(
                        CONF_USERNAME, self._config_entry.data.get(CONF_USERNAME)
                    )
                    data[CONF_PASSWORD] = user_input.get(
                        CONF_PASSWORD, self._config_entry.data.get(CONF_PASSWORD)
                    )
                else:
                    ip_input = user_input.get(CONF_IP_ADDRESS, "")
                    ipaddress.ip_address(ip_input)
                    data[CONF_IP_ADDRESS] = ip_input

                return self.async_create_entry(title="", data=data)

            except ValueError as e:
                if str(e) == "scan_out_of_range":
                    errors["base"] = "invalid_scan_interval"
                else:
                    errors[CONF_IP_ADDRESS] = "invalid_ip"

        scan_field = {
            vol.Required(OPT_SCAN_INTERVAL, default=current_scan): vol.All(
                vol.Coerce(int), vol.Range(min=SCAN_INTERVAL_MIN_SEC, max=SCAN_INTERVAL_MAX_SEC)
            )
        }

        if is_cloud:
            schema = vol.Schema({
                vol.Optional(
                    CONF_USERNAME,
                    default=self._config_entry.data.get(CONF_USERNAME, ""),
                ): str,
                vol.Optional(CONF_PASSWORD): str,
                **scan_field,
            })
        else:
            current_ip = self._config_entry.options.get(
                CONF_IP_ADDRESS, self._config_entry.data.get(CONF_IP_ADDRESS, "")
            )
            schema = vol.Schema({
                vol.Required(CONF_IP_ADDRESS, default=current_ip): str,
                vol.Required("source", default=source): vol.In(DATA_SOURCE),
                **scan_field,
            })

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
