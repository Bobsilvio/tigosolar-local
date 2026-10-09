from __future__ import annotations
from typing import Any
import voluptuous as vol
import ipaddress

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_IP_ADDRESS, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    DATA_SOURCE,
    LOCAL_SOURCES,
    SOURCE_CCA,
    SOURCE_ESP,
    SOURCE_CLOUD,
    CONF_SYSTEM_ID,
    CONF_FORCE_CLOUD,
    _LOGGER,
)
from .cloud.api import API_AUTO, API_V3, API_V4, TigoApiError, TigoAuthError, async_create_client
from .cloud.const import (
    CONF_API_VERSION,
    CONF_ENABLE_CURRENT,
    CONF_ENABLE_RSSI,
    CONF_ENABLE_VOLTAGE,
    CONF_ENERGY_POLL_INTERVAL,
    CONF_NIGHT_SKIP,
    CONF_PANEL_SCAN_INTERVAL,
    CONF_PREMIUM,
    CONF_PROBE_EXTRA_HARDWARE,
    CONF_VERBOSE_LOGGING,
    DEFAULT_ENERGY_POLL_INTERVAL,
    DEFAULT_NIGHT_SKIP,
    DEFAULT_PANEL_SCAN_INTERVAL,
)
from .migration import CONFIG_VERSION

_API_CHOICES = [API_AUTO, API_V4, API_V3]


async def _validate_cloud(hass, email: str, password: str, api_pref: str) -> list[dict]:
    """Login cloud e lista impianti; solleva TigoAuthError/TigoApiError."""
    from .cloud import async_resolve_systems

    session = async_get_clientsession(hass)
    client = await async_create_client(session, email, password, api_pref=api_pref)
    return await async_resolve_systems(client)


def _system_id(system: dict) -> int:
    return int(system.get("system_id") or system.get("id"))


class TigoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        self._ip: str | None = None
        self._creds: dict[str, Any] = {}
        self._systems: list[dict] = []
        self._probe: dict | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            ip_input = (user_input.get(CONF_IP_ADDRESS) or "").strip()
            source = user_input.get("source", SOURCE_CCA)
            force_cloud = user_input.get(CONF_FORCE_CLOUD, False)

            # Cloud scelto esplicitamente: l'IP non serve
            if source == SOURCE_CLOUD:
                return await self.async_step_cloud()

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
                vol.Optional(CONF_IP_ADDRESS, default=""): str,
                vol.Required("source", default=SOURCE_CCA): vol.In(DATA_SOURCE),
                vol.Optional(CONF_FORCE_CLOUD, default=False): bool,
            }),
            errors=errors,
        )

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Login all'account cloud Tigo."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_pref = user_input.get(CONF_API_VERSION, API_AUTO)
            try:
                self._systems = await _validate_cloud(
                    self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD], api_pref
                )
            except TigoAuthError:
                errors["base"] = "invalid_auth"
            except (TigoApiError, Exception):  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                self._creds = {
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_API_VERSION: api_pref,
                    CONF_PREMIUM: user_input.get(CONF_PREMIUM, False),
                }
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
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_PREMIUM, default=False): bool,
                vol.Optional(CONF_API_VERSION, default=API_AUTO): vol.In(_API_CHOICES),
            }),
            errors=errors,
            description_placeholders={"fw": fw or "-"},
        )

    async def async_step_select_system(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Selezione impianto quando l'account ne ha più di uno."""
        if user_input is not None:
            sid = int(user_input[CONF_SYSTEM_ID])
            chosen = next((s for s in self._systems if _system_id(s) == sid), None)
            if chosen:
                return await self._create_cloud_entry(chosen)

        options = {
            str(_system_id(s)): f"{s.get('name', 'System')} ({_system_id(s)})"
            for s in self._systems
        }
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
        sid = _system_id(system)
        await self.async_set_unique_id(f"tigo-{sid}", raise_on_progress=False)
        self._abort_if_unique_id_configured()
        data = {
            "source": SOURCE_CLOUD,
            CONF_EMAIL: self._creds[CONF_EMAIL],
            CONF_PASSWORD: self._creds[CONF_PASSWORD],
            CONF_SYSTEM_ID: sid,
        }
        if self._ip:
            data[CONF_IP_ADDRESS] = self._ip
        return self.async_create_entry(
            title=f"Tigo {system.get('name') or sid} (Cloud)",
            data=data,
            options={
                CONF_PREMIUM: self._creds[CONF_PREMIUM],
                CONF_API_VERSION: self._creds[CONF_API_VERSION],
            },
        )

    # ---- reauth (solo Cloud) ----
    async def async_step_reauth(self, _entry_data: dict) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if user_input is not None:
            try:
                await _validate_cloud(
                    self.hass,
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    entry.options.get(CONF_API_VERSION, API_AUTO),
                )
            except TigoAuthError:
                errors["base"] = "invalid_auth"
            except (TigoApiError, Exception):  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=entry.data.get(CONF_EMAIL, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        source = config_entry.options.get("source") or config_entry.data.get("source")
        if source == SOURCE_CLOUD:
            return TigoCloudOptionsFlow(config_entry)
        return TigoOptionsFlow(config_entry)


class TigoOptionsFlow(config_entries.OptionsFlow):
    """Opzioni sorgenti locali (CCA / ESP32)."""

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

                ip_input = user_input.get(CONF_IP_ADDRESS, "")
                ipaddress.ip_address(ip_input)
                return self.async_create_entry(
                    title="",
                    data={
                        "source": user_input.get("source", source),
                        OPT_SCAN_INTERVAL: scan_sec,
                        CONF_IP_ADDRESS: ip_input,
                    },
                )
            except ValueError as e:
                if str(e) == "scan_out_of_range":
                    errors["base"] = "invalid_scan_interval"
                else:
                    errors[CONF_IP_ADDRESS] = "invalid_ip"

        current_ip = self._config_entry.options.get(
            CONF_IP_ADDRESS, self._config_entry.data.get(CONF_IP_ADDRESS, "")
        )
        schema = vol.Schema({
            vol.Required(CONF_IP_ADDRESS, default=current_ip): str,
            vol.Required("source", default=source): vol.In(LOCAL_SOURCES),
            vol.Required(OPT_SCAN_INTERVAL, default=current_scan): vol.All(
                vol.Coerce(int), vol.Range(min=SCAN_INTERVAL_MIN_SEC, max=SCAN_INTERVAL_MAX_SEC)
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)


class TigoCloudOptionsFlow(config_entries.OptionsFlow):
    """Opzioni sorgente Cloud: intervalli, metriche per pannello, log."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        super().__init__()
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        o = self._config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_API_VERSION, default=o.get(CONF_API_VERSION, API_AUTO)
                ): vol.In(_API_CHOICES),
                vol.Optional(CONF_PREMIUM, default=o.get(CONF_PREMIUM, False)): bool,
                vol.Optional(
                    CONF_PANEL_SCAN_INTERVAL,
                    default=o.get(CONF_PANEL_SCAN_INTERVAL, DEFAULT_PANEL_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=15, max=3600)),
                vol.Optional(
                    CONF_ENERGY_POLL_INTERVAL,
                    default=o.get(CONF_ENERGY_POLL_INTERVAL, DEFAULT_ENERGY_POLL_INTERVAL),
                ): vol.All(int, vol.Range(min=60, max=7200)),
                vol.Optional(
                    CONF_NIGHT_SKIP, default=o.get(CONF_NIGHT_SKIP, DEFAULT_NIGHT_SKIP)
                ): bool,
                vol.Optional(
                    CONF_ENABLE_VOLTAGE, default=o.get(CONF_ENABLE_VOLTAGE, False)
                ): bool,
                vol.Optional(
                    CONF_ENABLE_CURRENT, default=o.get(CONF_ENABLE_CURRENT, False)
                ): bool,
                vol.Optional(CONF_ENABLE_RSSI, default=o.get(CONF_ENABLE_RSSI, False)): bool,
                vol.Optional(
                    CONF_VERBOSE_LOGGING, default=o.get(CONF_VERBOSE_LOGGING, False)
                ): bool,
                vol.Optional(
                    CONF_PROBE_EXTRA_HARDWARE,
                    default=o.get(CONF_PROBE_EXTRA_HARDWARE, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
