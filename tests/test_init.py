"""Tests für Start, Entitäten, Schalter, Heartbeat und Dienst."""
from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.octopulse.const import CONF_HEARTBEAT_MINUTES, DOMAIN

from .conftest import BASE, CHECKS, PING_URL, check, make_entry


async def _setup(hass: HomeAssistant, **kwargs):
    entry = make_entry(**kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, entry, platform: str, check_id: int | None, key: str) -> str:
    suffix = key if check_id is None else f"{check_id}_{key}"
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{suffix}")
    assert entity_id, f"{platform} {suffix} fehlt"
    return entity_id


async def test_setup_creates_devices_and_entities(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert {d.name for d in devices} == {"OctoPulse (octopulse.example.com)", "Home Assistant", "Spielserver", "Backup nachts"}
    server = next(d for d in devices if d.entry_type is dr.DeviceEntryType.SERVICE)
    assert all(d.via_device_id == server.id for d in devices if d is not server)

    # online -> an, ausgefallen -> aus, pausiert -> unbekannt (keine Aussage über den Dienst)
    states = {
        c["name"]: hass.states.get(_entity_id(hass, entry, "binary_sensor", c["id"], "connectivity")).state for c in CHECKS
    }
    assert states == {"Home Assistant": STATE_ON, "Spielserver": STATE_OFF, "Backup nachts": STATE_UNKNOWN}

    down = hass.states.get(_entity_id(hass, entry, "binary_sensor", 2, "connectivity"))
    assert down.attributes["status"] == "down"
    assert down.attributes["target"] == "192.0.2.10:25565"
    assert down.attributes["detail"] == "Verbindung abgelehnt"
    assert hass.states.get(_entity_id(hass, entry, "binary_sensor", 3, "connectivity")).attributes["schedule"] == "0 3 * * *"

    assert hass.states.get(_entity_id(hass, entry, "sensor", 2, "status")).state == "down"
    assert hass.states.get(_entity_id(hass, entry, "sensor", 3, "status")).state == "paused"
    assert hass.states.get(_entity_id(hass, entry, "sensor", 1, "last_ping")).state.startswith("2026-10-14T13:20:00")

    # Sammelwerte: Pausierte zählen nicht als Problem, Ausgefallene schon.
    assert hass.states.get(_entity_id(hass, entry, "sensor", None, "checks_total")).state == "3"
    assert hass.states.get(_entity_id(hass, entry, "sensor", None, "checks_up")).state == "1"
    assert hass.states.get(_entity_id(hass, entry, "sensor", None, "checks_problem")).state == "1"
    problem = hass.states.get(_entity_id(hass, entry, "binary_sensor", None, "problem"))
    assert problem.state == STATE_ON and problem.attributes["checks"] == ["Spielserver"]

    # Ohne Heartbeat gibt es keinen Heartbeat-Sensor und keinen Ping.
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_heartbeat_last_sent") is None
    assert not [c for c in api.mock_calls if c[1].path.startswith("/ping/")]


async def test_switch_pauses_and_resumes(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = await _setup(hass)
    paused = _entity_id(hass, entry, "switch", 3, "monitoring")
    running = _entity_id(hass, entry, "switch", 1, "monitoring")
    assert hass.states.get(paused).state == STATE_OFF
    assert hass.states.get(running).state == STATE_ON

    await hass.services.async_call("switch", "turn_on", {"entity_id": paused}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": running}, blocking=True)
    posts = {c[1].path for c in api.mock_calls if c[0].lower() == "post"}
    assert posts == {"/api/v1/checks/3/resume", "/api/v1/checks/1/pause"}


async def test_switch_error_is_reported(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = await _setup(hass)
    api.clear_requests()  # der erste passende Eintrag gewinnt, also den erfolgreichen vorher entfernen
    api.get(f"{BASE}/api/v1/checks", json={"checks": CHECKS})
    api.post(f"{BASE}/api/v1/checks/1/pause", status=404, json={"error": "Check nicht gefunden"})
    with pytest.raises(HomeAssistantError, match="Check nicht gefunden"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": _entity_id(hass, entry, "switch", 1, "monitoring")}, blocking=True
        )


async def test_status_change_is_picked_up(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = await _setup(hass)
    sensor = _entity_id(hass, entry, "binary_sensor", 2, "connectivity")
    assert hass.states.get(sensor).state == STATE_OFF

    api.clear_requests()
    api.get(f"{BASE}/api/v1/checks", json={"checks": [check(1, "Home Assistant"), check(2, "Spielserver", "up")]})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == STATE_ON


async def test_new_check_gets_entities_and_deleted_check_loses_device(
    hass: HomeAssistant, api: AiohttpClientMocker
) -> None:
    entry = await _setup(hass)
    api.clear_requests()
    api.get(
        f"{BASE}/api/v1/checks",
        json={"checks": [check(1, "Home Assistant (neu benannt)"), check(2, "Spielserver"), check(4, "NAS")]},
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    names = {d.name for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)}
    assert "NAS" in names  # neuer Check: neues Gerät
    assert "Home Assistant (neu benannt)" in names and "Home Assistant" not in names  # umbenannt
    assert "Backup nachts" not in names  # gelöscht
    assert hass.states.get(_entity_id(hass, entry, "binary_sensor", 4, "connectivity")).state == STATE_ON


async def test_server_unreachable_makes_entities_unavailable(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = await _setup(hass)
    sensor = _entity_id(hass, entry, "binary_sensor", 1, "connectivity")
    api.clear_requests()
    api.get(f"{BASE}/api/v1/checks", exc=aiohttp.ClientError("weg"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "unavailable"


async def test_setup_retries_when_server_is_down(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/checks", exc=aiohttp.ClientError("weg"))
    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_asks_for_new_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/checks", status=401, json={"error": "nein"})
    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_unload(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


# ---------- Heartbeat ----------


def _pings(api: AiohttpClientMocker) -> list[str]:
    return [c[1].path for c in api.mock_calls if c[1].path.startswith("/ping/")]


async def test_heartbeat_first_ping_and_interval(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = await _setup(hass, heartbeat=True)
    assert _pings(api) == ["/ping/geheimer-token"]  # gleich nach dem Start
    sensor = _entity_id(hass, entry, "sensor", None, "heartbeat_last_sent")
    assert hass.states.get(sensor).state != STATE_UNKNOWN

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()
    assert len(_pings(api)) == 2


async def test_heartbeat_interval_is_configurable(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    await _setup(hass, heartbeat=True, options={CONF_HEARTBEAT_MINUTES: 2})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2, seconds=1))
    await hass.async_block_till_done()
    assert len(_pings(api)) == 2


async def test_heartbeat_failure_is_recorded_and_recovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": CHECKS})
    aioclient_mock.get(PING_URL, status=500)
    entry = await _setup(hass, heartbeat=True)
    sensor = _entity_id(hass, entry, "sensor", None, "heartbeat_last_sent")
    state = hass.states.get(sensor)
    assert state.state == STATE_UNKNOWN and "HTTP 500" in state.attributes["last_error"]
    # Die Warnung nennt den Rechner, aber nie den geheimen Token.
    assert "geheimer-token" not in caplog.text

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": CHECKS})
    aioclient_mock.get(PING_URL, text="OK\n")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.state != STATE_UNKNOWN and "last_error" not in state.attributes


async def test_heartbeat_retries_once_on_connection_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": CHECKS})
    aioclient_mock.get(PING_URL, exc=aiohttp.ClientError("weg"))
    await _setup(hass, heartbeat=True)
    assert len(_pings(aioclient_mock)) == 2  # erster Versuch plus eine Wiederholung


async def test_heartbeat_works_without_valid_api_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Der Heartbeat hängt nicht am API-Key. Ein abgelaufener Key darf OctoPulse nicht fälschlich Alarm schlagen lassen."""
    aioclient_mock.get(f"{BASE}/api/v1/checks", status=401, json={"error": "nein"})
    aioclient_mock.get(PING_URL, text="OK\n")
    entry = make_entry(heartbeat=True)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert _pings(aioclient_mock) == ["/ping/geheimer-token"]
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


# ---------- Dienst ----------


async def test_service_sends_signals(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    api.get(PING_URL + "/start", text="OK\n")
    api.post(PING_URL + "/fail", text="OK\n")
    await _setup(hass, heartbeat=True)
    api.clear_requests()
    api.get(PING_URL, text="OK\n")
    api.get(PING_URL + "/start", text="OK\n")
    api.post(PING_URL + "/fail", text="OK\n")

    await hass.services.async_call(DOMAIN, "send_heartbeat", {}, blocking=True)
    await hass.services.async_call(DOMAIN, "send_heartbeat", {"signal": "start"}, blocking=True)
    await hass.services.async_call(
        DOMAIN, "send_heartbeat", {"signal": "fail", "message": "Backup kaputt"}, blocking=True
    )
    assert [(c[0].lower(), c[1].path) for c in api.mock_calls] == [
        ("get", "/ping/geheimer-token"),
        ("get", "/ping/geheimer-token/start"),
        ("post", "/ping/geheimer-token/fail"),
    ]
    assert api.mock_calls[2][2] == b"Backup kaputt"


async def test_service_without_heartbeat(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "send_heartbeat", {}, blocking=True)


async def test_service_reports_failure(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": CHECKS})
    aioclient_mock.get(PING_URL, text="OK\n")
    await _setup(hass, heartbeat=True)
    aioclient_mock.clear_requests()
    aioclient_mock.get(PING_URL, status=404)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "send_heartbeat", {}, blocking=True)
    assert "geheimer-token" not in str(err.value)


async def test_diagnostics_hide_secrets(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    from custom_components.octopulse.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass, heartbeat=True)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "geheimer-token" not in text and "ocp_testkey" not in text
    assert diag["checks"] == {"total": 3, "up": 1, "problems": 1, "by_status": {"up": 1, "down": 1, "paused": 1}}
    assert diag["heartbeat"]["last_error"] is None


async def test_heartbeat_logs_only_the_first_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": CHECKS})
    aioclient_mock.get(PING_URL, status=500)
    await _setup(hass, heartbeat=True)
    for minutes in (5, 10, 15):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes, seconds=1))
        await hass.async_block_till_done()
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "Heartbeat" in r.getMessage()]
    assert len(warnings) == 1  # ein langer Ausfall füllt das Log nicht


async def test_overdue_counts_as_problem(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Überfällig ist noch kein Ausfall, aber schon ein Problem: Binärsensor aus, Sammelwerte zählen es mit."""
    aioclient_mock.get(
        f"{BASE}/api/v1/checks",
        json={"checks": [check(1, "Spät", "late"), check(2, "Gut", "up"), check(3, "Pause", "paused")]},
    )
    entry = await _setup(hass)
    assert hass.states.get(_entity_id(hass, entry, "binary_sensor", 1, "connectivity")).state == STATE_OFF
    assert hass.states.get(_entity_id(hass, entry, "sensor", None, "checks_problem")).state == "1"
    problem = hass.states.get(_entity_id(hass, entry, "binary_sensor", None, "problem"))
    assert problem.state == STATE_ON and problem.attributes["checks"] == ["Spät"]
