"""Binärsensoren: Läuft der Check? Und gibt es irgendwo ein Problem?"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OctoPulseConfigEntry
from .api import Check
from .const import PROBLEM_STATUSES, STATUS_DOWN, STATUS_LATE, STATUS_OFFLINE, STATUS_UP
from .entity import OctoPulseCheckEntity, OctoPulseServerEntity, async_setup_check_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: OctoPulseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([OctoPulseProblemSensor(entry)])
    async_setup_check_entities(entry, async_add_entities, lambda e, check: [OctoPulseCheckSensor(e, check)])


class OctoPulseCheckSensor(OctoPulseCheckEntity, BinarySensorEntity):
    """An, wenn der Check online ist. Aus bei ausgefallen und überfällig, bei Geräten auch bei offline.

    Ein Gerät, das aus ist, steht auf offline: Das ist kein Ausfall und zählt deshalb nicht in "Probleme",
    der Sensor ist aber aus (nicht erreichbar).

    Pausiert, wartend und in Wartung: unbekannt. In diesen Fällen sagt OctoPulse bewusst nichts über den Dienst,
    und eine Automation soll daraus keinen Alarm machen.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = None  # trägt den Namen des Geräts, also des Checks

    def __init__(self, entry: OctoPulseConfigEntry, check: Check) -> None:
        super().__init__(entry, check, "connectivity")

    @property
    def is_on(self) -> bool | None:
        check = self.check
        if check is None:
            return None
        if check.status == STATUS_UP:
            return True
        if check.status in (STATUS_DOWN, STATUS_LATE, STATUS_OFFLINE):
            return False
        return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        check = self.check
        if check is None:
            return None
        attrs: dict[str, Any] = {
            "check_id": check.id,
            "status": check.status,
            "kind": check.kind,
            "interval_s": check.period_s,
            "grace_s": check.grace_s,
        }
        for key, value in (
            ("schedule", check.schedule),
            ("target", check.target),
            ("detail", check.last_detail),
            ("status_since", check.status_since),
        ):
            if value:
                attrs[key] = value.isoformat() if hasattr(value, "isoformat") else value
        return attrs


class OctoPulseProblemSensor(OctoPulseServerEntity, BinarySensorEntity):
    """An, sobald mindestens ein Check ausgefallen oder überfällig ist."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "problem"

    def __init__(self, entry: OctoPulseConfigEntry) -> None:
        super().__init__(entry, "problem")

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return False if self.coordinator.last_update_success else None
        return any(c.status in PROBLEM_STATUSES for c in self.coordinator.data.values())

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        data = self.coordinator.data or {}
        names = sorted(c.name for c in data.values() if c.status in PROBLEM_STATUSES)
        return {"checks": names} if names else None
