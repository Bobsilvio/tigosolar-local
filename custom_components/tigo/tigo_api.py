import requests
from datetime import datetime
import logging
import time

_LOGGER = logging.getLogger(__name__)

AUTH_HEADER = {
    "Authorization": "Basic VGlnbzokb2xhcg==",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0"
}

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
            dataset = r.json().get("dataset", [])
            if dataset and "order" in dataset[0]:
                return dataset[0]["order"]
        except Exception as e:
            _LOGGER.warning(f"Errore nel recupero dell'ordine pannelli: {e}")
        return []

    panel_order = fetch_panel_order()

    for temp in temps:
        try:
            params = {"date": date, "temp": temp, "_": int(time.time())}
            r = requests.get(base_url, headers=AUTH_HEADER, params=params, timeout=10)
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

        except Exception as e:
            _LOGGER.warning(f"Errore {temp}: {e}")
            continue

    for panel, values in panel_data.items():
        try:
            vin = float(values.get("Vin", 0))
            pin = float(values.get("Pin", 0))
            if vin > 0:
                values["Iin"] = round(pin / vin, 2)
            else:
                values["Iin"] = 0
        except (TypeError, ValueError):
            _LOGGER.debug(f"Valori non validi per corrente su {panel}: Vin={values.get('Vin')}, Pin={values.get('Pin')}")
            values["Iin"] = 0
    
    

    return panel_data

def fetch_tigo_layout_from_ip(ip: str) -> dict:
    try:
        url = f"http://{ip}/cgi-bin/summary_config"
        response = requests.get(url, headers=AUTH_HEADER, timeout=10)
        data = response.json()
    except Exception as e:
        _LOGGER.error(f"Errore layout: {e}")
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
        data = response.json()
        return [{"date": d[0], "energy_wh": d[1]} for d in data if isinstance(d, list) and len(d) == 2]
    except Exception as e:
        _LOGGER.warning(f"Errore fetch_tigo_energy_history: {e}")
        return []

def fetch_daily_energy(ip: str) -> dict:
    url = f"http://{ip}/cgi-bin/summary_energy"

    try:
        response = requests.get(url, headers=AUTH_HEADER, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        _LOGGER.warning(f"Errore nel recupero della produzione giornaliera da {url}: {e}")
        return {"daily_energy": 0, "history": []}

    history = data[-7:] if len(data) >= 7 else data
    today = datetime.now().date().isoformat()
    today_value = next((v[1] for v in reversed(data) if v[0] == today), 0)

    return {
        "daily_energy": today_value,
        "history": history
    }

