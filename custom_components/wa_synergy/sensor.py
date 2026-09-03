"""Diagnostic sensors for WA Synergy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ServiceStatus
from .const import DOMAIN
from .coordinator import SynergyCoordinator

if TYPE_CHECKING:
    from . import SynergyRuntimeData


class SynergyStatusSensor(CoordinatorEntity[SynergyCoordinator], SensorEntity):
    """A timestamp reported by the companion service."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SynergyCoordinator,
        entry: ConfigEntry[SynergyRuntimeData],
        key: str,
        value: Callable[[ServiceStatus], datetime | None],
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._value = value
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Synergy",
            name="WA Synergy",
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the current timestamp."""

        return self._value(self.coordinator.data.status)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[SynergyRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WA Synergy status sensors."""

    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        (
            SynergyStatusSensor(
                coordinator,
                entry,
                "data_through",
                lambda status: status.data_through,
            ),
            SynergyStatusSensor(
                coordinator,
                entry,
                "last_success",
                lambda status: status.last_success,
            ),
        )
    )
