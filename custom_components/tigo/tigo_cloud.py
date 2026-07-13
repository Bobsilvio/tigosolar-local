"""Client per l'API cloud Tigo (mapi.tigoenergy.com).

Usato quando il firmware locale del CCA (>= 4.0.4) protegge gli endpoint
`/cgi-bin/*` con password e il dato locale non è più accessibile.

Replica le chiamate fatte dall'app iPhone:
  - POST /api/v3/user/login?type=8            -> token Bearer
  - GET  /api/v3/tigobuild/config             -> layout (pannelli/stringhe/inverter)
  - GET  /api/v4/system/summary/aggenergy     -> energia giornaliera per pannello (Wh)
  - GET  /api/v4/smart/systems/{id}/homepage  -> potenza istantanea + totali sistema
  - GET  /api/v4/system/summary/aggpower      -> curva potenza del giorno
  - GET  /api/v4/system/summary/calendar      -> storico energia giornaliera

NOTA: un account "Basic" (non premium) espone per-pannello solo l'energia
giornaliera cumulativa. Pin/Vin/Vout/Iin/Temp/Rssi istantanei per pannello NON
sono disponibili via cloud (richiedono premium): quelli restano ottenibili solo
dalla sorgente locale con firmware < 4.0.4.
"""
from __future__ import annotations

from datetime import datetime
import logging

import requests

from .const import CLOUD_BASE, CLOUD_HEADERS, _LOGGER


class TigoAuthError(Exception):
    """Credenziali cloud non valide o login rifiutato."""


class TigoCloudError(Exception):
    """Errore generico nella comunicazione col cloud Tigo."""


# Mappatura tipo-oggetto (campo "B") dal tigobuild/config
_TYPE_SYSTEM = 1
_TYPE_PANEL = 2
_TYPE_STRING = 3
_TYPE_INVERTER = 4
_TYPE_CCA = 44


