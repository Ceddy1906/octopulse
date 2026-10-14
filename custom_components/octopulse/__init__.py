"""OctoPulse: Checks aus dem Monitor in Home Assistant und Lebenszeichen von Home Assistant an den Monitor."""
from __future__ import annotations

from dataclasses import dataclass
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import OctoPulseClient
from .const import (
    ATTR_CONFIG_ENTRY,
    ATTR_MESSAGE,
    ATTR_SIGNAL,
    CONF_API_KEY,
    CONF_HEARTBEAT_URL,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HEARTBEAT_MAX_MESSAGE,
    SERVICE_SEND_HEARTBEAT,
    SIGNALS,
    SIGNAL_SUCCESS,
)
from .coordinator import OctoPulseCoordinator
from .entity import server_device
from .heartbeat import HeartbeatManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_HEARTBEAT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SIGNAL, default=SIGNAL_SUCCESS): vol.In(SIGNALS),
        vol.Optional(ATTR_MESSAGE): vol.All(cv.string, vol.Length(max=HEARTBEAT_MAX_MESSAGE)),
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
    }
)


@dataclass
class OctoPulseData:
    """Alles, was zu einem eingerichteten OctoPulse-Server gehört."""

    client: OctoPulseClient
    coordinator: OctoPulseCoordinator
    heartbeat: HeartbeatManager | None
    server_device_id: str = ""  # Geräte-ID des Servers, Elternteil der Check-Geräte


type OctoPulseConfigEntry = ConfigEntry[OctoPulseData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Registriert den Dienst, einmal für die ganze Integration (nicht je Server)."""

    async def _send_heartbeat(call: ServiceCall) -> None:
        wanted = call.data.get(ATTR_CONFIG_ENTRY)
        entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED and (wanted is None or e.entry_id == wanted)
        ]
        targets = [e for e in entries if e.runtime_data.heartbeat is not None]
        if not targets:
            raise ServiceValidationError(
                "Kein OctoPulse-Server mit Heartbeat gefunden. Heartbeat unter Einstellungen > Geräte & Dienste > "
                "OctoPulse > Konfigurieren einrichten.",
                translation_domain=DOMAIN,
                translation_key="no_heartbeat",
            )
        for entry in targets:
            await entry.runtime_data.heartbeat.async_send(call.data[ATTR_SIGNAL], call.data.get(ATTR_MESSAGE))

    hass.services.async_register(DOMAIN, SERVICE_SEND_HEARTBEAT, _send_heartbeat, schema=SEND_HEARTBEAT_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OctoPulseConfigEntry) -> bool:
    session = async_get_clientsession(hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True))
    client = OctoPulseClient(session, entry.data[CONF_URL], entry.data[CONF_API_KEY])
    coordinator = OctoPulseCoordinator(hass, entry, client, entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    heartbeat = None
    if ping_url := entry.data.get(CONF_HEARTBEAT_URL):
        heartbeat = HeartbeatManager(hass, entry, session, ping_url)

    # Ist der Server nicht erreichbar, versucht es Home Assistant später erneut.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        if heartbeat is None:
            raise
        # Der Heartbeat braucht den API-Key nicht. Ein abgelaufener Key darf nicht dazu führen, dass OctoPulse
        # Home Assistant für ausgefallen hält. Also läuft die Integration weiter (ohne Check-Daten) und fragt nach dem Key.
        _LOGGER.warning("Der API-Key für OctoPulse wurde abgelehnt. Der Heartbeat läuft weiter, die Checks sind nicht verfügbar")
        entry.async_start_reauth(hass)

    entry.runtime_data = OctoPulseData(client=client, coordinator=coordinator, heartbeat=heartbeat)

    # Das Server-Gerät muss vor den Check-Geräten existieren, damit diese darauf verweisen können.
    server = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **server_device(entry))
    entry.runtime_data.server_device_id = server.id

    _sync_devices(hass, entry)
    entry.async_on_unload(coordinator.async_add_listener(lambda: _sync_devices(hass, entry)))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_change))

    if heartbeat is not None:
        heartbeat.async_start()
        entry.async_on_unload(heartbeat.async_stop)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OctoPulseConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_on_change(hass: HomeAssistant, entry: OctoPulseConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


@callback
def _sync_devices(hass: HomeAssistant, entry: OctoPulseConfigEntry) -> None:
    """Gleicht die Geräte mit den Checks ab: umbenannte Checks umbenennen, gelöschte Geräte entfernen.

    Läuft nur nach einem erfolgreichen Abruf. Der Server selbst ist ein Gerät, jeder Check ein weiteres darunter.
    """
    coordinator = entry.runtime_data.coordinator
    if not coordinator.last_update_success or coordinator.data is None:
        return
    registry = dr.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, ident in device.identifiers:
            if domain != DOMAIN or not ident.startswith(prefix):
                continue
            try:
                check_id = int(ident[len(prefix):])
            except ValueError:
                continue
            check = coordinator.data.get(check_id)
            if check is None:
                registry.async_remove_device(device.id)
            elif device.name != check.name:
                registry.async_update_device(device.id, name=check.name)
