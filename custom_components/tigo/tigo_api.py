import requests
from datetime import datetime, timedelta
import logging
import time
import websocket
import json
from requests.adapters import HTTPAdapter

_LOGGER = logging.getLogger(__name__)

AUTH_HEADER = {
    "Authorization": "Basic VGlnbzokb2xhcg==",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0"
}

_session: requests.Session | None = None

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        adapter = HTTPAdapter(max_retries=0, pool_connections=10, pool_maxsize=10)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
    return _session

_LAST_LOG = {}  # {key: epoch}
def _log_throttled(key: str, level: int, msg: str, min_interval_sec: int = 1800) -> None:
    now = time.time()
    last = _LAST_LOG.get(key, 0)
    if now - last >= min_interval_sec:
        _LAST_LOG[key] = now
        _LOGGER.log(level, msg)

def _get_json(url: str, *, params: dict | None = None, timeout: float = 6.0) -> dict | list | None:
    s = _get_session()
    try:
        r = s.get(url, headers=AUTH_HEADER, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectTimeout:
        _log_throttled(f"timeout:{url}", logging.INFO, f"Tigo timeout su {url}: probabile standby notturno")
        return None
    except requests.exceptions.ReadTimeout:
        _log_throttled(f"readtimeout:{url}", logging.INFO, f"Tigo read-timeout su {url}: probabile standby notturno")
        return None
    except requests.exceptions.ConnectionError as e:
        _log_throttled(f"connerr:{url}", logging.INFO, f"Tigo non raggiungibile ({e.__class__.__name__}): {url}")
        return None
    except ValueError as e:
        _log_throttled(f"badjson:{url}", logging.WARNING, f"Tigo JSON non valido da {url}: {e}")
        return None
    except Exception as e:
        _log_throttled(f"generic:{url}", logging.WARNING, f"Errore generico su {url}: {e}")
        return None

def fetch_tigo_data_from_ws(ws_url: str) -> dict:
    """Legge i dati dal WS e li restituisce come dict {panel_id: {...}}"""
    try:
        ws = websocket.create_connection(ws_url, timeout=5)
        raw = ws.recv()
        ws.close()
        data_list = json.loads(raw) if raw else []
    except Exception as e:
        _log_throttled(f"ws:{ws_url}", logging.INFO, f"Errore WS fetch: {e}")
        return {}

    panel_data = {}
    for mod in data_list or []:
        barcode = mod.get("barcode") or None
        addr = mod.get("addr") or None
        generic_id = mod.get("id")
        panel_id = barcode or addr or generic_id
        if not panel_id:
            continue
        panel_data[panel_id] = {
            "Pin": mod.get("watt", 0),
            "Vin": mod.get("vin", 0),
            "Vout": mod.get("vout", 0),
            "Iin": mod.get("amp", 0),
            "Temp": mod.get("temp", 0),
            "Rssi": mod.get("rssi", 0),
            "Addr": addr,
            "Barcode": barcode,
            "GenericID": generic_id,
        }
    return panel_data


def fetch_tigo_data_from_ip(ip: str) -> dict:
    base_url = f"http://{ip}/cgi-bin/summary_data"
    date = datetime.now().date().isoformat()
    temps = ["vin", "pin", "rssi"]

    panel_data: dict[str, dict] = {}

    params = {"date": date, "temp": "pin", "_": int(time.time())}
    data = _get_json(base_url, params=params, timeout=6.0)
    if not isinstance(data, dict):
        return {}
    dataset = data.get("dataset", [])
    if not dataset or "order" not in dataset[0]:
        return {}

    panel_order = dataset[0]["order"]

    for temp in temps:
        params = {"date": date, "temp": temp, "_": int(time.time())}
        data = _get_json(base_url, params=params, timeout=6.0)
        if not isinstance(data, dict):
            continue
        ds = data.get("dataset", [])
        if not ds:
            continue

        for block in reversed(ds):
            current_order = block.get("order") or panel_order
            for entry in reversed(block.get("data", [])):
                raw = entry.get("d")
                if not raw:
                    continue
                for i, panel in enumerate(current_order):
                    if i < len(raw):
                        try:
                            val = float(raw[i])
                        except (TypeError, ValueError):
                            val = 0.0
                        k = temp.capitalize()
                        panel_data.setdefault(panel, {})[k] = val
                break
            if panel_data:
                break

    for panel, values in panel_data.items():
        try:
            vin = float(values.get("Vin", 0) or 0)
            pin = float(values.get("Pin", 0) or 0)
            values["Iin"] = round(pin / vin, 2) if vin > 0 else 0.0
        except (TypeError, ValueError):
            values["Iin"] = 0.0

    return panel_data or {}


def fetch_tigo_layout_from_ip(ip: str) -> dict:
    url = f"http://{ip}/cgi-bin/summary_config"
    data = _get_json(url, timeout=6.0)
    if not isinstance(data, list):
        return {}

    objects = {o["id"]: o for o in data if isinstance(o, dict) and "id" in o}
    layout = {"system": {"inverters": []}}

    for inverter_obj in [o for o in data if isinstance(o, dict) and o.get("type") == 4]:
        inverter = {
            "label": inverter_obj.get("label", "Inverter"),
            "object_id": inverter_obj.get("id"),
            "type": "Inverter",
            "mppts": []
        }
        for string_id in inverter_obj.get("children", []):
            string_obj = objects.get(string_id)
            if not string_obj or string_obj.get("type") != 3:
                continue
            string = {
                "label": string_obj.get("label", "String"),
                "object_id": string_obj.get("id"),
                "parent": string_obj.get("parent"),
                "type": "String",
                "panels": []
            }
            for panel_id in string_obj.get("children", []):
                panel_obj = objects.get(panel_id)
                if not panel_obj or panel_obj.get("type") != 2:
                    continue
                string["panels"].append({
                    "label": panel_obj.get("label"),
                    "serial": panel_obj.get("serial"),
                    "object_id": panel_obj.get("id"),
                    "type": "Panel",
                    "channel": panel_obj.get("channel"),
                    "MP": panel_obj.get("MP"),
                    "parent": panel_obj.get("parent"),
                })
            inverter["mppts"].append(string)
        layout["system"]["inverters"].append(inverter)

    return layout


def fetch_tigo_energy_history(ip: str) -> list[dict]:
    url = f"http://{ip}/cgi-bin/summary_energy"
    data = _get_json(url, timeout=6.0)
    if not isinstance(data, list):
        return []
    return [{"date": d[0], "energy_wh": d[1]} for d in data if isinstance(d, list) and len(d) == 2]

def fetch_daily_energy(ip: str) -> dict:
    base_url = f"http://{ip}/cgi-bin/summary_data"
    today = datetime.now().date()
    history = []

    def get_day_energy(date_str: str) -> float:
        params = {"date": date_str, "temp": "pin", "_": int(time.time())}
        data = _get_json(base_url, params=params, timeout=8.0)
        if not isinstance(data, dict):
            return 0.0
        dataset = data.get("dataset", [])
        total_wh = 0.0
        for block in dataset or []:
            for entry in block.get("data", []):
                values = entry.get("d", [])
                minute_sum = 0.0
                for v in values:
                    try:
                        minute_sum += float(v)
                    except (TypeError, ValueError):
                        pass
                total_wh += minute_sum / 60.0
        return round(total_wh / 1000.0, 2)

    for i in range(6, -1, -1):
        date_obj = today - timedelta(days=i)
        date_str = date_obj.isoformat()
        energy = get_day_energy(date_str)
        if date_obj != today and energy == 0.0:
            energy = get_day_energy(date_str)
        history.append([date_str, energy])

    weekly_energy = sum(val for _, val in history[-7:])
    yesterday_energy = history[-2][1] if len(history) >= 2 else 0.0
    today_energy = history[-1][1] if history else 0.0

    import calendar
    history_named = {
        f"{date_str} ({calendar.day_name[datetime.strptime(date_str, '%Y-%m-%d').weekday()]})": value
        for date_str, value in history
    }

    return {
        "today_energy": today_energy,
        "yesterday_energy": yesterday_energy,
        "weekly_energy": weekly_energy,
        "history": history,
        "history_named": history_named,
    }

def fetch_device_info(ip: str) -> dict:
    url = f"http://{ip}/cgi-bin/mobile_api?cmd=DEVICE_INFO"
    data = _get_json(url, timeout=6.0)
    if not isinstance(data, dict):
        return {}

    status_entries = {item.get("name"): item.get("status") for item in data.get("status", []) if isinstance(item, dict)}
    readable_status = {
        "serial": data.get("serial"),
        "software": data.get("software"),
        "sysid": data.get("sysid"),
        "last_data_sync": next((k for k in status_entries if "Last Data Sync" in k), None),
        "discovery": next((k for k in status_entries if "Discovery" in k), None),
        "kernel": next((k for k in status_entries if "Kernel" in k), None),
    }
    return readable_status

