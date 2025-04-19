import requests
from datetime import datetime, timedelta
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
                                        value = 0  # Sostituiamo '-' o valori invalidi con 0.0
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
    base_url = f"http://{ip}/cgi-bin/summary_data"
    today = datetime.now().date()
    history = []
    daily_energy = 0.0

    def get_day_energy(date_str: str) -> float:
        try:
            params = {"date": date_str, "temp": "pin", "_": int(time.time())}
            r = requests.get(base_url, headers=AUTH_HEADER, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            dataset = data.get("dataset", [])

            total_wh = 0
            for block in dataset:
                for entry in block.get("data", []):
                    values = entry.get("d", [])
                    minute_sum = sum(
                        float(v) for v in values
                        if isinstance(v, (int, float, str)) and str(v).replace('.', '', 1).isdigit()
                    )
                    total_wh += minute_sum / 60  # Wh per minuto

            return round(total_wh / 1000, 2)  # kWh
        except Exception as e:
            _LOGGER.warning(f"Errore nel recupero dei dati per {date_str}: {e}")
            return 0.0

    for i in range(6, -1, -1):
        date_obj = today - timedelta(days=i)
        date_str = date_obj.isoformat()
        is_today = date_obj == today
        is_midnight = datetime.now().hour == 0

        energy = get_day_energy(date_str)
        if not is_today and not is_midnight and energy == 0:
            _LOGGER.debug(f"Valore 0 per {date_str}, provo a ricalcolare (HA riavviato?)")
            energy = get_day_energy(date_str)

        history.append([date_str, energy])
        if is_today:
            daily_energy = energy

    today_str = today.isoformat()
    today_energy = 0
    previous_days = []

    for date_str, value in history:
        if date_str.strip() == today_str:
            today_energy = value
        else:
            previous_days.append((date_str, value))
    
    yesterday_energy = previous_days[-1][1] if len(previous_days) >= 1 else 0
    _LOGGER.debug(f"Filtrati per weekly_energy: {[d for d, _ in previous_days[-7:]]}")
    weekly_energy = sum(val for _, val in previous_days[-7:])
    
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
    
    

    


