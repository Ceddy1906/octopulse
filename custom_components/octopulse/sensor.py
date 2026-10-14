"""Sensoren: Status und letztes Lebenszeichen je Check, Zähler und Heartbeat für den Server."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OctoPulseConfigEntry
from .api import Check
from .coordinator import OctoPulseCoordinator
from .const import STATUSES
from .entity import OctoPulseCheckEntity, OctoPulseServerEntity, async_setup_check_entities

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class CheckSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Check], str | datetime | None]


CHECK_SENSORS: tuple[CheckSensorDescription, ...] = (
    CheckSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
        value_fn=lambda c: c.status if c.status in STATUSES else None,
    ),
    CheckSensorDescription(
        key="last_ping",
        translation_key="last_ping",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.last_ping,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ServerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[OctoPulseCoordinator], int]


SERVER_SENSORS: tuple[ServerSensorDescription, ...] = (
    ServerSensorDescription(
        key="checks_total",
        translation_key="checks_total",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.total,
    ),
    ServerSensorDescription(
        key="checks_up",
        translation_key="checks_up",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.up,
    ),
    ServerSensorDescription(
        key="checks_problem",
        translation_key="checks_problem",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.problems,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: OctoPulseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities: list[SensorEntity] = [OctoPulseServerSensor(entry, d) for d in SERVER_SENSORS]
    if entry.runtime_data.heartbeat is not None:
        entities.append(OctoPulseHeartbeatSensor(entry))
    async_add_entities(entities)
    async_setup_check_entities(
        entry, async_add_entities, lambda e, check: [OctoPulseCheckSensor(e, check, d) for d in CHECK_SENSORS]
    )


class OctoPulseCheckSensor(OctoPulseCheckEntity, SensorEntity):
    entity_description: CheckSensorDescription

    def __init__(self, entry: OctoPulseConfigEntry, check: Check, description: CheckSensorDescription) -> None:
        super().__init__(entry, check, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | datetime | None:
        check = self.check
        return self.entity_description.value_fn(check) if check else None


class OctoPulseServerSensor(OctoPulseServerEntity, SensorEntity):
    entity_description: ServerSensorDescription

    def __init__(self, entry: OctoPulseConfigEntry, description: ServerSensorDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator)


class OctoPulseHeartbeatSensor(OctoPulseServerEntity, SensorEntity):
    """Wann Home Assistant zuletzt erfolgreich ein Lebenszeichen gesendet hat.

    Hängt nicht vom Abruf der Checks ab: Der Heartbeat läuft auch dann, wenn der API-Key abgelaufen ist.
    """

    _attr_translation_key = "heartbeat_last_sent"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: OctoPulseConfigEntry) -> None:
        super().__init__(entry, "heartbeat_last_sent")
        self._heartbeat = entry.runtime_data.heartbeat

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        return self._heartbeat.last_success

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        error = self._heartbeat.last_error
        return {"last_error": error} if error else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _changed() -> None:
            self.async_write_ha_state()

        self.async_on_remove(async_dispatcher_connect(self.hass, self._heartbeat.signal, _changed))