class TigoCloudClient:
    """Wrapper sincrono attorno all'API cloud Tigo.

    Va usato dentro ``hass.async_add_executor_job`` perché usa ``requests``.
    Ri-effettua il login automaticamente se il token scade (401/403).
    """

    def __init__(
        self,
        username: str,
        password: str,
        system_id: int | None = None,
        token: str | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self.system_id = int(system_id) if system_id else None
        self._token = token
        self._session = requests.Session()
        # MAC del CCA (uid), necessario per la potenza per-pannello. Popolato da fetch_layout.
        self._cca_uid: str | None = None

    # --- Autenticazione -------------------------------------------------

    def login(self) -> str:
        """Esegue il login e memorizza il token Bearer. Ritorna il token."""
        url = f"{CLOUD_BASE}/api/v3/user/login?type=8"
        try:
            r = self._session.post(
                url,
                headers={**CLOUD_HEADERS, "content-type": "application/json"},
                json={"username": self._username, "password": self._password},
                timeout=15,
            )
        except requests.RequestException as e:
            raise TigoCloudError(f"Login non raggiungibile: {e}") from e

        if r.status_code in (401, 403):
            raise TigoAuthError("Username o password Tigo non validi")
        if r.status_code != 200:
            raise TigoCloudError(f"Login fallito (HTTP {r.status_code})")

        try:
            token = r.json()["user"]["auth"]
        except (ValueError, KeyError, TypeError) as e:
            raise TigoCloudError(f"Risposta login inattesa: {e}") from e

        self._token = token
        return token

    @property
    def token(self) -> str | None:
        return self._token

    def _get(self, path: str, *, _retry: bool = True) -> dict | list | None:
        """GET autenticato. Ri-esegue il login una volta su 401/403."""
        if not self._token:
            self.login()

        url = f"{CLOUD_BASE}{path}"
        headers = {**CLOUD_HEADERS, "authorization": f"Bearer {self._token}"}
        try:
            r = self._session.get(url, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise TigoCloudError(f"GET {path} fallita: {e}") from e

        if r.status_code in (401, 403) and _retry:
            _LOGGER.info("Token Tigo scaduto/rifiutato, ri-eseguo login")
            self.login()
            return self._get(path, _retry=False)

        if r.status_code != 200:
            _LOGGER.debug("GET %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
            return None

        try:
            return r.json()
        except ValueError:
            return None

    # --- Discovery ------------------------------------------------------

    def discover_systems(self) -> list[dict]:
        """Ritorna [{'system_id', 'name'}] degli impianti dell'account."""
        data = self._get(
            "/api/v3/systems/query?limit=50&include=images&page=1&sort=-id"
        )
        out: list[dict] = []
        if isinstance(data, dict):
            for s in data.get("systems", []):
                if isinstance(s, dict) and s.get("system_id"):
                    out.append(
                        {"system_id": s["system_id"], "name": s.get("name") or str(s["system_id"])}
                    )
        return out

    # --- Layout ---------------------------------------------------------

    def fetch_layout(self) -> dict:
        """Legge tigobuild/config e ritorna {object_id(str): {...}} per i pannelli.

        Ogni pannello: name, serial, short_serial, channel, watt_rating,
        string, inverter, mp.
        """
        sid = self.system_id
        data = self._get(
            f"/api/v3/tigobuild/config?system_id={sid}&resourceId=config"
        )
        panels: dict[str, dict] = {}
        if not isinstance(data, dict):
            return panels

        objects = data.get("system", {}).get("objects", [])
        if not isinstance(objects, list):
            return panels

        # Indicizza per id e raccogli nomi stringhe/inverter
        by_id = {o["A"]: o for o in objects if isinstance(o, dict) and "A" in o}

        def label_of(obj_id, want_type) -> str | None:
            obj = by_id.get(obj_id)
            if obj and obj.get("B") == want_type:
                return obj.get("C")
            return None

        # MAC del CCA (campo T dell'oggetto tipo 44): serve come uid per la potenza.
        for o in objects:
            if isinstance(o, dict) and o.get("B") == _TYPE_CCA and o.get("T"):
                self._cca_uid = o.get("T")
                break

        for o in objects:
            if not isinstance(o, dict) or o.get("B") != _TYPE_PANEL:
                continue
            oid = str(o.get("A"))
            string_id = o.get("K")
            string_obj = by_id.get(string_id) or {}
            inverter_id = string_obj.get("K")
            panels[oid] = {
                "name": o.get("C") or oid,
                "serial": o.get("V") or o.get("N"),
                "short_serial": o.get("T"),
                "channel": o.get("S"),
                "watt_rating": o.get("J"),
                "mp": o.get("M"),
                "string": label_of(string_id, _TYPE_STRING),
                "inverter": label_of(inverter_id, _TYPE_INVERTER),
            }
        return panels

    # --- Dati ------------------------------------------------------------

    def _today(self) -> str:
        return datetime.now().date().isoformat()

    def fetch_panel_energy(self, date_str: str | None = None) -> dict:
        """Energia giornaliera per pannello (Wh) + statistiche giornaliere.

        Ritorna {'panels': {object_id: {'energy_today_wh', 'last_data'}},
                 'total_energy_wh', 'reclaimed_wh', 'last_data'}.
        """
        sid = self.system_id
        date_str = date_str or self._today()
        data = self._get(
            f"/api/v4/system/summary/aggenergy?system_id={sid}"
            f"&date={date_str}&temp=energy&resourceId=data-{date_str}-energy"
        )
        out = {"panels": {}, "total_energy_wh": None, "reclaimed_wh": None, "last_data": None}
        if not isinstance(data, dict):
            return out

        dataset = data.get("dataset") or {}
        last_map = data.get("datasetLastData") or {}
        for oid, wh in dataset.items():
            out["panels"][str(oid)] = {
                "energy_today_wh": wh,
                "last_data": last_map.get(oid),
            }
        stats = data.get("dailyStats") or {}
        out["total_energy_wh"] = _to_float(stats.get("total_agg_energy"))
        out["reclaimed_wh"] = _to_float(stats.get("total_agg_reclaimed"))
        out["last_data"] = data.get("lastData")
        return out

    def fetch_panel_summary(self, temp: str, date_str: str | None = None) -> dict:
        """Serie a 15 min per pannello dall'endpoint summary/summary.

        ``temp`` = 'pin' (potenza) o 'reclaimedPower' (potenza recuperata).
        Ritorna {object_id(str): {'value', 'time'}} dall'ultimo intervallo con
        dati validi della giornata. Vin/Vout/Iin/Temp/Rssi restituiscono vuoto
        su un account Basic (non premium).
        """
        if not self._cca_uid:
            _LOGGER.debug("uid CCA assente: salto summary temp=%s", temp)
            return {}

        sid = self.system_id
        uid = self._cca_uid
        date_str = date_str or self._today()
        data = self._get(
            f"/api/v4/system/summary/summary?system_id={sid}&date={date_str}"
            f"&temp={temp}&uid={uid}&resourceId=data-{date_str}-{temp}-{uid}"
        )
        out: dict[str, dict] = {}
        if not isinstance(data, dict):
            return out
        dataset = data.get("dataset") or []
        if not dataset:
            return out
        block = dataset[0]
        order = block.get("order") or []
        rows = block.get("data") or []
        for row in reversed(rows):
            d = row.get("d") or []
            if any(x != "-" for x in d):
                for i, oid in enumerate(order):
                    if i < len(d) and d[i] != "-":
                        val = _to_float(d[i])
                        if val is not None:
                            out[str(oid)] = {"value": val, "time": row.get("t")}
                break
        return out

    def fetch_homepage(self) -> dict:
        """Totali sistema: potenza istantanea + energia day/week/month/year/lifetime (Wh)."""
        sid = self.system_id
        data = self._get(f"/api/v4/smart/systems/{sid}/homepage")
        out = {
            "power_now_w": None,
            "energy_day_wh": None,
            "energy_week_wh": None,
            "energy_month_wh": None,
            "energy_year_wh": None,
            "energy_lifetime_wh": None,
            "last_data": None,
        }
        if not isinstance(data, dict):
            return out
        ep = data.get("energyProduction") or {}
        out["power_now_w"] = _to_float(ep.get("now"))
        out["energy_day_wh"] = _to_float(ep.get("day"))
        out["energy_week_wh"] = _to_float(ep.get("week"))
        out["energy_month_wh"] = _to_float(ep.get("month"))
        out["energy_year_wh"] = _to_float(ep.get("year"))
        out["energy_lifetime_wh"] = _to_float(ep.get("lifetime"))
        out["last_data"] = data.get("minLastTime")
        return out

    def fetch_power_day_max(self, date_str: str | None = None) -> float | None:
        """Picco di potenza del giorno (W) dalla curva aggpower."""
        sid = self.system_id
        date_str = date_str or self._today()
        data = self._get(
            f"/api/v4/system/summary/aggpower?system_id={sid}&date={date_str}"
        )
        if isinstance(data, dict):
            return _to_float(data.get("dayMax"))
        return None

    def fetch_calendar(self) -> list[list]:
        """Storico energia giornaliera: [[date, wh], ...] (solo giorni con dato)."""
        sid = self.system_id
        data = self._get(
            f"/api/v4/system/summary/calendar?systemId={sid}&output=arr"
        )
        if isinstance(data, list):
            return [row for row in data if isinstance(row, list) and len(row) == 2 and row[1] is not None]
        return []

    # --- Aggregazione per il coordinator --------------------------------

    def fetch_all(self, layout: dict | None = None) -> dict:
        """Raccoglie tutto in un unico dict per il DataUpdateCoordinator."""
        if not self.system_id:
            raise TigoCloudError("system_id non impostato")

        energy = self.fetch_panel_energy()
        power = self.fetch_panel_summary("pin")
        reclaimed = self.fetch_panel_summary("reclaimedPower")
        home = self.fetch_homepage()
        power_max = self.fetch_power_day_max()

        # Fonde layout (statico) + energia/potenza/recuperato (dinamici) per pannello
        panels: dict[str, dict] = {}
        layout = layout or {}
        panel_ids = (
            set(layout.keys())
            | set(energy["panels"].keys())
            | set(power.keys())
            | set(reclaimed.keys())
        )
        for oid in panel_ids:
            base = dict(layout.get(oid, {}))
            base.update(energy["panels"].get(oid, {}))
            p = power.get(oid)
            if p:
                base["power_w"] = p["value"]
                base["power_time"] = p["time"]
            r = reclaimed.get(oid)
            if r:
                base["reclaimed_w"] = r["value"]
            base.setdefault("name", oid)
            panels[oid] = base

        system = {
            "power_now_w": home["power_now_w"],
            "power_day_max_w": power_max,
            "energy_today_wh": home["energy_day_wh"] if home["energy_day_wh"] is not None else energy["total_energy_wh"],
            "energy_week_wh": home["energy_week_wh"],
            "energy_month_wh": home["energy_month_wh"],
            "energy_year_wh": home["energy_year_wh"],
            "energy_lifetime_wh": home["energy_lifetime_wh"],
            "reclaimed_today_wh": energy["reclaimed_wh"],
            "last_data": home["last_data"] or energy["last_data"],
        }
        return {"panels": panels, "system": system}


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
