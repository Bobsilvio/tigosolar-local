"""Stable panel identity for the ESP32 (RS-485) source.

The ESP32 firmware identifies each optimizer by two values:

* ``addr``: the short PV node ID assigned by the CCA. It can change when the CCA
  rediscovers the network, so history keyed on it can jump to another panel.
* ``longaddr``/``barcode``: the 64-bit 802.15.4 long address of the optimizer, fixed
  in hardware.

Panels are keyed on the long address. Entities and devices created by earlier
versions (keyed on ``addr``) are adopted at setup: their unique_id and device
identifier are renamed in the registries, so entity_ids and history are kept.
"""

from __future__ import annotations

import logging
import string

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .compat import get_device
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_HEX = set(string.hexdigits)


def long_address(value: object) -> str | None:
    """Normalized long address (16 hex digits, not all zero), else None."""
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    if len(value) != 16 or not set(value) <= _HEX or not value.strip("0"):
        return None
    return value


def is_long_address(panel_id: object) -> bool:
    return long_address(panel_id) == panel_id


def adopt_short_ids(hass: HomeAssistant, entry: ConfigEntry, prefix: str, panel_data: dict) -> int:
    """Rename registry entries keyed on the short ``addr`` to the long address.

    ``panel_data`` is the coordinator data: ``{long_address: {"Addr": short, ...}}``.
    An entry is renamed only when nothing exists yet under the new id; otherwise
    it is left untouched. Returns the number of renamed entities.
    """
    renames = {
        data["Addr"]: panel_id
        for panel_id, data in panel_data.items()
        if isinstance(data, dict)
        and is_long_address(panel_id)
        and isinstance(data.get("Addr"), str)
        and data["Addr"]
        and data["Addr"] != panel_id
    }
    if not renames:
        return 0

    ent_reg = er.async_get(hass)
    renamed = 0
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        uid = ent.unique_id or ""
        for old, new in renames.items():
            old_prefix = f"{prefix}_tigo_{old}_"
            if not uid.startswith(old_prefix):
                continue
            new_uid = f"{prefix}_tigo_{new}_{uid[len(old_prefix):]}"
            if ent_reg.async_get_entity_id(ent.domain, DOMAIN, new_uid) is None:
                ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_uid)
                renamed += 1
                _LOGGER.debug("Tigo ESP32: %s unique_id %s -> %s", ent.entity_id, uid, new_uid)
            break

    dev_reg = dr.async_get(hass)
    for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        new_ids = set()
        changed = False
        for domain, ident in dev.identifiers:
            if domain == DOMAIN:
                for old, new in renames.items():
                    if ident == f"{prefix}_{old}":
                        target = f"{prefix}_{new}"
                        if get_device(dev_reg, (DOMAIN, target), entry.entry_id) is None:
                            ident = target
                            changed = True
                        break
            new_ids.add((domain, ident))
        if changed:
            dev_reg.async_update_device(dev.id, new_identifiers=new_ids)

    if renamed:
        _LOGGER.info(
            "Tigo ESP32: %d entities moved from short node IDs to optimizer long addresses",
            renamed,
        )
    return renamed
