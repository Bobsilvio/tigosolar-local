"""Topology model reconciling Tigo's three identifier spaces.

Three identifier spaces describe the same panels and must be cross-referenced:

* ``equipmentId`` (e.g. ``"A1"``) and the *array index* from
  ``/api/v4/equipments`` -- this index drives ``d[i]`` in the per-minute
  ``/system/summary/summary`` telemetry. The order is stable but **not**
  alphabetical (``A1, A10, A2, ...``) and must be used verbatim.
* ``object_id`` from the ``/systems/layout`` panel tree -- this is the key in
  the ``/system/summary/aggenergy`` per-panel daily-energy dataset.
* The HA device tree: System -> Inverter -> String -> Panel (labels come from
  the layout tree).

``build_topology`` joins ``/systems/layout`` (authoritative per-panel
object_id/serial/labels) with ``/api/v4/equipments`` (telemetry order) by
serial, with documented fallbacks, and exposes lookups plus a stable
``signature`` so the coordinator can detect equipment-order drift.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)


def _norm_serial(value: object) -> str:
    """Normalize a serial for joining (case-insensitive, strip non-alnum)."""
    s = str(value or "").upper()
    return "".join(ch for ch in s if ch.isalnum())


@dataclass(frozen=True)
class PanelMeta:
    """Everything needed to create entities for one panel + map its data."""

    index: int                  # position in the equipments list (telemetry d[i])
    equipment_id: str           # e.g. "A1"
    serial: str | None          # equipmentSerial / layout serial
    model: str | None           # equipmentModel, e.g. "TS4-A-O-700W"
    object_id: str | None       # aggenergy dataset key (per-panel)
    label: str                  # display label
    inverter_label: str
    mppt_label: str
    string_label: str
    inverter_id: object | None = None
    string_id: object | None = None

    @property
    def full_label(self) -> str:
        return (
            f"{self.inverter_label} / {self.mppt_label} / "
            f"{self.string_label} / {self.label}"
        )


@dataclass(frozen=True)
class InverterMeta:
    inverter_id: object
    label: str
    is_monitored: bool


@dataclass
class Topology:
    panels: list[PanelMeta] = field(default_factory=list)
    inverters: list[InverterMeta] = field(default_factory=list)
    cca_uids: list[str] = field(default_factory=list)
    signature: str = ""

    by_index: dict[int, PanelMeta] = field(default_factory=dict)
    by_object_id: dict[str, PanelMeta] = field(default_factory=dict)
    by_equipment_id: dict[str, PanelMeta] = field(default_factory=dict)


def topology_signature(equipments: list[dict]) -> str:
    """Stable hash of equipment identity + order (drift detection)."""
    ident = [
        [e.get("equipmentId"), e.get("equipmentSerial"), e.get("equipmentType")]
        for e in equipments
    ]
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _index_layout_panels(layout: dict) -> tuple[list[dict], list[InverterMeta]]:
    """Flatten the layout tree into panel dicts (+ inverter metadata)."""
    panels: list[dict] = []
    inverters: list[InverterMeta] = []
    system = (layout or {}).get("system", {}) if isinstance(layout, dict) else {}
    for inv in system.get("inverters", []) or []:
        inv_label = inv.get("label", "Inverter")
        inverters.append(
            InverterMeta(
                inverter_id=inv.get("inverter_id") or inv.get("id"),
                label=inv_label,
                is_monitored=bool(inv.get("is_monitored", False)),
            )
        )
        for mppt in inv.get("mppts", []) or []:
            mppt_label = mppt.get("label", "MPPT")
            for string in mppt.get("strings", []) or []:
                string_label = string.get("label", "String")
                for panel in string.get("panels", []) or []:
                    panels.append(
                        {
                            "object_id": str(panel.get("object_id"))
                            if panel.get("object_id") is not None
                            else None,
                            "label": panel.get("label") or "",
                            "serial": panel.get("serial"),
                            "model": panel.get("type"),
                            "inverter_label": inv_label,
                            "mppt_label": mppt_label,
                            "string_label": string_label,
                            "inverter_id": inv.get("inverter_id") or inv.get("id"),
                            "string_id": string.get("string_id")
                            or string.get("id"),
                        }
                    )
    return panels, inverters


def build_topology(layout: dict, equipments: list[dict]) -> Topology:
    """Join layout (per-panel metadata) with equipments (telemetry order)."""
    layout_panels, inverters = _index_layout_panels(layout)

    by_serial: dict[str, dict] = {}
    by_label: dict[str, dict] = {}
    for lp in layout_panels:
        if lp["serial"]:
            by_serial[_norm_serial(lp["serial"])] = lp
        if lp["label"]:
            by_label[str(lp["label"]).upper()] = lp

    topo = Topology(inverters=inverters)
    used: set[int] = set()

    for idx, eq in enumerate(equipments):
        etype = (eq.get("equipmentType") or "").lower()
        if etype in ("unit", "cca", "gateway"):
            if eq.get("equipmentSerial"):
                topo.cca_uids.append(str(eq["equipmentSerial"]))
            continue
        if etype and etype != "panel":
            # Non-panel equipment (inverter/meter/battery) is logged elsewhere;
            # not modelled as a telemetry panel here.
            continue

        eq_id = str(eq.get("equipmentId") or f"idx{idx}")
        eq_serial = eq.get("equipmentSerial")

        lp = by_serial.get(_norm_serial(eq_serial)) if eq_serial else None
        if lp is None:
            lp = by_label.get(eq_id.upper())
        if lp is None and idx < len(layout_panels):
            # Last-resort positional fallback (logged: mapping is uncertain).
            lp = layout_panels[idx]
            _LOGGER.debug(
                "Topology: positional fallback for equipment %s (serial %s)",
                eq_id,
                eq_serial,
            )

        meta = PanelMeta(
            index=idx,
            equipment_id=eq_id,
            serial=eq_serial or (lp.get("serial") if lp else None),
            model=eq.get("equipmentModel") or (lp.get("model") if lp else None),
            object_id=lp.get("object_id") if lp else None,
            label=(lp.get("label") if lp and lp.get("label") else eq_id),
            inverter_label=lp.get("inverter_label", "Inverter") if lp else "Inverter",
            mppt_label=lp.get("mppt_label", "MPPT") if lp else "MPPT",
            string_label=lp.get("string_label", "String") if lp else "String",
            inverter_id=lp.get("inverter_id") if lp else None,
            string_id=lp.get("string_id") if lp else None,
        )
        topo.panels.append(meta)
        topo.by_index[idx] = meta
        topo.by_equipment_id[eq_id] = meta
        if meta.object_id:
            topo.by_object_id[meta.object_id] = meta

    topo.signature = topology_signature(equipments)

    matched = sum(1 for p in topo.panels if p.object_id)
    _LOGGER.debug(
        "Topology built: %d panels (%d with object_id), %d inverters, CCAs=%s",
        len(topo.panels),
        matched,
        len(topo.inverters),
        topo.cca_uids,
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        # The /api/v4/equipments order (alphabetical: A1, A10, A11, A2, ...).
        # NOTE: this is NOT the summary d[] column order -- d[] is mapped by
        # the payload's own `order` list, see coordinator._summary_column_map
        # (issue #8). Logged only to correlate equipmentId <-> object_id.
        order = [
            (i, e.get("equipmentId"), (e.get("equipmentType") or "").lower())
            for i, e in enumerate(equipments)
        ]
        _LOGGER.debug("EQUIPMENT-ORDER (equipments list order, NOT d[]): %s", order)
    return topo
