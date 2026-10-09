"""Single shared data coordinator for the Tigo v4 cloud API.

Responsibilities:

* Incremental per-minute fetch: the summary endpoint returns the whole day's
  1440-minute array every call; we only emit values for minutes newer than the
  last processed one, and carry previous values forward otherwise (no history
  spam).
* Day rollover (local/system tz): finalize energy, reset per-minute/per-day
  baselines, refresh system info (sunrise/sunset/premium/features).
* Night / CCA-cadence skips: don't hammer the API when the sun is down or when
  ``lastData`` has not advanced.
* Resilience for Tigo's poor uptime: exponential backoff with jitter, and
  strict respect of throttling (429 / 503 + Retry-After). Surfaces health via
  ``last_update_success`` (a connectivity binary_sensor reads it) and raises a
  repair issue after a prolonged outage.

Not wired into setup yet -- the entity model (commit 8) consumes this and flips
the integration from the v3 path to v4/auto.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from functools import partial
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import restore_state
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TigoApiError, TigoAuthError
from .api.errors import TigoThrottleError
from .const import (
    BACKOFF_BASE,
    BACKOFF_MAX,
    CONF_ENABLE_CURRENT,
    CONF_ENABLE_RSSI,
    CONF_ENABLE_VOLTAGE,
    CONF_ENERGY_POLL_INTERVAL,
    CONF_NIGHT_SKIP,
    CONF_PANEL_SCAN_INTERVAL,
    CONF_PROBE_EXTRA_HARDWARE,
    DEFAULT_ENERGY_POLL_INTERVAL,
    DEFAULT_NIGHT_SKIP,
    DEFAULT_PANEL_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_API_UNREACHABLE,
    METRIC_IIN,
    METRIC_PIN,
    METRIC_RECLAIMED,
    METRIC_RSSI,
    METRIC_VIN,
    OUTAGE_ISSUE_AFTER,
)
from .topology import PanelMeta, Topology, build_topology

_LOGGER = logging.getLogger(__name__)


async def _async_recorder_last_state(hass: HomeAssistant, entity_id: str):
    """Ultimo stato numerico registrato dal recorder (None se assente).

    Gli ultimi record possono essere ``unavailable``/vuoti (spegnimento,
    ricaricamento): si scorrono gli ultimi 60 giorni dal più recente.
    """
    try:
        from homeassistant.components.recorder import get_instance, history
    except ImportError:
        return None
    if "recorder" not in hass.config.components:
        return None
    try:
        states = await get_instance(hass).async_add_executor_job(
            partial(
                history.state_changes_during_period,
                hass,
                dt_util.utcnow() - timedelta(days=60),
                entity_id=entity_id,
                descending=True,
                limit=100,
                include_start_time_state=False,
            )
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Recorder lookup for %s failed: %s", entity_id, err)
        return None
    for state in states.get(entity_id) or states.get(entity_id.lower()) or []:
        if _as_float(state) is not None:
            return state
    return None


def _as_float(state) -> float | None:
    try:
        return float(state.state) if state is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class _EnergyState:
    """Daily-reset Wh -> monotonic cumulative kWh accumulator."""

    cumulative_wh: float = 0.0
    d_prev: float = 0.0
    # True dopo un seed senza il valore "oggi" noto: la prima lettura fa solo
    # da riferimento (niente doppio conteggio dell'energia già nel lifetime).
    baseline_pending: bool = False

    def seed(self, lifetime_kwh: float, today_kwh: float | None) -> None:
        self.cumulative_wh = float(lifetime_kwh) * 1000.0
        if today_kwh is None:
            self.baseline_pending = True
        else:
            self.d_prev = float(today_kwh) * 1000.0

    def apply(self, current_wh: float | None) -> None:
        if current_wh is None:
            return
        if self.baseline_pending:
            self.baseline_pending = False
            self.d_prev = current_wh
            return
        if current_wh >= self.d_prev:
            self.cumulative_wh += current_wh - self.d_prev
        else:  # daily/meter reset detected within the day
            self.cumulative_wh += current_wh
        self.d_prev = current_wh

    def rollover(self) -> None:
        # New local day: next reading is the day's first; keep cumulative.
        self.d_prev = 0.0

    @property
    def lifetime_kwh(self) -> float:
        return round(self.cumulative_wh / 1000.0, 3)

    @property
    def today_kwh(self) -> float:
        return round(self.d_prev / 1000.0, 3)


@dataclass
class _Backoff:
    failures: int = 0
    next_allowed: float = 0.0  # time.monotonic() gate

    def ok(self) -> bool:
        return time.monotonic() >= self.next_allowed

    def hold(self, seconds: float) -> None:
        self.next_allowed = time.monotonic() + max(seconds, 0.0)

    def on_success(self) -> None:
        self.failures = 0
        self.next_allowed = 0.0

    def on_failure(self) -> float:
        self.failures += 1
        step = min(BACKOFF_BASE * (2 ** (self.failures - 1)), BACKOFF_MAX)
        jitter = step * 0.2 * random.random()
        delay = step + jitter
        self.hold(delay)
        return delay


@dataclass
class _RuntimeMeta:
    tz: ZoneInfo | None = None
    current_date: str = ""
    sunrise_h: float = 0.0
    sunset_h: float = 24.0
    has_premium: bool = False
    features: set[str] = field(default_factory=set)
    capabilities: dict[str, Any] = field(default_factory=dict)
    last_lastdata: str | None = None


class TigoDataUpdateCoordinator(DataUpdateCoordinator):
    """One coordinator; all panels read from its data dict."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client,
        system_id: int,
    ) -> None:
        self.entry = entry
        self.client = client
        self.system_id = system_id
        self.topology: Topology | None = None

        opts = {**entry.data, **entry.options}
        self._scan = int(
            opts.get(CONF_PANEL_SCAN_INTERVAL, DEFAULT_PANEL_SCAN_INTERVAL)
        )
        self._energy_every = int(
            opts.get(CONF_ENERGY_POLL_INTERVAL, DEFAULT_ENERGY_POLL_INTERVAL)
        )
        self._night_skip = bool(opts.get(CONF_NIGHT_SKIP, DEFAULT_NIGHT_SKIP))
        self._probe_extra = bool(opts.get(CONF_PROBE_EXTRA_HARDWARE, False))
        self.extra_probe: dict[str, Any] = {}

        # Options force a metric on regardless of entity state. Optional
        # metrics are otherwise fetched on demand: as soon as the user
        # enables one of their (disabled-by-default) entities.
        self._forced_metrics = {METRIC_PIN}
        if opts.get(CONF_ENABLE_VOLTAGE):
            self._forced_metrics.add(METRIC_VIN)
        if opts.get(CONF_ENABLE_CURRENT):
            self._forced_metrics.add(METRIC_IIN)
        if opts.get(CONF_ENABLE_RSSI):
            self._forced_metrics.add(METRIC_RSSI)

        self._meta = _RuntimeMeta()
        self._backoff = _Backoff()
        self._panel_vals: dict[str, dict[str, float | None]] = {}
        self._sys_energy = _EnergyState()
        self._panel_energy: dict[str, _EnergyState] = {}
        self._last_energy_fetch = 0.0
        self._disabled_metrics: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            name="Tigo",
            update_interval=timedelta(seconds=self._scan),
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _now(self) -> datetime:
        return datetime.now(self._meta.tz) if self._meta.tz else datetime.now()

    async def _refresh_system_info(self, today: date) -> None:
        info = await self.client.get_system_info(
            self.system_id, today.isoformat()
        )
        if not isinstance(info, dict):
            return
        feats = info.get("features") or []
        self._meta.features = {str(f) for f in feats}
        self._meta.has_premium = bool(info.get("has_premium", False))
        day = info.get(today.isoformat()) or {}
        if isinstance(day, dict):
            self._meta.sunrise_h = float(day.get("sunrise", 0.0) or 0.0)
            self._meta.sunset_h = float(day.get("sunset", 24.0) or 24.0)
            tzname = day.get("timezone")
            if tzname:
                try:
                    self._meta.tz = ZoneInfo(tzname)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Unknown Tigo timezone %r", tzname)

    def _is_night(self, now: datetime) -> bool:
        if not self._night_skip:
            return False
        hour = now.hour + now.minute / 60.0
        return hour < (self._meta.sunrise_h - 0.25) or hour > (
            self._meta.sunset_h + 0.5
        )

    def _cca_uids(self) -> list[str]:
        if self.topology and self.topology.cca_uids:
            return self.topology.cca_uids
        return [str(self.system_id)]

    @staticmethod
    def _minute_index(t: str) -> int:
        try:
            hh, mm = t.split(":")[:2]
            return int(hh) * 60 + int(mm)
        except Exception:  # noqa: BLE001
            return -1

    # ------------------------------------------------------------------ #
    # main update
    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, Any]:
        # Backoff / throttle gate: skip the network entirely, keep last data.
        if not self._backoff.ok():
            if self.data is not None:
                return self.data
            raise UpdateFailed("Tigo API backing off (no data yet)")

        try:
            data = await self._do_update()
        except TigoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TigoThrottleError as err:
            wait = err.retry_after if err.retry_after else self._backoff.on_failure()
            if err.retry_after:
                self._backoff.hold(err.retry_after)
                self._backoff.failures += 1
            _LOGGER.warning(
                "Tigo API throttled; honoring Retry-After=%ss", round(wait)
            )
            self._maybe_raise_outage_issue()
            raise UpdateFailed(f"Tigo API throttled (retry in {round(wait)}s)") from err
        except Exception as err:  # noqa: BLE001
            delay = self._backoff.on_failure()
            _LOGGER.warning(
                "Tigo API update failed (%s); backing off %ss", err, round(delay)
            )
            self._maybe_raise_outage_issue()
            raise UpdateFailed(str(err)) from err

        self._backoff.on_success()
        self._clear_outage_issue()
        return data

    async def _do_update(self) -> dict[str, Any]:
        now = self._now()
        today = now.date()
        date_str = today.isoformat()

        # First run / topology.
        if self.topology is None:
            layout = {}
            try:
                layout = await self.client.get_system_layout(self.system_id)
            except TigoApiError as err:
                _LOGGER.info(
                    "Tigo layout unavailable (%s); telemetry-only mapping", err
                )
            equipments = await self.client.get_equipments(self.system_id)
            self.topology = build_topology(layout, equipments)
            try:
                self._meta.capabilities = await self.client.get_capabilities(
                    self.system_id
                )
            except TigoApiError:
                self._meta.capabilities = {}
            if self._probe_extra and hasattr(
                self.client, "probe_extra_hardware"
            ):
                try:
                    self.extra_probe = await self.client.probe_extra_hardware(
                        self.system_id
                    )
                except TigoApiError as err:
                    _LOGGER.debug("Extra-hardware probe failed: %s", err)
            await self._refresh_system_info(today)
            self._meta.current_date = date_str

        # Day rollover.
        if date_str != self._meta.current_date:
            _LOGGER.debug("Tigo day rollover %s -> %s", self._meta.current_date, date_str)
            self._sys_energy.rollover()
            for st in self._panel_energy.values():
                st.rollover()
            self._meta.last_lastdata = None
            await self._refresh_system_info(today)
            self._meta.current_date = date_str

        night = self._is_night(now)

        # --- per-minute telemetry ---
        if not night:
            await self._fetch_telemetry(date_str)
        else:
            _LOGGER.debug("Tigo night skip: telemetry fetch suppressed")

        # --- per-panel / system daily energy ---
        if (
            time.monotonic() - self._last_energy_fetch >= self._energy_every
            or self._last_energy_fetch == 0.0
            or night  # ensure a final capture after sunset / after midnight
        ):
            await self._fetch_energy(date_str)
            self._last_energy_fetch = time.monotonic()

        return self._build_result()

    # metric -> entity unique_id suffix (matches sensor.PANEL_METRICS)
    _METRIC_SUFFIX = {
        METRIC_PIN: "power",
        METRIC_VIN: "voltage",
        METRIC_IIN: "current",
        METRIC_RSSI: "rssi",
        METRIC_RECLAIMED: "reclaimed",
    }

    def _active_metrics(self) -> list[str]:
        """pin always; an optional metric is active if forced via options
        OR at least one of its entities is enabled in the registry."""
        active = set(self._forced_metrics)
        try:
            reg = er.async_get(self.hass)
            enabled_suffixes = {
                ent.unique_id.rsplit("_", 1)[-1]
                for ent in er.async_entries_for_config_entry(
                    reg, self.entry.entry_id
                )
                if ent.disabled_by is None and ent.unique_id
            }
            for metric, suffix in self._METRIC_SUFFIX.items():
                if suffix in enabled_suffixes:
                    active.add(metric)
        except Exception:  # noqa: BLE001 - registry not ready -> forced only
            pass
        # Stable order: pin, vin, iin, rssi, reclaimedPower
        return [
            m
            for m in (
                METRIC_PIN,
                METRIC_VIN,
                METRIC_IIN,
                METRIC_RSSI,
                METRIC_RECLAIMED,
            )
            if m in active
        ]

    async def _fetch_telemetry(self, date_str: str) -> None:
        # CCA-cadence skip: probe pin first; if lastData unchanged, the other
        # metrics produced no new minute either -> skip them this cycle.
        metrics = [
            m for m in self._active_metrics() if m not in self._disabled_metrics
        ]
        advanced = True
        for metric in metrics:
            if metric != METRIC_PIN and not advanced:
                continue
            for uid in self._cca_uids():
                try:
                    payload = await self.client.get_panel_summary(
                        self.system_id, date_str, metric, uid
                    )
                except TigoApiError as err:
                    if err.status == 403:
                        _LOGGER.warning(
                            "Tigo metric %s not entitled (403); disabling for "
                            "this session",
                            metric,
                        )
                        self._disabled_metrics.add(metric)
                        break
                    if err.status in (404, 422):
                        break  # absent for this account; ignore quietly
                    raise
                last_data = payload.get("lastData")
                if metric == METRIC_PIN:
                    advanced = last_data != self._meta.last_lastdata
                    self._meta.last_lastdata = last_data
                self._apply_summary(metric, payload)

    def _debug_summary_shape(self, metric: str, ds0: dict) -> None:
        """Compact DEBUG dump of the summary d[] layout vs. the topology.

        The full-day payload is ~330 KB, so log_raw truncates it to the
        (empty) night rows. This logs only the richest row, the payload's
        ``order`` list, and the resolved column->panel map, so users can
        confirm the per-panel columns are populated AND mapped to the right
        panel. Issues #7 (population) and #8 (ordering).
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG) or self.topology is None:
            return
        rows = ds0.get("data") or []
        best: list | None = None
        best_t = ""
        best_cnt = -1
        for row in rows:
            d = row.get("d") or []
            cnt = sum(1 for v in d if v not in ("-", "", None))
            if cnt > best_cnt:
                best_cnt, best, best_t = cnt, d, row.get("t", "")
        order = ds0.get("order") or ds0.get("orders") or []
        col_map = self._summary_column_map(ds0)
        used_order = bool(order) and col_map != dict(self.topology.by_index)
        _LOGGER.debug(
            "SUMMARY-SHAPE[%s]: order len=%d sample=%s | resolved via=%s | "
            "col_map size=%d sample=%s",
            metric,
            len(order),
            order[:6],
            "order[]" if used_order else "positional by_index",
            len(col_map),
            [(i, col_map[i].equipment_id) for i in sorted(col_map)[:6]],
        )
        if best is None:
            _LOGGER.debug("SUMMARY-SHAPE[%s]: no rows", metric)
            return
        filled = [(i, v) for i, v in enumerate(best) if v not in ("-", "", None)]
        mapped = [
            (i, v, col_map[i].equipment_id) for i, v in filled if i in col_map
        ]
        unmapped = [(i, v) for i, v in filled if i not in col_map]
        _LOGGER.debug(
            "SUMMARY-SHAPE[%s]: richest t=%s len(d)=%d non_dash=%d | "
            "filled->panel=%s | filled->UNMAPPED(aggregate?)=%s",
            metric,
            best_t,
            len(best),
            best_cnt,
            mapped[:50],
            unmapped[:50],
        )

    def _summary_column_map(self, ds0: dict) -> dict[int, PanelMeta]:
        """Map each d[] column index -> PanelMeta using the payload's own order.

        The summary payload carries a ground-truth ``order`` list whose i-th
        entry identifies the panel in d[i]. This is authoritative: the d[]
        column order does NOT match ``/api/v4/equipments`` (which is
        alphabetical: A1, A10, A11, A2, ...), so mapping by the equipments
        index silently assigns each panel its neighbour's reading (issue #8).
        On a fully healthy array every column still shows a plausible value,
        so the swap is invisible unless a panel is dark.

        ``order`` entries may be object_ids (aggenergy keys) or equipmentIds;
        match against both. Fall back to the positional equipments index only
        when no order is present (older payloads), warning if an order exists
        but resolves nothing.
        """
        assert self.topology is not None
        order = ds0.get("order") or ds0.get("orders") or []
        if order:
            col: dict[int, PanelMeta] = {}
            for idx, ident in enumerate(order):
                key = str(ident)
                meta = self.topology.by_object_id.get(
                    key
                ) or self.topology.by_equipment_id.get(key)
                if meta is not None:
                    col[idx] = meta
            if col:
                return col
            _LOGGER.warning(
                "Tigo summary 'order' present (%d entries) but none matched the "
                "topology; falling back to positional mapping (issue #8)",
                len(order),
            )
        return dict(self.topology.by_index)

    def _apply_summary(self, metric: str, payload: dict) -> None:
        dataset = payload.get("dataset") or []
        if not dataset:
            return
        ds0 = dataset[0]
        rows = ds0.get("data") or []
        self._debug_summary_shape(metric, ds0)
        assert self.topology is not None

        # Column -> panel via the payload's ground-truth order (issue #8).
        col_map = self._summary_column_map(ds0)

        # Per-panel-column latest-valid selection (issue #7). The old code
        # picked ONE newest row where *any* column was non-dash, then mapped
        # its whole d[]. But the trailing CCA/aggregate columns are non-dash in
        # every minute of the day -- including night and not-yet-reached
        # minutes -- so "newest such row" always walked to the end-of-day row,
        # where the per-panel columns are still "-", collapsing every panel to
        # null. This is the same failure the v2.0.1 changelog fixed for the v3
        # CSV path, unfixed here.
        #
        # Fix: scan every row and, per panel column, keep the highest-minute
        # non-dash value. Only panel columns (col_map) are considered, so the
        # always-fresh aggregate columns can't hijack selection.
        latest: dict[int, tuple[int, Any]] = {}
        for row in rows:
            mi = self._minute_index(row.get("t", ""))
            if mi < 0:
                continue
            vals = row.get("d") or []
            for idx in col_map:
                if idx >= len(vals):
                    continue
                raw = vals[idx]
                if raw in ("-", "", None):
                    continue
                prev = latest.get(idx)
                if prev is None or mi > prev[0]:
                    latest[idx] = (mi, raw)
        if not latest:
            return  # nothing populated yet today -> carry forward
        for idx, (_mi, raw) in latest.items():
            meta = col_map[idx]
            try:
                val = round(float(raw), 2)
            except (TypeError, ValueError):
                val = None
            self._panel_vals.setdefault(meta.equipment_id, {})[metric] = val

    async def _fetch_energy(self, date_str: str) -> None:
        try:
            agg = await self.client.get_agg_energy(self.system_id, date_str)
        except TigoApiError as err:
            if err.status in (403, 404, 422):
                return
            raise
        ds = agg.get("dataset") or {}
        for obj_id, wh in ds.items():
            st = self._panel_energy.setdefault(str(obj_id), _EnergyState())
            try:
                st.apply(float(wh))
            except (TypeError, ValueError):
                continue
        stats = agg.get("dailyStats") or {}
        total = stats.get("total_agg_energy")
        if total is not None:
            try:
                self._sys_energy.apply(float(total))
            except (TypeError, ValueError):
                pass

    def _build_result(self) -> dict[str, Any]:
        assert self.topology is not None
        panels: dict[str, dict[str, Any]] = {}
        sys_power = 0.0
        for meta in self.topology.panels:
            vals = dict(self._panel_vals.get(meta.equipment_id, {}))
            pin = vals.get(METRIC_PIN)
            if isinstance(pin, (int, float)):
                sys_power += pin
            est = (
                self._panel_energy.get(meta.object_id)
                if meta.object_id
                else None
            )
            if est is not None:
                vals["energy_kwh_lifetime"] = est.lifetime_kwh
                vals["energy_kwh_today"] = est.today_kwh
            panels[meta.equipment_id] = vals
        return {
            "panels": panels,
            "system": {
                "power_w": round(sys_power, 2),
                "lifetime_energy_kwh": self._sys_energy.lifetime_kwh,
                "today_energy_kwh": self._sys_energy.today_kwh,
            },
            "meta": {
                "has_premium": self._meta.has_premium,
                "features": sorted(self._meta.features),
                "capabilities": self._meta.capabilities,
                "last_data": self._meta.last_lastdata,
                "disabled_metrics": sorted(self._disabled_metrics),
            },
        }

    # ------------------------------------------------------------------ #
    # restore seeding: chiamato PRIMA del primo refresh, così i contatori
    # lifetime monotoni (Energy Dashboard) riprendono dall'ultimo valore
    # invece di ripartire dall'energia di oggi a ogni riavvio di HA.
    # ------------------------------------------------------------------ #
    async def async_seed_from_restore(self) -> None:
        ent_reg = er.async_get(self.hass)
        stored = restore_state.async_get(self.hass).last_states
        today = dt_util.now().date()

        async def last(unique_id: str):
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
            if not entity_id:
                return None, None
            st = stored.get(entity_id)
            state = st.state if st else None
            if _as_float(state) is None:
                # Nessuno stato salvato valido (es. entità migrate da un sensore
                # che non era RestoreEntity): ultimo valore dal recorder.
                state = await _async_recorder_last_state(self.hass, entity_id)
            value = _as_float(state)
            if value is None:
                return None, None
            same_day = dt_util.as_local(state.last_updated).date() == today
            return value, (state if same_day else None)

        def today_of(state) -> float | None:
            """kWh di oggi già inclusi nel lifetime salvato (None = ignoto)."""
            if state is None:
                return 0.0  # salvato in un giorno precedente: oggi riparte da 0
            if "today_kwh" in state.attributes:
                try:
                    return float(state.attributes["today_kwh"])
                except (TypeError, ValueError):
                    return None
            return None

        lifetime, state = await last(f"tigo_{self.system_id}_production")
        if lifetime is not None:
            today_kwh = today_of(state)
            if today_kwh is None and state is not None:
                value, same = await last(f"tigo_{self.system_id}_production_today")
                if value is not None and same is not None:
                    today_kwh = value
            self._sys_energy.seed(lifetime, today_kwh)

        for entry in er.async_entries_for_config_entry(ent_reg, self.entry.entry_id):
            uid = entry.unique_id or ""
            if entry.domain != "sensor" or not uid.endswith("_energy"):
                continue
            object_id = uid[len("tigo_"):-len("_energy")]
            lifetime, state = await last(uid)
            if lifetime is not None:
                self._panel_energy.setdefault(object_id, _EnergyState()).seed(
                    lifetime, today_of(state)
                )

    # ------------------------------------------------------------------ #
    # outage repair issue
    # ------------------------------------------------------------------ #
    def _maybe_raise_outage_issue(self) -> None:
        if self._backoff.failures < OUTAGE_ISSUE_AFTER:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_API_UNREACHABLE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_API_UNREACHABLE,
        )

    def _clear_outage_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_API_UNREACHABLE)
