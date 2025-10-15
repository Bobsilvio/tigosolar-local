import requests
from datetime import datetime, timedelta
import logging
import time
import websocket
import json

_LOGGER = logging.getLogger(__name__)

AUTH_HEADER = {
    "Authorization": "Basic VGlnbzokb2xhcg==",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0"
}

def fetch_tigo_data_from_ws(ws_url: str) -> dict:
    """Legge i dati dal WS e li restituisce come dict {panel_id: {...}}"""
    try:
        ws = websocket.create_connection(ws_url, timeout=5)
        raw = ws.recv()
        ws.close()

        data_list = json.loads(raw)  # lista di moduli
        panel_data = {}
        for mod in data_list:
            panel_id = mod.get("id")
            if panel_id:
                panel_data[panel_id] = {
                    "Pin": mod.get("watt", 0),
                    "Vin": mod.get("vin", 0),
                    "Vout": mod.get("vout", 0),
                    "Iin": mod.get("amp", 0),
                    "Temp": mod.get("temp", 0),
                    "Rssi": mod.get("rssi", 0)
                }

        return panel_data

    except Exception as e:
        _LOGGER.warning(f"Errore WS fetch: {e}")
        return {}  # <-- MAI None, sempre dict



def fetch_tigo_data_from_ip(ip: str) -> dict:
    base_url = f"http://{ip}/cgi-bin/summary_data"
    date = datetime.now().date().isoformat()
    temps = ["vin", "pin", "rssi"]

    panel_data = {}
    panel_order = []

    def fetch_panel_order():
        try:
            params = {"date": date, "temp": "pin", "_": int(time.time())}
            r = requests.get(base_url, headers=AUTH_HEADER, params=params, timeout=10)
            r.raise_for_status()
            _LOGGER.debug(f"fetch_panel_order response: {r.text}")
            dataset = r.json().get("dataset", [])
            if dataset and "order" in dataset[0]:
                return dataset[0]["order"]
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
            _LOGGER.info("Tigo non raggiungibile (timeout): probabile standby notturno")
        except Exception as e:
            _LOGGER.warning(f"Errore nel recupero dell'ordine pannelli: {e}")
        return []

    panel_order = fetch_panel_order()
    if not panel_order:
        return {}

    for temp in temps:
        try:
            params = {"date": date, "temp": temp, "_": int(time.time())}
            r = requests.get(base_url, headers=AUTH_HEADER, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            dataset = data.get("dataset", [])
            if not dataset:
                continue

            for block in reversed(dataset):
                if "order" in block:
                    current_order = block["order"]
                    for entry in reversed(block.get("data", [])):
                        if entry.get("d"):
                            for i, panel in enumerate(current_order):
                                if i < len(entry["d"]):
                                    raw_value = entry["d"][i]
                                    try:
                                        value = float(raw_value)
                                    except (TypeError, ValueError):
                                        value = 0
                                    panel_data.setdefault(panel, {})[temp.capitalize()] = value
                            break
                if panel_data:
                    break

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
            _LOGGER.info(f"Tigo non risponde a '{temp}' (timeout): standby notturno")
        except Exception as e:
            _LOGGER.warning(f"Errore {temp}: {e}")
            continue

    for panel, values in panel_data.items():
        try:
            vin = float(values.get("Vin", 0))
            pin = float(values.get("Pin", 0))
            values["Iin"] = round(pin / vin, 2) if vin > 0 else 0
        except (TypeError, ValueError):
            _LOGGER.debug(f"Valori non validi per corrente su {panel}: Vin={values.get('Vin')}, Pin={values.get('Pin')}")
            values["Iin"] = 0

    return panel_data or {}

def fetch_tigo_layout_from_ip(ip: str) -> dict:
    try:
        url = f"http://{ip}/cgi-bin/summary_config"
        response = requests.get(url, headers=AUTH_HEADER, timeout=10)
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
        _LOGGER.info("Tigo non raggiungibile (timeout) per layout: standby notturno")
        return {}
    except Exception as e:
        _LOGGER.warning(f"Errore layout: {e}")
        return {}

    objects = {o["id"]: o for o in data if isinstance(o, dict) and "id" in o}
    layout = {"system": {"inverters": []}}

    for inverter_obj in [o for o in data if o.get("type") == 4]:
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
                panel = {
                    "label": panel_obj.get("label"),
                    "serial": panel_obj.get("serial"),
                    "object_id": panel_obj.get("id"),
                    "type": "Panel",
                    "channel": panel_obj.get("channel"),
                    "MP": panel_obj.get("MP"),
                    "parent": panel_obj.get("parent"),
                }
                string["panels"].append(panel)
            inverter["mppts"].append(string)
        layout["system"]["inverters"].append(inverter)

    return layout


def fetch_tigo_energy_history(ip: str) -> list[dict]:
    try:
        url = f"http://{ip}/cgi-bin/summary_energy"
        response = requests.get(url, headers=AUTH_HEADER, timeout=10)
        response.raise_for_status()
        data = response.json()
        return [{"date": d[0], "energy_wh": d[1]} for d in data if isinstance(d, list) and len(d) == 2]
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
        _LOGGER.info("Tigo non raggiungibile (timeout) per energy_history: standby notturno")
        return []
    except Exception as e:
        _LOGGER.warning(f"Errore fetch_tigo_energy_history: {e}")
        return []

def fetch_daily_energy(ip: str) -> dict:
    """
    Calcola l'energia prodotta dai pannelli Tigo,
    restituendo oggi, ieri, settimanale e mensile per ogni pannello.
    """
    base_url = f"http://{ip}/cgi-bin/summary_data"
    today = datetime.now().date()
    history: list[tuple[str, dict[int, float]]] = []  # [(data, {panel_index: kWh})]

    def get_day_energy(date_str: str) -> dict[int, float]:
        """Restituisce la produzione per pannello in kWh in una giornata."""
        panel_wh: dict[int, float] = {}
        try:
            params = {"date": date_str, "temp": "pin", "_": int(time.time())}
            r = requests.get(base_url, headers=AUTH_HEADER, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            dataset = data.get("dataset", [])

            for block in dataset:
                for entry in block.get("data", []):
                    values = entry.get("d", [])
                    for i, raw_v in enumerate(values):
                        try:
                            v = float(raw_v)
                        except (TypeError, ValueError):
                            v = 0.0
                        # Wh accumulati (per minuto)
                        panel_wh[i] = panel_wh.get(i, 0) + v / 60
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
            _LOGGER.info(f"Tigo non raggiungibile (timeout) per energy del {date_str}")
        except Exception as e:
            _LOGGER.warning(f"Errore nel recupero dei dati per {date_str}: {e}")

        # Converti in kWh
        return {i: round(val / 1000, 2) for i, val in panel_wh.items()}

    # Recupera ultimi 30 giorni
    for i in range(29, -1, -1):
        date_obj = today - timedelta(days=i)
        date_str = date_obj.isoformat()
        energy_per_panel = get_day_energy(date_str)
        history.append((date_str, energy_per_panel))

    # Oggi, ieri
    today_energy = history[-1][1] if history else {}
    yesterday_energy = history[-2][1] if len(history) > 1 else {}

    # Weekly (ultimi 7 giorni)
    weekly_energy: dict[int, float] = {}
    for _, day_data in history[-7:]:
        for panel_id, val in day_data.items():
            weekly_energy[panel_id] = weekly_energy.get(panel_id, 0) + val

    # Monthly (ultimi 30 giorni)
    monthly_energy: dict[int, float] = {}
    for _, day_data in history:
        for panel_id, val in day_data.items():
            monthly_energy[panel_id] = monthly_energy.get(panel_id, 0) + val

    import calendar
    history_named = {
        f"{date_str} ({calendar.day_name[datetime.strptime(date_str, '%Y-%m-%d').weekday()]})": day_data
        for date_str, day_data in history
    }

    return {
        "today_energy": today_energy,          # {panel: kWh}
        "yesterday_energy": yesterday_energy,  # {panel: kWh}
        "weekly_energy": weekly_energy,        # {panel: kWh}
        "monthly_energy": monthly_energy,      # {panel: kWh}
        "history": history,                    # lista [(data, {panel: kWh})]
        "history_named": history_named,        # dict {data: {panel: kWh}}
    }

def fetch_device_info(ip: str) -> dict:
    try:
        url = f"http://{ip}/cgi-bin/mobile_api?cmd=DEVICE_INFO"
        r = requests.get(url, headers=AUTH_HEADER, timeout=10)
        r.raise_for_status()
        data = r.json()

        status_entries = {item["name"]: item["status"] for item in data.get("status", [])}
        readable_status = {
            "serial": data.get("serial"),
            "software": data.get("software"),
            "sysid": data.get("sysid"),
            "last_data_sync": next((k for k in status_entries if "Last Data Sync" in k), None),
            "discovery": next((k for k in status_entries if "Discovery" in k), None),
            "kernel": next((k for k in status_entries if "Kernel" in k), None),
        }

        return readable_status
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
        _LOGGER.info("Tigo non raggiungibile (timeout) per device info: standby notturno")
        return {}
    except Exception as e:
        _LOGGER.warning(f"Errore nel recupero device info: {e}")
        return {}
