"""Migrazione delle config entry verso la versione 3.

Due integrazioni condividevano il dominio ``tigo``:

* **tigosolar-local** (questa) -- entry v1. Sorgenti CCA/ESP32 invariate; la
  vecchia sorgente Cloud (client sincrono, chiavi ``username``/``password``)
  viene convertita al motore Cloud ex tigosolar-online.
* **tigosolar-online** -- entry v1 (solo email/password) e v2 (con
  ``system_id``, opzioni premium/api_version). Chi passa da online a local
  si ritrova queste entry: basta marcarle come sorgente Cloud, gli unique_id
  delle entità sono già quelli del motore Cloud (storico preservato).

Le due integrazioni non potevano coesistere (stessa cartella
``custom_components/tigo``), quindi un'entry v1 con ``email`` è di online,
una con ``ip_address``/``source``/``username`` è di local.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .compat import get_device
from .const import DOMAIN, SOURCE_CLOUD

_LOGGER = logging.getLogger(__name__)

CONFIG_VERSION = 3

# Chiavi della vecchia sorgente Cloud di local (<= 3.1.x)
_LEGACY_USERNAME = "username"

# Sensori di sistema della vecchia sorgente Cloud di local -> unique_id suffix
# del motore Cloud. Le chiavi senza equivalente vengono rimosse.
_LEGACY_SYSTEM_MAP = {
    "power_now_w": "current_power",
    "energy_today_wh": "production_today",
    "energy_lifetime_wh": "production",
}
_LEGACY_SYSTEM_KEYS = {
    "power_now_w",
    "power_day_max_w",
    "energy_today_wh",
    "energy_week_wh",
    "energy_month_wh",
    "energy_year_wh",
    "energy_lifetime_wh",
    "reclaimed_today_wh",
}
# Sensori per pannello: suffix legacy -> suffix motore Cloud (None = rimuovi).
# energy_today (TOTAL, giornaliero) non è compatibile con l'energia lifetime
# (TOTAL_INCREASING) del motore Cloud: niente rimappatura.
_LEGACY_PANEL_MAP = {
    "power": "power",
    "reclaimed": "reclaimed",
    "energy_today": None,
}


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Porta l'entry alla versione 3 (chiamata da HA prima del setup)."""
    if entry.version >= CONFIG_VERSION:
        return True

    data = dict(entry.data)
    _LOGGER.info(
        "Migrating Tigo config entry %s (v%s) -> v%s",
        entry.entry_id, entry.version, CONFIG_VERSION,
    )

    if CONF_EMAIL in data:
        # Entry di tigosolar-online (v1 o v2): diventa sorgente Cloud.
        data["source"] = SOURCE_CLOUD
        options = dict(entry.options)
        options.setdefault("premium", False)
        options.setdefault("api_version", "auto")
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=CONFIG_VERSION
        )
        _LOGGER.info("Tigo: entry di tigosolar-online convertita in sorgente Cloud")
        return True

    source = entry.options.get("source") or data.get("source")
    if source == SOURCE_CLOUD:
        _migrate_legacy_cloud(hass, entry, data)
        return True

    # Sorgenti locali CCA / ESP32: nessun cambiamento ai dati.
    hass.config_entries.async_update_entry(entry, version=CONFIG_VERSION)
    return True


def _migrate_legacy_cloud(hass: HomeAssistant, entry: ConfigEntry, data: dict) -> None:
    """Vecchia sorgente Cloud di local -> motore Cloud (ex online)."""
    sid = int(data["system_id"])
    email = entry.options.get(_LEGACY_USERNAME) or data.get(_LEGACY_USERNAME)
    password = entry.options.get(CONF_PASSWORD) or data.get(CONF_PASSWORD)

    new_data = {
        "source": SOURCE_CLOUD,
        CONF_EMAIL: email,
        CONF_PASSWORD: password,
        "system_id": sid,
    }
    if data.get("ip_address"):
        new_data["ip_address"] = data["ip_address"]

    _migrate_legacy_cloud_registry(hass, entry, sid)

    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options={"premium": False, "api_version": "auto"},
        unique_id=f"tigo-{sid}",
        version=CONFIG_VERSION,
    )
    _LOGGER.info("Tigo: sorgente Cloud legacy migrata al nuovo motore (system %s)", sid)


def _migrate_legacy_cloud_registry(hass: HomeAssistant, entry: ConfigEntry, sid: int) -> None:
    """Rimappa unique_id di entità e identifier dei device al nuovo schema."""
    ent_reg = er.async_get(hass)
    prefix = f"tigo_cloud_{sid}_"

    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        uid = ent.unique_id or ""
        if not uid.startswith(prefix):
            continue
        rest = uid[len(prefix):]

        new_uid: str | None = None
        if rest in _LEGACY_SYSTEM_KEYS:
            suffix = _LEGACY_SYSTEM_MAP.get(rest)
            new_uid = f"tigo_{sid}_{suffix}" if suffix else None
        else:
            for legacy, suffix in _LEGACY_PANEL_MAP.items():
                if rest.endswith(f"_{legacy}"):
                    oid = rest[: -len(legacy) - 1]
                    new_uid = f"tigo_{oid}_{suffix}" if suffix else None
                    break

        if new_uid and not ent_reg.async_get_entity_id(ent.domain, DOMAIN, new_uid):
            ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_uid)
            _LOGGER.debug("Tigo migration: %s unique_id %s -> %s", ent.entity_id, uid, new_uid)
        else:
            ent_reg.async_remove(ent.entity_id)
            _LOGGER.debug("Tigo migration: rimosso %s (nessun equivalente)", ent.entity_id)

    # Device: tigo_cloud_{sid}_system -> {sid}; tigo_cloud_{sid}_{oid} -> {oid}
    dev_reg = dr.async_get(hass)
    for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        new_ids = set()
        changed = False
        for domain, ident in dev.identifiers:
            if domain == DOMAIN and ident.startswith(prefix):
                rest = ident[len(prefix):]
                ident = str(sid) if rest == "system" else rest
                changed = True
            new_ids.add((domain, ident))
        if not changed:
            continue
        if any(get_device(dev_reg, i, entry.entry_id) for i in new_ids):
            continue  # già presente un device col nuovo identifier
        dev_reg.async_update_device(dev.id, new_identifiers=new_ids)
