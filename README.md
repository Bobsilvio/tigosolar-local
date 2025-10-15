# Tigo Local Integration for Home Assistant (v2.0.0)

A custom integration that connects Home Assistant to **Tigo CCA** (Cloud Connect Advanced) and **ESP32 solar modules** via the local network, without using cloud services or API keys.  
It fetches real-time and historical solar panel data directly from the devices.

⚠️ **Do not use with Tigosolar-online**

---

## 🧩 HACS

To be added when available via HACS.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bobsilvio&repository=tigosolar-local&category=integration)

---

## ⚙️ MANUAL INSTALL

1. Copy the `tigo` directory into:  
   `/config/custom_components/`
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, and search for **Tigo Local**.
4. Enter the **local IP address** of your **Tigo CCA** or **ESP32 module** (e.g. `192.168.1.100`).

> ⚠️ Do **not** include `http://` or use hostnames like `tigo.local`.

---

## 🖼️ Image Previews

<img src="images/1.png" alt="Tigo1" width="450"/> <img src="images/2.png" alt="Tigo2" width="450"/>  
<img src="images/3.png" alt="Tigo3" width="450"/> <img src="images/4.png" alt="Tigo4" width="450"/>

---

## 📦 Features

- Connects locally to **Tigo CCA** and **ESP32 solar modules** via IP.
- Retrieves data for **each individual panel**, including:
  - Power (W)
  - Voltage (V)
  - Current (A)
  - Signal Strength (dBm)
  - Temperature (ESP32 only)
- Organizes panels by inverter and string.
- Displays **daily and 7-day energy history** (if available).
- No credentials required — works entirely over local HTTP access.
- Dynamic sensor names with prefixes:
  - `CCA1`, `CCA2`, `ESP1`, `ESP2`, etc.
- Real-time update interval: **1 minute**.
- ESP32 modules supported via [tigo_server](https://github.com/Bobsilvio/tigo_server).

---

## ⚠️ Upgrade Notes (v2.0.0)

- This version introduces **dynamic prefixes** for each device (e.g., `CCA1`, `ESP1`, …).  
- **Previous configurations must be deleted and re-added.**
- **Automations and Lovelace dashboards** using old entity names must be updated.
- Adds full support for **ESP32 modules** using  
  👉 [tigo_server](https://github.com/Bobsilvio/tigo_server).

---

## 📊 Example Entities

- `sensor.cca1_a1_power`
- `sensor.cca1_a1_voltage`
- `sensor.esp1_a10_current`
- `sensor.cca1_daily_energy`
- `sensor.cca1_last_7_days_energy`

Each panel also includes attributes such as:
- Inverter label
- String label
- MP number
- Serial number
- Channel ID

---

## 🙏 Credits

This project is inspired by reverse-engineering efforts and aims to bring **offline, privacy-friendly** monitoring of Tigo solar installations to Home Assistant.

---

## 📄 License

This project is licensed under the **MIT License**.
