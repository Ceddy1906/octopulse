"""Tests für Einrichtung, Neuanmeldung, Änderung und Optionen."""
from __future__ import annotations

import aiohttp
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.octopulse.const import (
    CONF_API_KEY,
    CONF_HEARTBEAT_CHECK_ID,
    CONF_HEARTBEAT_GRACE_MINUTES,
    CONF_HEARTBEAT_MINUTES,
    CONF_HEARTBEAT_NAME,
    CONF_HEARTBEAT_URL,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from .conftest import API_KEY, BASE, PING_URL, check, make_entry

USER_INPUT = {CONF_URL: BASE, CONF_API_KEY: API_KEY, CONF_VERIFY_SSL: True}


async def _start(hass: HomeAssistant, user_input: dict | None = None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input or USER_INPUT)


async def test_full_flow_without_heartbeat(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "heartbeat"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OctoPulse (octopulse.example.com)"
    assert result["data"] == USER_INPUT
    assert CONF_HEARTBEAT_URL not in result["data"]


async def test_url_is_normalized(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    result = await _start(hass, {**USER_INPUT, CONF_URL: "  octopulse.example.com/ "})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["data"][CONF_URL] == BASE


async def test_flow_creates_heartbeat_check(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    api.post(f"{BASE}/api/v1/checks", status=201, json=check(9, "Home Assistant", "new", ping_url=PING_URL))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "heartbeat_create"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "heartbeat_create"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HEARTBEAT_NAME: "Haus", CONF_HEARTBEAT_MINUTES: 10, CONF_HEARTBEAT_GRACE_MINUTES: 3},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HEARTBEAT_URL] == PING_URL
    assert result["data"][CONF_HEARTBEAT_CHECK_ID] == 9
    assert result["options"][CONF_HEARTBEAT_MINUTES] == 10
    # Minuten werden zu Sekunden: Intervall 600, Karenz 180.
    sent = [c for c in api.mock_calls if c[0].lower() == "post"][0][2]
    assert sent == {"kind": "heartbeat", "name": "Haus", "period_s": 600, "grace_s": 180}


async def test_flow_heartbeat_create_rejected(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    api.post(f"{BASE}/api/v1/checks", status=400, json={"error": "Name fehlt"})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "heartbeat_create"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HEARTBEAT_NAME: "x", CONF_HEARTBEAT_MINUTES: 5, CONF_HEARTBEAT_GRACE_MINUTES: 5},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "create_failed"}
    assert result["description_placeholders"] == {"error": "Name fehlt"}


async def test_flow_with_existing_ping_url(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "heartbeat_url"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HEARTBEAT_URL: PING_URL + "/fail", CONF_HEARTBEAT_MINUTES: 5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HEARTBEAT_URL] == PING_URL  # /fail wird abgeschnitten
    assert CONF_HEARTBEAT_CHECK_ID not in result["data"]
    # Ein Lebenszeichen zur Prüfung der Adresse, ein weiteres als erster Takt nach dem Anlegen des Eintrags.
    assert sum(1 for c in api.mock_calls if c[1].path == "/ping/geheimer-token") == 2


async def test_flow_ping_url_validation(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    api.get(f"{BASE}/ping/falsch", status=404)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "heartbeat_url"})
    for url, error in (
        (f"{BASE}/checks/1", "invalid_ping_url"),
        (f"{BASE}/ping/", "invalid_ping_url"),
        (f"{BASE}/ping/falsch", "ping_rejected"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HEARTBEAT_URL: url, CONF_HEARTBEAT_MINUTES: 5}
        )
        assert result["type"] is FlowResultType.FORM and result["errors"] == {CONF_HEARTBEAT_URL: error}, url


async def test_flow_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    cases = [
        ({CONF_URL: "ftp://x"}, None, {CONF_URL: "invalid_url"}),
        ({}, {"status": 401, "json": {"error": "nein"}}, {"base": "invalid_auth"}),
        ({}, {"exc": aiohttp.ClientError("weg")}, {"base": "cannot_connect"}),
        ({}, {"text": "<html></html>"}, {"base": "invalid_response"}),
    ]
    for override, response, errors in cases:
        aioclient_mock.clear_requests()
        if response:
            aioclient_mock.get(f"{BASE}/api/v1/checks", **response)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, **override})
        assert result["type"] is FlowResultType.FORM and result["errors"] == errors, override

    # Nach den Fehlern lässt sich der Dialog mit richtigen Angaben beenden.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": []})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.MENU


async def test_already_configured(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{BASE}/api/v1/checks", status=401, json={"error": "nein"})
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "ocp_alt"})
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/v1/checks", json={"checks": []})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "ocp_neu"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "ocp_neu"


async def test_reconfigure(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE + "/", CONF_API_KEY: "ocp_zwei", CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_KEY] == "ocp_zwei" and entry.data[CONF_VERIFY_SSL] is False
    assert entry.data[CONF_URL] == BASE


async def test_options_settings(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "heartbeat_remove" not in result["menu_options"]  # ohne Heartbeat gibt es nichts zu entfernen
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120, CONF_HEARTBEAT_MINUTES: 7}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 120 and entry.options[CONF_HEARTBEAT_MINUTES] == 7


async def test_options_remove_heartbeat(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    entry = make_entry(heartbeat=True)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "heartbeat_remove" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "heartbeat_remove"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_HEARTBEAT_URL not in entry.data
    assert entry.data[CONF_API_KEY] == API_KEY  # der Rest bleibt


async def test_options_replace_heartbeat_url(hass: HomeAssistant, api: AiohttpClientMocker) -> None:
    other = f"{BASE}/ping/anderer-token"
    api.get(other, text="OK\n")
    entry = make_entry(heartbeat=True)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "heartbeat_url"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HEARTBEAT_URL: other, CONF_HEARTBEAT_MINUTES: 3}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_HEARTBEAT_URL] == other
    assert entry.options[CONF_HEARTBEAT_MINUTES] == 3
