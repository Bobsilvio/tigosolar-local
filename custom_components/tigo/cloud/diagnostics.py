"""Downloadable, redacted diagnostics.

The clean "share with us" path (no log scraping): topology, capability flags,
features/premium, the last data sample and -- if the user enabled the
probe_extra_hardware option -- the raw responses of the hardware-gated
endpoints, with all secrets redacted.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {
    "email",
    "password",
    "token",
    "refresh_token",
    "auth",
    "username",
    "contact_email",
    "contact_name",
    "latitude",
    "longitude",
    "street",
    "zip",
    "address",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = store.get("coordinator")

    out: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "version": entry.version,
        },
        "api_version": getattr(store.get("client"), "api_version", None),
    }

    if coordinator is not None:
        topo = getattr(coordinator, "topology", None)
        out["topology"] = (
            {
                "panel_count": len(topo.panels),
                "panels_with_object_id": sum(
                    1 for p in topo.panels if p.object_id
                ),
                "inverters": [
                    {"label": i.label, "is_monitored": i.is_monitored}
                    for i in topo.inverters
                ],
                "cca_uids": topo.cca_uids,
                "signature": topo.signature,
                "panels": [
                    {
                        "index": p.index,
                        "equipment_id": p.equipment_id,
                        "model": p.model,
                        "has_object_id": p.object_id is not None,
                        "string": p.string_label,
                        "inverter": p.inverter_label,
                    }
                    for p in topo.panels
                ],
            }
            if topo
            else None
        )
        out["data_sample"] = coordinator.data
        out["extra_hardware_probe"] = async_redact_data(
            getattr(coordinator, "extra_probe", {}), TO_REDACT
        )

    return out
