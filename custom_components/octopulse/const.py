"""Konstanten der OctoPulse-Integration."""
from __future__ import annotations

DOMAIN = "octopulse"

# Konfiguration (entry.data)
CONF_URL = "url"
CONF_API_KEY = "api_key"
CONF_VERIFY_SSL = "verify_ssl"
CONF_HEARTBEAT_URL = "heartbeat_url"  # enthält den geheimen Token, nie ins Log schreiben
CONF_HEARTBEAT_CHECK_ID = "heartbeat_check_id"

# Optionen (entry.options)
CONF_SCAN_INTERVAL = "scan_interval"
CONF_HEARTBEAT_MINUTES = "heartbeat_minutes"

# Felder der Dialoge, die nicht gespeichert werden
CONF_HEARTBEAT_NAME = "heartbeat_name"
CONF_HEARTBEAT_GRACE_MINUTES = "heartbeat_grace_minutes"

DEFAULT_SCAN_INTERVAL = 60  # Sekunden
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 3600
DEFAULT_HEARTBEAT_MINUTES = 5
MIN_HEARTBEAT_MINUTES = 1
MAX_HEARTBEAT_MINUTES = 1440
DEFAULT_HEARTBEAT_GRACE_MINUTES = 5
DEFAULT_HEARTBEAT_NAME = "Home Assistant"

REQUEST_TIMEOUT = 15  # Sekunden
HEARTBEAT_TIMEOUT = 10
HEARTBEAT_RETRY_DELAY = 5

# Anzeigestatus eines Checks, wie ihn die OctoPulse-API liefert. Die Reihenfolge ist die der Sensor-Optionen.
STATUS_NEW = "new"
STATUS_UP = "up"
STATUS_LATE = "late"
STATUS_DOWN = "down"
STATUS_PAUSED = "paused"
STATUS_MAINTENANCE = "maintenance"
STATUSES = [STATUS_UP, STATUS_LATE, STATUS_DOWN, STATUS_NEW, STATUS_PAUSED, STATUS_MAINTENANCE]
PROBLEM_STATUSES = (STATUS_LATE, STATUS_DOWN)

SIGNAL_SUCCESS = "success"
SIGNAL_START = "start"
SIGNAL_FAIL = "fail"
SIGNALS = [SIGNAL_SUCCESS, SIGNAL_START, SIGNAL_FAIL]

SERVICE_SEND_HEARTBEAT = "send_heartbeat"
ATTR_SIGNAL = "signal"
ATTR_MESSAGE = "message"
ATTR_CONFIG_ENTRY = "config_entry"

SIGNAL_HEARTBEAT_UPDATED = f"{DOMAIN}_heartbeat_updated_{{}}"

HEARTBEAT_MAX_MESSAGE = 4096  # OctoPulse speichert höchstens 4 KB aus dem Body

