"""Tests für Adressen, Datenmodell und Client."""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.octopulse.api import (
    Check,
    OctoPulseApiError,
    OctoPulseAuthError,
    OctoPulseClient,
    OctoPulseConnectionError,
    async_send_ping,
    host_of,
    normalize_base_url,
)

from .conftest import API_KEY, BASE, CHECKS, PING_URL, check


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://octopulse.example.com", "https://octopulse.example.com"),
        ("https://octopulse.example.com/", "https://octopulse.example.com"),
        ("  octopulse.example.com  ", "https://octopulse.example.com"),
        ("http://192.168.1.5:8080/", "http://192.168.1.5:8080"),
        ("https://example.com/octopulse/", "https://example.com/octopulse"),
        ("https://example.com/?x=1#frag", "https://example.com"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://example.com", "https://", "https://user:pw@example.com"])
def test_normalize_base_url_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(raw)


def test_host_of_never_leaks_the_token() -> None:
    assert host_of(PING_URL) == "octopulse.example.com"
    assert "geheimer" not in host_of(PING_URL)


def test_check_from_json() -> None:
    parsed = Check.from_json(CHECKS[1])
    assert (parsed.id, parsed.name, parsed.status, parsed.kind) == (2, "Spielserver", "down", "tcp")
    assert parsed.target == "192.0.2.10:25565"
    assert parsed.last_detail == "Verbindung abgelehnt"
    assert parsed.last_ping is not None and parsed.last_ping.tzinfo is not None
    # Ein Check ohne Lebenszeichen und ohne optionale Felder.
    bare = Check.from_json({"id": 7, "name": "Neu", "kind": "heartbeat", "status": "new"})
    assert bare.last_ping is None and bare.schedule == "" and bare.period_s == 0


async def test_get_checks(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    client = OctoPulseClient(async_get_clientsession(hass), BASE, API_KEY)
    checks = await client.async_get_checks()
    assert [c.name for c in checks] == ["Home Assistant", "Spielserver", "Backup nachts"]
    # Der Key geht als Bearer-Token mit, nicht in der Adresse.
    _, url, _, headers = api.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in str(url)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"error": "API-Key fehlt oder ist ungültig"}, OctoPulseAuthError),
        (429, {"error": "zu viele Anfragen"}, OctoPulseApiError),
        (500, {"error": "interner Fehler"}, OctoPulseApiError),
        (404, None, OctoPulseApiError),
    ],
)
async def test_get_checks_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, body, error) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/checks", status=status, json=body)
    client = OctoPulseClient(async_get_clientsession(hass), BASE, API_KEY)
    with pytest.raises(error):
        await client.async_get_checks()


async def test_get_checks_connection_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    import aiohttp

    aioclient_mock.get(f"{BASE}/api/v1/checks", exc=aiohttp.ClientError("weg"))
    client = OctoPulseClient(async_get_clientsession(hass), BASE, API_KEY)
    with pytest.raises(OctoPulseConnectionError):
        await client.async_get_checks()


async def test_get_checks_not_octopulse(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Eine Webseite, die kein OctoPulse ist, wird nicht für einen leeren Server gehalten."""
    aioclient_mock.get(f"{BASE}/api/v1/checks", text="<html>hallo</html>")
    client = OctoPulseClient(async_get_clientsession(hass), BASE, API_KEY)
    with pytest.raises(OctoPulseApiError):
        await client.async_get_checks()


async def test_create_heartbeat(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        f"{BASE}/api/v1/checks", status=201, json=check(9, "Home Assistant", "new", ping_url=PING_URL)
    )
    client = OctoPulseClient(async_get_clientsession(hass), BASE, API_KEY)
    created, url = await client.async_create_heartbeat("Home Assistant", 300, 300)
    assert created.id == 9 and url == PING_URL
    sent = aioclient_mock.mock_calls[0][2]
    assert sent == {"kind": "heartbeat", "name": "Home Assistant", "period_s": 300, "grace_s": 300}


async def test_create_heartbeat_without_ping_url(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{BASE}/api/v1/checks", status=201, json=check(9, "x", "new"))
    client = OctoPulseClient(async_get_clientsession(hass), BASE, API_KEY)
    with pytest.raises(OctoPulseApiError):
        await client.async_create_heartbeat("x", 300, 300)


@pytest.mark.parametrize(
    ("signal", "suffix"), [("success", ""), ("start", "/start"), ("fail", "/fail")]
)
async def test_send_ping_signals(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, signal, suffix) -> None:
    aioclient_mock.get(PING_URL + suffix, text="OK\n")
    await async_send_ping(async_get_clientsession(hass), PING_URL + "/", signal)
    assert aioclient_mock.mock_calls[0][1].path.endswith("geheimer-token" + suffix)


async def test_send_ping_with_message_posts_it(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(PING_URL + "/fail", text="OK\n")
    await async_send_ping(async_get_clientsession(hass), PING_URL, "fail", "Backup kaputt")
    method, _, data, _ = aioclient_mock.mock_calls[0]
    assert method.lower() == "post" and data == b"Backup kaputt"


async def test_send_ping_truncates_long_message(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(PING_URL, text="OK\n")
    await async_send_ping(async_get_clientsession(hass), PING_URL, "success", "x" * 10000)
    assert len(aioclient_mock.mock_calls[0][2]) == 4096


@pytest.mark.parametrize(("status", "error"), [(404, OctoPulseApiError), (429, OctoPulseApiError), (500, OctoPulseApiError)])
async def test_send_ping_errors_do_not_leak_the_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, error
) -> None:
    aioclient_mock.get(PING_URL, status=status)
    with pytest.raises(error) as err:
        await async_send_ping(async_get_clientsession(hass), PING_URL)
    assert "geheimer-token" not in str(err.value)
