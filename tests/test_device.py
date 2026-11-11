"""Tests für Geräte-Checks (Windows-Client): Daten der letzten Meldung als Sensoren."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.octopulse.api import Check, DeviceData
from custom_components.octopulse.const import DOMAIN

from .conftest import BASE, check, make_entry

DEVICE = {
    "reported_at": "2026-11-12T10:00:00Z",
    "public_ip": "203.0.113.7",
    "address": "192.168.1.23",
    "network_name": "Heimnetz",
    "v": 1,
    "agent": {"version": "v0.3.2", "os": "windows", "arch": "amd64"},
    "host": {"name": "PC-MAX", "domain": "WORKGROUP"},
    "os": {"name": "Windows 11 Pro", "version": "24H2", "build": "26100.2033"},
    "users": [{"name": "PC-MAX\\max", "state": "active"}, {"name": "PC-MAX\\gast", "state": "disconnected"}],
    "network": [
        {"name": "WLAN", "kind": "wlan", "profile": "Heimnetz", "ssid": "Heimnetz", "addresses": ["192.168.1.23", "2001:db8::5"]},
        {"name": "vEthernet (WSL)", "kind": "ethernet", "addresses": ["172.30.0.1"]},
    ],
    "boot": "2026-11-12T06:00:00Z",
    "uptime_s": 14400,
    "time_zone": "W. Europe Standard Time (UTC+01:00)",
    "memory": {"total_mb": 16000, "free_mb": 4000},
    "disks": [{"drive": "C:", "total_gb": 500, "free_gb": 125}],
}


def device_check(status: str = "up", device: Any = DEVICE, check_id: int = 7) -> dict[str, Any]:
    data = check(check_id, "PC Max", status, kind="device")
    if device is not None:
        data["device"] = deepcopy(device)
    return data


async def _setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, checks: list[dict[str, Any]]):
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": checks})
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, entry, platform: str, key: str, check_id: int = 7):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{check_id}_{key}")
    assert entity_id, f"{platform} {key} fehlt"
    return hass.states.get(entity_id)


def _has(hass: HomeAssistant, entry, platform: str, key: str, check_id: int = 7) -> bool:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{check_id}_{key}") is not None


# ---------- Datenmodell ----------


def test_device_data_parses_report() -> None:
    d = Check.from_json(device_check()).device
    assert d is not None
    assert (d.host_name, d.domain, d.address, d.network, d.public_ip) == ("PC-MAX", "WORKGROUP", "192.168.1.23", "Heimnetz", "203.0.113.7")
    assert [(u.name, u.active) for u in d.users] == [("PC-MAX\\max", True), ("PC-MAX\\gast", False)]
    assert d.networks[0].addresses == ("192.168.1.23", "2001:db8::5") and d.networks[0].kind == "wlan"
    assert d.memory_used_percent == 75.0
    assert d.disks[0].free_percent == 25.0
    assert d.agent_version == "v0.3.2" and d.boot == dt_util.parse_datetime("2026-11-12T06:00:00Z")


def test_device_data_survives_garbage() -> None:
    assert Check.from_json(device_check(device=None)).device is None
    assert Check.from_json(device_check(device="kaputt")).device is None
    assert DeviceData.from_json([1, 2]) is None
    weird = {
        "host": "kein Objekt", "os": 5, "users": "x", "network": [1, {"addresses": "nein"}], "disks": [{"drive": "C:", "total_gb": "viel"}, 3],
        "memory": {"total_mb": True, "free_mb": "x"}, "boot": 42, "reported_at": "gestern",
    }
    d = DeviceData.from_json(weird)
    assert d is not None
    assert d.host_name == "" and d.users == () and d.disks == () and d.boot is None
    assert d.memory_used_percent is None
    # Null Gesamtgröße führt nicht zur Division durch null
    z = DeviceData.from_json({"disks": [{"drive": "D:", "total_gb": 0, "free_gb": 0}], "memory": {"total_mb": 0, "free_mb": 0}})
    assert z is not None and z.disks[0].free_percent is None and z.memory_used_percent is None


# ---------- Entitäten ----------


async def test_device_sensors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [device_check()])

    assert _state(hass, entry, "sensor", "host_name").state == "PC-MAX"
    assert _state(hass, entry, "sensor", "host_name").attributes["domain"] == "WORKGROUP"
    ip = _state(hass, entry, "sensor", "ip_address")
    assert ip.state == "192.168.1.23" and "172.30.0.1" in ip.attributes["addresses"]
    assert _state(hass, entry, "sensor", "public_ip").state == "203.0.113.7"
    net = _state(hass, entry, "sensor", "network")
    assert net.state == "Heimnetz" and net.attributes["connections"][0]["kind"] == "wlan"
    users = _state(hass, entry, "sensor", "users")
    assert users.state == "PC-MAX\\max, PC-MAX\\gast" and users.attributes["disconnected"] == ["PC-MAX\\gast"]
    os_ = _state(hass, entry, "sensor", "os")
    assert os_.state == "Windows 11 Pro 24H2" and os_.attributes["build"] == "26100.2033"
    assert _state(hass, entry, "sensor", "boot").state.startswith("2026-11-12T06:00:00")
    mem = _state(hass, entry, "sensor", "memory_used")
    assert mem.state == "75.0" and mem.attributes["total_mb"] == 16000 and mem.attributes["unit_of_measurement"] == "%"
    agent = _state(hass, entry, "sensor", "agent_version")
    assert agent.state == "v0.3.2"
    disk = _state(hass, entry, "sensor", "disk_c")
    assert disk.state == "125" and disk.attributes["total_gb"] == 500 and disk.attributes["free_percent"] == 25.0
    assert disk.attributes["friendly_name"] == "PC Max Drive C: free"  # englische Testumgebung, Platzhalter ersetzt

    # Status, Lebenszeichen und Erreichbarkeit gibt es wie bei jedem Check
    assert _state(hass, entry, "sensor", "status").state == "up"
    assert _state(hass, entry, "binary_sensor", "connectivity").state == STATE_ON

    device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) if d.name == "PC Max")
    assert device.model == "Windows-PC"


async def test_offline_device_is_not_a_problem(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [device_check("offline"), check(1, "Home Assistant", "up")])

    assert _state(hass, entry, "sensor", "status").state == "offline"
    # nicht erreichbar, aber kein Alarm: "Probleme" und "Checks mit Problem" bleiben unberührt
    assert _state(hass, entry, "binary_sensor", "connectivity").state == STATE_OFF
    problem = hass.states.get(er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_problem"))
    assert problem.state == STATE_OFF
    count = hass.states.get(er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_checks_problem"))
    assert count.state == "0"
    # der letzte Stand bleibt sichtbar
    assert _state(hass, entry, "sensor", "host_name").state == "PC-MAX"


async def test_device_without_report_has_unknown_values(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [device_check("new", device=None)])
    for key in ("host_name", "ip_address", "public_ip", "network", "users", "os", "boot", "memory_used", "agent_version"):
        assert _state(hass, entry, "sensor", key).state == STATE_UNKNOWN, key
    assert not _has(hass, entry, "sensor", "disk_c"), "ohne Meldung kennt niemand die Laufwerke"
    assert _state(hass, entry, "binary_sensor", "connectivity").state == STATE_UNKNOWN


async def test_no_device_sensors_for_other_kinds(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [check(1, "Home Assistant", "up")])
    for key in ("host_name", "ip_address", "users", "disk_c"):
        assert not _has(hass, entry, "sensor", key, check_id=1), key


async def test_nobody_signed_in(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    device = deepcopy(DEVICE)
    device["users"] = []
    entry = await _setup(hass, aioclient_mock, [device_check(device=device)])
    users = _state(hass, entry, "sensor", "users")
    assert users.state == "Niemand" and users.attributes["users"] == []


async def test_new_drive_and_updated_values_are_picked_up(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [device_check()])
    assert not _has(hass, entry, "sensor", "disk_d")

    changed = deepcopy(DEVICE)
    changed["disks"].append({"drive": "D:", "total_gb": 1000, "free_gb": 400})
    changed["disks"][0]["free_gb"] = 100
    changed["users"] = [{"name": "PC-MAX\\anna", "state": "active"}]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": [device_check(device=changed)]})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert _state(hass, entry, "sensor", "disk_d").state == "400"
    assert _state(hass, entry, "sensor", "disk_c").state == "100"
    assert _state(hass, entry, "sensor", "users").state == "PC-MAX\\anna"
    # kein zweiter Sensor für dasselbe Laufwerk
    registry = er.async_get(hass)
    disks = [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if "_disk_" in e.unique_id]
    assert len(disks) == 2


async def test_long_user_list_is_cut_to_state_limit(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    device = deepcopy(DEVICE)
    device["users"] = [{"name": f"DOMAIN\\benutzer-mit-langem-namen-{i:03d}", "state": "active"} for i in range(30)]
    entry = await _setup(hass, aioclient_mock, [device_check(device=device)])
    users = _state(hass, entry, "sensor", "users")
    assert len(users.state) <= 255
    assert len(users.attributes["users"]) == 30
