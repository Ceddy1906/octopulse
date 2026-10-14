"""Gemeinsame Hilfen der Tests. Sie brauchen pytest-homeassistant-custom-component (siehe requirements_test.txt)."""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.octopulse.const import (
    CONF_API_KEY,
    CONF_HEARTBEAT_URL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

if sys.platform == "win32":
    # Home Assistant ist für Linux gebaut. Unter Windows braucht die Ereignisschleife echte Sockets (socketpair),
    # die das Test-Plugin sperrt. Netzwerkzugriffe der Tests bleiben durch aioclient_mock trotzdem simuliert.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None

BASE = "https://octopulse.example.com"
API_KEY = "ocp_testkey"
PING_URL = f"{BASE}/ping/geheimer-token"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Lässt Home Assistant die Integration aus custom_components laden."""
    yield


@pytest.fixture(autouse=True)
def no_retry_delay():
    """Der Heartbeat wartet vor der Wiederholung. In Tests nicht."""
    with patch("custom_components.octopulse.heartbeat.HEARTBEAT_RETRY_DELAY", 0):
        yield


def check(check_id: int, name: str, status: str = "up", **extra: Any) -> dict[str, Any]:
    """Ein Check, wie ihn die API liefert."""
    data: dict[str, Any] = {
        "id": check_id,
        "owner_id": 1,
        "name": name,
        "kind": "heartbeat",
        "status": status,
        "public": False,
        "period_s": 300,
        "grace_s": 300,
        "channels": [],
        "last_ping": "2026-10-14T15:20:00.123+02:00",
        "status_since": "2026-10-14T12:00:00Z",
    }
    data.update(extra)
    return data


CHECKS = [
    check(1, "Home Assistant", "up"),
    check(2, "Spielserver", "down", kind="tcp", target="192.0.2.10:25565", last_detail="Verbindung abgelehnt"),
    check(3, "Backup nachts", "paused", schedule="0 3 * * *"),
]


@pytest.fixture
def api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Ein erreichbarer OctoPulse-Server mit drei Checks."""
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": CHECKS})
    aioclient_mock.post(f"{BASE}/api/v1/checks/3/resume", json=check(3, "Backup nachts", "up"))
    aioclient_mock.post(f"{BASE}/api/v1/checks/1/pause", json=check(1, "Home Assistant", "paused"))
    aioclient_mock.get(PING_URL, text="OK\n")
    return aioclient_mock


def make_entry(*, heartbeat: bool = False, options: dict[str, Any] | None = None) -> MockConfigEntry:
    data = {CONF_URL: BASE, CONF_API_KEY: API_KEY, CONF_VERIFY_SSL: True}
    if heartbeat:
        data[CONF_HEARTBEAT_URL] = PING_URL
    return MockConfigEntry(
        domain=DOMAIN, title="OctoPulse (octopulse.example.com)", data=data, options=options or {}, unique_id=BASE
    )
