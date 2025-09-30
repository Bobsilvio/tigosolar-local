# Tigo Local Integration for Home Assistant

A custom integration that connects Home Assistant to a **Tigo CCA** (Cloud Connect Advanced) via the local network, without using cloud services or API keys. It fetches real-time and historical solar panel data directly from the device.

> [!CAUTION]
> This will conflict with [tigosolar-online](https://github.com/Bobsilvio/tigosolar-online), you can not use both at the same time!

---

## HACS

To be added when available via HACS.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bobsilvio&repository=tigosolar-local&category=integration)

## MANUAL
1. Copy the `tigo` directory into: `/config/custom_components/`

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration**, and search for **Tigo Local**.

4. Enter the **local IP address** of your Tigo CCA (e.g. `192.168.1.100`).

> [!IMPORTANT]
> Do **not** include `http://` or use hostnames like `tigo.local`.

---
## Image
<img src="images/1.png" alt="Tigo1" width="450"/> <img src="images/2.png" alt="Tigo2" width="450"/>
<img src="images/3.png" alt="Tigo3" width="450"/> <img src="images/4.png" alt="Tigo4" width="450"/>

---
## 📦 Features

- Connects locally to your Tigo CCA via its IP address.
- Retrieves data for **each individual panel**, including:
  - Power (W)
  - Voltage (V)
  - Current (A)
  - Signal Strength (dBm)
- Organizes panels by inverter and string.
- Displays **daily and 7-day energy history** (if available).
- No credentials required, works entirely over local HTTP access.
- Update 1 min real info
  
---

## 📸 Example Entities

- `sensor.tigo_a1_power`
- `sensor.tigo_b10_voltage`
- `sensor.tigo_daily_energy`
- `sensor.tigo_last_7_days_energy`

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

This project is licensed under the [MIT License](LICENSE).
