"""Tests für DNS-Checks: aufgelöste IP als Sensor (OctoPulse ab 0.4.0)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.octopulse.api import Check, DnsData
from custom_components.octopulse.const import DOMAIN

from .conftest import BASE, check, make_entry


def dns_check(status: str = "up", found: list[str] | None = None, check_id: int = 9, **extra: Any) -> dict[str, Any]:
    data = check(
        check_id,
        "Heimnetz DNS",
        status,
        kind="dns",
        target="home.example.com",
        probe={"expected_ips": ["203.0.113.7"], "dns_match": "contains", "dns_on_change": "adopt", "fail_threshold": 2},
        **extra,
    )
    if found is not None:
        data["dns"] = {"found": found, "ttl_s": 300}
    return data


async def _setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, checks: list[dict[str, Any]]):
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": checks})
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, entry, platform: str, key: str, check_id: int = 9):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{check_id}_{key}")
    assert entity_id, f"{platform} {key} fehlt"
    return hass.states.get(entity_id)


def _has(hass: HomeAssistant, entry, platform: str, key: str, check_id: int) -> bool:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{check_id}_{key}") is not None


# ---------- Datenmodell ----------


def test_check_parses_dns_fields() -> None:
    c = Check.from_json(dns_check(found=["203.0.113.7", "2001:db8::1"]))
    assert c.dns == DnsData(found=("203.0.113.7", "2001:db8::1"), ttl_s=300)
    assert c.expected_ips == ("203.0.113.7",) and c.dns_match == "contains" and c.dns_on_change == "adopt"
    assert c.target == "home.example.com"


def test_dns_data_survives_garbage() -> None:
    assert Check.from_json(dns_check()).dns is None  # noch keine Auflösung
    assert DnsData.from_json("kaputt") is None
    assert DnsData.from_json({"found": []}) is None
    assert DnsData.from_json({"found": [1, None, "  "]}) is None
    d = DnsData.from_json({"found": ["203.0.113.7", 5], "ttl_s": "viel"})
    assert d == DnsData(found=("203.0.113.7",), ttl_s=None)
    # ein seltsames probe-Objekt stört nichts
    odd = dns_check()
    odd["probe"] = "kein Objekt"
    assert Check.from_json(odd).expected_ips == ()
    odd["probe"] = {"expected_ips": [1, "203.0.113.7", None]}
    assert Check.from_json(odd).expected_ips == ("203.0.113.7",)


# ---------- Entitäten ----------


async def test_resolved_ip_sensor(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [dns_check(found=["203.0.113.7"])])

    ip = _state(hass, entry, "sensor", "resolved_ip")
    assert ip.state == "203.0.113.7"
    assert ip.attributes["addresses"] == ["203.0.113.7"]
    assert ip.attributes["ttl"] == 300
    assert ip.attributes["expected"] == ["203.0.113.7"]
    assert ip.attributes["match"] == "contains" and ip.attributes["on_change"] == "adopt"
    assert ip.attributes["friendly_name"] == "Heimnetz DNS Resolved IP"  # englische Testumgebung

    # Status, Lebenszeichen und Erreichbarkeit gibt es wie bei jedem Check, der Name steht als Ziel
    assert _state(hass, entry, "sensor", "status").state == "up"
    conn = _state(hass, entry, "binary_sensor", "connectivity")
    assert conn.state == STATE_ON and conn.attributes["target"] == "home.example.com" and conn.attributes["kind"] == "dns"

    device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) if d.name == "Heimnetz DNS")
    assert device.model == "DNS"


async def test_several_addresses_and_mismatch(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    found = ["198.51.100.4", "198.51.100.5", "2001:db8::1"]
    entry = await _setup(hass, aioclient_mock, [dns_check("down", found=found, last_detail="Abweichung: erwartet 203.0.113.7")])
    ip = _state(hass, entry, "sensor", "resolved_ip")
    assert ip.state == "198.51.100.4, 198.51.100.5, 2001:db8::1" and ip.attributes["addresses"] == found
    # Abweichung ist ein Ausfall: Erreichbarkeit aus, Problem an
    assert _state(hass, entry, "binary_sensor", "connectivity").state == STATE_OFF
    problem = hass.states.get(er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_problem"))
    assert problem.state == STATE_ON


async def test_unknown_before_first_resolution(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [dns_check("new")])
    ip = _state(hass, entry, "sensor", "resolved_ip")
    assert ip.state == STATE_UNKNOWN
    assert ip.attributes["expected"] == ["203.0.113.7"] and "addresses" not in ip.attributes


async def test_no_resolved_ip_for_other_kinds(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [check(1, "Home Assistant", "up")])
    assert not _has(hass, entry, "sensor", "resolved_ip", check_id=1)


async def test_dyn_dns_change_is_picked_up(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await _setup(hass, aioclient_mock, [dns_check(found=["203.0.113.7"])])
    assert _state(hass, entry, "sensor", "resolved_ip").state == "203.0.113.7"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": [dns_check(found=["198.51.100.4"])]})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert _state(hass, entry, "sensor", "resolved_ip").state == "198.51.100.4"
