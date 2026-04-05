from datetime import timedelta
import logging

DOMAIN = "tigo"
_LOGGER = logging.getLogger(__name__)
DATA_SOURCE = ["CCA", "ESP32_WS"]
SCAN_INTERVAL = timedelta(seconds=60)
AUTH_HEADER = {
    "Authorization": "Basic VGlnbzokb2xhcg==",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0"
}

# --- Scan interval (opzioni) ---
OPT_SCAN_INTERVAL = "scan_interval"          # chiave opzione
SCAN_INTERVAL_DEFAULT_SEC = 30                # default (secondi)
SCAN_INTERVAL_MIN_SEC = 5                     # minimo consigliato
SCAN_INTERVAL_MAX_SEC = 600                   # massimo (10 min)