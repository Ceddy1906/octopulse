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
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OctoPulseConfigEntry
from .api import Check, DeviceData
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
class DeviceSensorDescription(SensorEntityDescription):
    """Sensor mit Angaben aus der letzten Meldung des Windows-Clients (nur für Checks der Art "device")."""

    value_fn: Callable[[DeviceData], str | int | float | datetime | None]
    attrs_fn: Callable[[DeviceData], dict[str, Any]] | None = None


def _users_text(d: DeviceData) -> str:
    names = ", ".join(u.name for u in d.users)
    return names[:255] if names else "Niemand"  # Zustände sind in Home Assistant höchstens 255 Zeichen lang


def _os_text(d: DeviceData) -> str | None:
    return " ".join(p for p in (d.os_name, d.os_version) if p) or None


DEVICE_SENSORS: tuple[DeviceSensorDescription, ...] = (
    DeviceSensorDescription(
        key="host_name",
        translation_key="host_name",
        value_fn=lambda d: d.host_name or None,
        attrs_fn=lambda d: {"domain": d.domain} if d.domain else {},
    ),
    DeviceSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        value_fn=lambda d: d.address or None,
        attrs_fn=lambda d: {
            "addresses": [a for n in d.networks for a in n.addresses],
        },
    ),
    DeviceSensorDescription(
        key="public_ip",
        translation_key="public_ip",
        value_fn=lambda d: d.public_ip or None,
    ),
    DeviceSensorDescription(
        key="network",
        translation_key="network",
        value_fn=lambda d: d.network or None,
        attrs_fn=lambda d: {
            "connections": [{"name": n.name, "kind": n.kind, "addresses": list(n.addresses)} for n in d.networks]
        },
    ),
    DeviceSensorDescription(
        key="users",
        translation_key="users",
        value_fn=_users_text,
        attrs_fn=lambda d: {
            "users": [u.name for u in d.users],
            "disconnected": [u.name for u in d.users if not u.active],
        },
    ),
    DeviceSensorDescription(
        key="os",
        translation_key="os",
        value_fn=_os_text,
        attrs_fn=lambda d: {"build": d.os_build} if d.os_build else {},
    ),
    DeviceSensorDescription(
        key="boot",
        translation_key="boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.boot,
    ),
    DeviceSensorDescription(
        key="memory_used",
        translation_key="memory_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.memory_used_percent,
        attrs_fn=lambda d: {"total_mb": d.memory_total_mb, "free_mb": d.memory_free_mb},
    ),
    DeviceSensorDescription(
        key="agent_version",
        translation_key="agent_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.agent_version or None,
        attrs_fn=lambda d: {"time_zone": d.time_zone} if d.time_zone else {},
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
    async_setup_check_entities(entry, async_add_entities, _check_entities)
    _setup_disks(entry, async_add_entities)


def _check_entities(entry: OctoPulseConfigEntry, check: Check) -> list[SensorEntity]:
    entities: list[SensorEntity] = [OctoPulseCheckSensor(entry, check, d) for d in CHECK_SENSORS]
    if check.kind == "device":
        entities.extend(OctoPulseDeviceSensor(entry, check, d) for d in DEVICE_SENSORS)
    if check.kind == "dns":
        entities.append(OctoPulseResolvedIpSensor(entry, check))
    return entities


@callback
def _setup_disks(entry: OctoPulseConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Ein Sensor je Laufwerk. Welche Laufwerke es gibt, steht erst in der Meldung des Geräts: Neue kommen später dazu."""
    coordinator = entry.runtime_data.coordinator
    known: set[tuple[int, str]] = set()

    @callback
    def _add_new() -> None:
        new: list[SensorEntity] = []
        for check in (coordinator.data or {}).values():
            if check.device is None:
                continue
            for disk in check.device.disks:
                key = (check.id, disk.drive.upper())
                if key not in known:
                    known.add(key)
                    new.append(OctoPulseDiskSensor(entry, check, disk.drive))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


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


class OctoPulseDeviceSensor(OctoPulseCheckEntity, SensorEntity):
    """Eine Angabe aus der letzten Meldung des Windows-Clients. Nach dem Abmelden bleibt der letzte Stand stehen."""

    entity_description: DeviceSensorDescription

    def __init__(self, entry: OctoPulseConfigEntry, check: Check, description: DeviceSensorDescription) -> None:
        super().__init__(entry, check, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | float | datetime | None:
        check = self.check
        if check is None or check.device is None:
            return None
        return self.entity_description.value_fn(check.device)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        check = self.check
        fn = self.entity_description.attrs_fn
        if check is None or check.device is None or fn is None:
            return None
        return fn(check.device) or None


class OctoPulseDiskSensor(OctoPulseCheckEntity, SensorEntity):
    """Freier Platz eines festen Laufwerks in GB, mit Gesamtgröße und Prozent als Attribute."""

    _attr_translation_key = "disk_free"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, entry: OctoPulseConfigEntry, check: Check, drive: str) -> None:
        super().__init__(entry, check, f"disk_{drive.lower().rstrip(':')}")
        self._drive = drive.upper()
        self._attr_translation_placeholders = {"drive": drive.upper()}

    def _disk(self):
        check = self.check
        if check is None or check.device is None:
            return None
        return next((d for d in check.device.disks if d.drive.upper() == self._drive), None)

    @property
    def native_value(self) -> int | None:
        disk = self._disk()
        return None if disk is None else disk.free_gb

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        disk = self._disk()
        if disk is None:
            return None
        return {"total_gb": disk.total_gb, "free_percent": disk.free_percent}


class OctoPulseResolvedIpSensor(OctoPulseCheckEntity, SensorEntity):
    """Die IP-Adresse(n), auf die der Name eines DNS-Checks bei der letzten Auflösung zeigte.

    Bei mehreren Adressen (Round-Robin, IPv4 und IPv6) steht der Zustand mit Komma getrennt, die Liste steht im Attribut
    `addresses`. Das ist praktisch bei Dyn-DNS: Eine Automation kann auf eine Änderung des Zustands reagieren.
    """

    _attr_translation_key = "resolved_ip"

    def __init__(self, entry: OctoPulseConfigEntry, check: Check) -> None:
        super().__init__(entry, check, "resolved_ip")

    @property
    def native_value(self) -> str | None:
        check = self.check
        if check is None or check.dns is None:
            return None
        return ", ".join(check.dns.found)[:255]  # Zustände sind in Home Assistant höchstens 255 Zeichen lang

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        check = self.check
        if check is None:
            return None
        attrs: dict[str, Any] = {"expected": list(check.expected_ips)}
        if check.dns is not None:
            attrs["addresses"] = list(check.dns.found)
            if check.dns.ttl_s is not None:
                attrs["ttl"] = check.dns.ttl_s
        if check.dns_match:
            attrs["match"] = check.dns_match
        if check.dns_on_change:
            attrs["on_change"] = check.dns_on_change
        return attrs
