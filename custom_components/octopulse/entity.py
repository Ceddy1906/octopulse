"""Gemeinsame Grundlagen der Entitäten."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Check, host_of
from .const import DOMAIN
from .coordinator import OctoPulseCoordinator

if TYPE_CHECKING:
    from . import OctoPulseConfigEntry

KIND_LABELS = {"heartbeat": "Heartbeat", "http": "HTTP(S)", "tcp": "TCP-Port", "tls": "TLS-Zertifikat"}


# Neuere Home-Assistant-Versionen verknüpfen Geräte über die Geräte-ID (via_device_id) und stufen den Verweis über
# Kennungen (via_device) als veraltet ein. Ältere kennen nur via_device. Beides wird unterstützt.
_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def server_device(entry: "OctoPulseConfigEntry") -> DeviceInfo:
    """Das Gerät für den OctoPulse-Server. Die Checks hängen als eigene Geräte daran."""
    base_url = entry.runtime_data.client.base_url
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"OctoPulse ({host_of(base_url)})",
        manufacturer="OctoPulse",
        model="Server",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=base_url,
    )


class OctoPulseServerEntity(CoordinatorEntity[OctoPulseCoordinator]):
    """Entität, die zum Server als Ganzem gehört (Zähler, Sammelstatus, Heartbeat)."""

    _attr_has_entity_name = True

    def __init__(self, entry: "OctoPulseConfigEntry", key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = server_device(entry)


class OctoPulseCheckEntity(CoordinatorEntity[OctoPulseCoordinator]):
    """Entität eines einzelnen Checks. Fällt aus, wenn der Check in OctoPulse gelöscht wurde."""

    _attr_has_entity_name = True

    def __init__(self, entry: "OctoPulseConfigEntry", check: Check, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._check_id = check.id
        self._attr_unique_id = f"{entry.entry_id}_{check.id}_{key}"
        base_url = entry.runtime_data.client.base_url
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{check.id}")},
            name=check.name,
            manufacturer="OctoPulse",
            model=KIND_LABELS.get(check.kind, check.kind or "Check"),
            configuration_url=f"{base_url}/checks/{check.id}",
        )
        if _VIA_DEVICE_ID:
            info["via_device_id"] = entry.runtime_data.server_device_id
        else:
            info["via_device"] = (DOMAIN, entry.entry_id)  # type: ignore[typeddict-unknown-key]
        self._attr_device_info = info

    @property
    def check(self) -> Check | None:
        return self.coordinator.data.get(self._check_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.check is not None


@callback
def async_setup_check_entities(
    entry: "OctoPulseConfigEntry",
    async_add_entities: AddEntitiesCallback,
    factory: Callable[["OctoPulseConfigEntry", Check], list[Entity]],
) -> None:
    """Legt Entitäten für alle vorhandenen Checks an und später für jeden neu hinzugekommenen."""
    coordinator = entry.runtime_data.coordinator
    known: set[int] = set()

    @callback
    def _add_new() -> None:
        entities: list[Entity] = []
        for check in (coordinator.data or {}).values():
            if check.id not in known:
                known.add(check.id)
                entities.extend(factory(entry, check))
        if entities:
            async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
