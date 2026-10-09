"""Compatibilità tra versioni di Home Assistant."""
from __future__ import annotations

from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.helpers import device_registry as dr

# HA 2026.8 depreca `via_device` (tupla di identifier) a favore di `via_device_id`
# (id del device nel registry); le versioni precedenti non accettano il nuovo campo.
USE_VIA_DEVICE_ID = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 8)


def via_device_kwargs(parent: dr.DeviceEntry, identifier: tuple[str, str]) -> dict:
    """Campo `via_device_id` (HA >= 2026.8) o `via_device` verso il device padre."""
    if USE_VIA_DEVICE_ID:
        return {"via_device_id": parent.id}
    return {"via_device": identifier}


def get_device(
    dev_reg: dr.DeviceRegistry, identifier: tuple[str, str], config_entry_id: str
) -> dr.DeviceEntry | None:
    """Device per identifier: HA 2026.10 depreca `async_get_device`."""
    if hasattr(dev_reg, "async_get_device_by_identifier"):
        return dev_reg.async_get_device_by_identifier(identifier, config_entry_id)
    return dev_reg.async_get_device(identifiers={identifier})


def link_via_device(entities: list, via_device_id: str) -> None:
    """Sostituisce `via_device` con `via_device_id` nel device_info delle entità."""
    if not USE_VIA_DEVICE_ID:
        return
    for ent in entities:
        info = getattr(ent, "_attr_device_info", None)
        if info and info.pop("via_device", None) is not None:
            info["via_device_id"] = via_device_id
