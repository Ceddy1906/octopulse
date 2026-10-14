"""Sendet regelmäßig ein Lebenszeichen von Home Assistant an OctoPulse."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

import aiohttp

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .api import OctoPulseConnectionError, OctoPulseError, async_send_ping, host_of
from .const import (
    CONF_HEARTBEAT_MINUTES,
    DEFAULT_HEARTBEAT_MINUTES,
    HEARTBEAT_RETRY_DELAY,
    SIGNAL_HEARTBEAT_UPDATED,
    SIGNAL_SUCCESS,
)

if TYPE_CHECKING:
    from . import OctoPulseConfigEntry

_LOGGER = logging.getLogger(__name__)


class HeartbeatManager:
    """Hält fest, wann das letzte Lebenszeichen ankam, und schickt das nächste zur rechten Zeit.

    Der erste Ping geht raus, sobald Home Assistant gestartet ist. Danach folgt einer pro Intervall,
    solange die Integration geladen ist. Wiederholungen gibt es nur bei Verbindungsfehlern, und nur einmal:
    Der nächste Takt kommt ohnehin.
    """

    def __init__(
        self, hass: HomeAssistant, entry: "OctoPulseConfigEntry", session: aiohttp.ClientSession, ping_url: str
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._session = session
        self._ping_url = ping_url
        self._host = host_of(ping_url)
        self._lock = asyncio.Lock()
        self._unsub: CALLBACK_TYPE | None = None
        self._failing = False
        self.last_success: datetime | None = None
        self.last_error: str | None = None

    @property
    def signal(self) -> str:
        return SIGNAL_HEARTBEAT_UPDATED.format(self._entry.entry_id)

    @property
    def interval(self) -> timedelta:
        minutes = self._entry.options.get(CONF_HEARTBEAT_MINUTES, DEFAULT_HEARTBEAT_MINUTES)
        return timedelta(minutes=minutes)

    @callback
    def async_start(self) -> None:
        """Startet den Takt. Mehrfach aufrufen schadet nicht."""
        if self._unsub is not None:
            return
        self._unsub = async_track_time_interval(self._hass, self._async_tick, self.interval, cancel_on_shutdown=True)
        # Erster Ping nach dem Start von Home Assistant (oder sofort, wenn es schon läuft).
        self._entry.async_on_unload(async_at_started(self._hass, self._async_first_beat))

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_first_beat(self, _hass: HomeAssistant) -> None:
        await self._async_tick(None)

    async def _async_tick(self, _now: datetime | None) -> None:
        try:
            await self.async_send(SIGNAL_SUCCESS, retry=True)
        except HomeAssistantError:
            pass  # Ergebnis steht bereits in last_error und im Log

    async def async_send(self, signal: str = SIGNAL_SUCCESS, message: str | None = None, *, retry: bool = False) -> None:
        """Schickt ein Signal. Wirft HomeAssistantError, wenn es nicht angekommen ist."""
        async with self._lock:
            try:
                try:
                    await async_send_ping(self._session, self._ping_url, signal, message)
                except OctoPulseConnectionError:
                    if not retry:
                        raise
                    await asyncio.sleep(HEARTBEAT_RETRY_DELAY)
                    await async_send_ping(self._session, self._ping_url, signal, message)
            except OctoPulseError as err:
                self._record_failure(signal, str(err))
                raise HomeAssistantError(f"Heartbeat an OctoPulse ({self._host}) fehlgeschlagen: {err}") from err
            self._record_success(signal)

    def _record_success(self, signal: str) -> None:
        if signal == SIGNAL_SUCCESS:
            self.last_success = dt_util.utcnow()
        self.last_error = None
        if self._failing:
            self._failing = False
            _LOGGER.info("Heartbeat an OctoPulse (%s) funktioniert wieder", self._host)
        async_dispatcher_send(self._hass, self.signal)

    def _record_failure(self, signal: str, error: str) -> None:
        self.last_error = error
        if not self._failing:  # nur der Übergang wird gemeldet, sonst füllt ein langer Ausfall das Log
            self._failing = True
            _LOGGER.warning("Heartbeat an OctoPulse (%s) (%s) fehlgeschlagen: %s", self._host, signal, error)
        async_dispatcher_send(self._hass, self.signal)
