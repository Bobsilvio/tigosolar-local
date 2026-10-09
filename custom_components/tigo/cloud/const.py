"""Costanti della sorgente Cloud (motore ex tigosolar-online)."""

from ..const import DOMAIN  # noqa: F401  (stesso dominio "tigo")

# --- config entry / options keys ---
CONF_PREMIUM = "premium"
CONF_API_VERSION = "api_version"
CONF_PANEL_SCAN_INTERVAL = "panel_scan_interval"
CONF_ENERGY_POLL_INTERVAL = "energy_poll_interval"
CONF_NIGHT_SKIP = "night_skip"
CONF_ENABLE_VOLTAGE = "enable_voltage"
CONF_ENABLE_CURRENT = "enable_current"
CONF_ENABLE_RSSI = "enable_rssi"
CONF_VERBOSE_LOGGING = "verbose_logging"
CONF_PROBE_EXTRA_HARDWARE = "probe_extra_hardware"

# --- defaults ---
DEFAULT_PANEL_SCAN_INTERVAL = 60      # seconds
DEFAULT_ENERGY_POLL_INTERVAL = 300    # seconds
DEFAULT_NIGHT_SKIP = True

# --- resilience tuning (Tigo cloud has poor uptime) ---
BACKOFF_BASE = 60          # seconds; first backoff step
BACKOFF_MAX = 1800         # seconds; cap (30 min)
OUTAGE_ISSUE_AFTER = 6     # consecutive failed cycles before raising a repair
ISSUE_API_UNREACHABLE = "api_unreachable"

# Telemetry metrics (v4 temp= values)
METRIC_PIN = "pin"
METRIC_VIN = "vin"
METRIC_IIN = "iin"
METRIC_RSSI = "rssi"
METRIC_RECLAIMED = "reclaimedPower"
