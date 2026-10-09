from datetime import timedelta
import logging

DOMAIN = "tigo"
_LOGGER = logging.getLogger(__name__)

# Sorgenti dati supportate
SOURCE_CCA = "CCA"
SOURCE_ESP = "ESP32_WS"
SOURCE_CLOUD = "CLOUD"
DATA_SOURCE = [SOURCE_CCA, SOURCE_ESP, SOURCE_CLOUD]
LOCAL_SOURCES = [SOURCE_CCA, SOURCE_ESP]

SCAN_INTERVAL = timedelta(seconds=60)
AUTH_HEADER = {
    "Authorization": "Basic VGlnbzokb2xhcg==",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0"
}

# --- Cloud Tigo (firmware locale >= 4.0.4: endpoint locali protetti da password) ---
# Soglia oltre la quale il firmware locale richiede login: si passa al cloud.
FIRMWARE_CLOUD_MIN = (4, 0, 4)

CONF_SYSTEM_ID = "system_id"
# Forza il cloud anche con firmware locale < 4.0.4 (test/debug)
CONF_FORCE_CLOUD = "force_cloud"

# --- Scan interval (opzioni) ---
OPT_SCAN_INTERVAL = "scan_interval"          # chiave opzione
SCAN_INTERVAL_DEFAULT_SEC = 30                # default locale (secondi)
SCAN_INTERVAL_MIN_SEC = 5                     # minimo consigliato (locale)
SCAN_INTERVAL_MAX_SEC = 600                   # massimo (10 min)
