"""Holt regelmäßig alle Checks von OctoPulse."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Check, OctoPulseAuthError, OctoPulseClient, OctoPulseError, host_of
from .const import DOMAIN, PROBLEM_STATUSES, STATUS_UP

if TYPE_CHECKING:
    from . import OctoPulseConfigEntry

_LOGGER = logging.getLogger(__name__)


class OctoPulseCoordinator(DataUpdateCoordinator[dict[int, Check]]):
    """Eine Abfrage für alle Entitäten. Die Daten sind nach Check-ID geordnet."""

    config_entry: "OctoPulseConfigEntry"

    def __init__(
        self, hass: HomeAssistant, entry: "OctoPulseConfigEntry", client: OctoPulseClient, scan_interval: int
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {host_of(client.base_url)}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client

    async def _async_update_data(self) -> dict[int, Check]:
        try:
            checks = await self.client.async_get_checks()
        except OctoPulseAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OctoPulseError as err:
            raise UpdateFailed(f"OctoPulse nicht erreichbar oder antwortet fehlerhaft: {err}") from err
        return {check.id: check for check in checks}

    # Zusammenfassungen für die Sensoren des Servers
    @property
    def total(self) -> int:
        return len(self.data or {})

    @property
    def up(self) -> int:
        return sum(1 for c in (self.data or {}).values() if c.status == STATUS_UP)

    @property
    def problems(self) -> int:
        """Ausgefallen oder überfällig. Pausierte und in Wartung befindliche Checks zählen nicht."""
        return sum(1 for c in (self.data or {}).values() if c.status in PROBLEM_STATUSES)
