"""Diagnosedaten für Fehlerberichte, ohne Geheimnisse."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import OctoPulseConfigEntry
from .const import CONF_API_KEY, CONF_HEARTBEAT_URL, CONF_URL

TO_REDACT = {CONF_API_KEY, CONF_HEARTBEAT_URL, CONF_URL}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: OctoPulseConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data
    heartbeat = data.heartbeat
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "update_success": data.coordinator.last_update_success,
        "checks": {
            "total": data.coordinator.total,
            "up": data.coordinator.up,
            "problems": data.coordinator.problems,
            "by_status": _count_by_status(data.coordinator.data or {}),
        },
        "heartbeat": None
        if heartbeat is None
        else {
            "last_success": heartbeat.last_success.isoformat() if heartbeat.last_success else None,
            "last_error": heartbeat.last_error,
            "interval_s": heartbeat.interval.total_seconds(),
        },
    }


def _count_by_status(checks: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for check in checks.values():
        out[check.status] = out.get(check.status, 0) + 1
    return out
