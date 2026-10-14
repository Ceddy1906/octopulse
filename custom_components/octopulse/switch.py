"""Schalter: Überwachung eines Checks pausieren und fortsetzen."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OctoPulseConfigEntry
from .api import Check, OctoPulseError
from .const import STATUS_PAUSED
from .entity import OctoPulseCheckEntity, async_setup_check_entities

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: OctoPulseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_check_entities(entry, async_add_entities, lambda e, check: [OctoPulseMonitoringSwitch(e, check)])


class OctoPulseMonitoringSwitch(OctoPulseCheckEntity, SwitchEntity):
    """An: OctoPulse überwacht den Check. Aus: pausiert, es gibt keine Alarme.

    Praktisch für Wartungsarbeiten: Automation schaltet den Check vorher aus und danach wieder an.
    """

    _attr_translation_key = "monitoring"

    def __init__(self, entry: OctoPulseConfigEntry, check: Check) -> None:
        super().__init__(entry, check, "monitoring")
        self._client = entry.runtime_data.client

    @property
    def is_on(self) -> bool | None:
        check = self.check
        return None if check is None else check.status != STATUS_PAUSED

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._call(self._client.async_resume)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._call(self._client.async_pause)

    async def _call(self, action) -> None:
        try:
            await action(self._check_id)
        except OctoPulseError as err:
            raise HomeAssistantError(f"OctoPulse hat die Anfrage abgelehnt: {err}") from err
        await self.coordinator.async_request_refresh()
