"""Consumption summaries and synchronization diagnostics for WA Synergy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ServiceStatus, UsagePeriod, UsageSummary
from .const import DOMAIN
from .coordinator import SynergyCoordinator

if TYPE_CHECKING:
    from . import SynergyRuntimeData


def _sync_status(status: ServiceStatus) -> str:
    if status.syncing:
        return "syncing"
    if status.last_error:
        return "error"
    return "idle" if status.last_success is not None else "waiting"


def _data_age(status: ServiceStatus) -> float | None:
    if status.data_through is None:
        return None
    return (datetime.now(UTC) - status.data_through).total_seconds() / 3600


_STATUS_SENSORS: tuple[
    tuple[
        SensorEntityDescription,
        Callable[[ServiceStatus], datetime | str | float | None],
    ],
    ...,
] = (
    (
        SensorEntityDescription(
            key="data_through",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        lambda status: status.data_through,
    ),
    (
        SensorEntityDescription(
            key="last_success",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        lambda status: status.last_success,
    ),
    (
        SensorEntityDescription(
            key="data_age",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.HOURS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        _data_age,
    ),
    (
        SensorEntityDescription(
            key="sync_status",
            device_class=SensorDeviceClass.ENUM,
            options=["waiting", "syncing", "idle", "error"],
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        _sync_status,
    ),
)

_PERIOD_SENSORS: tuple[
    tuple[str, Callable[[UsageSummary], UsagePeriod | None]], ...
] = (
    ("latest_day", lambda summary: summary.latest_day),
    ("last_7_days", lambda summary: summary.last_7_days),
    ("month_to_date", lambda summary: summary.month_to_date),
)


class _SynergySensor(CoordinatorEntity[SynergyCoordinator], SensorEntity):
    """Stable identity and service device shared by all Synergy sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SynergyCoordinator,
        entry: ConfigEntry[SynergyRuntimeData],
        description: SensorEntityDescription,
        service_point_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_translation_key = description.key
        identity = entry.unique_id or entry.entry_id
        if service_point_id is not None:
            identity = f"{identity}_{service_point_id.casefold()}"
            self._attr_translation_placeholders = {"service_point_id": service_point_id}
        self._attr_unique_id = f"{identity}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Synergy",
            name="WA Synergy",
        )


class SynergyStatusSensor(_SynergySensor):
    """Provider synchronization state and delayed-data freshness."""

    def __init__(
        self,
        coordinator: SynergyCoordinator,
        entry: ConfigEntry[SynergyRuntimeData],
        description: SensorEntityDescription,
        value: Callable[[ServiceStatus], datetime | str | float | None],
    ) -> None:
        super().__init__(coordinator, entry, description)
        self._value = value

    @property
    def native_value(self) -> datetime | str | float | None:
        return self._value(self.coordinator.data.status)

    @property
    def extra_state_attributes(self) -> dict[str, str | None] | None:
        if self.entity_description.key == "sync_status":
            return {"last_error": self.coordinator.data.status.last_error}
        return None


class SynergyGridImportSensor(_SynergySensor):
    """Cumulative stored import; corrections may legitimately reduce the total."""

    def __init__(
        self,
        coordinator: SynergyCoordinator,
        entry: ConfigEntry[SynergyRuntimeData],
        service_point_id: str,
    ) -> None:
        super().__init__(
            coordinator,
            entry,
            SensorEntityDescription(
                key="grid_import",
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                state_class=SensorStateClass.TOTAL,
                suggested_display_precision=3,
            ),
            service_point_id,
        )
        self._service_point_id = service_point_id

    @property
    def native_value(self) -> Decimal | None:
        summary = self.coordinator.data.summaries.get(self._service_point_id)
        return summary.total_import_kwh if summary is not None else None


class SynergyPeriodSensor(_SynergySensor):
    """A dated consumption snapshot, not an accumulating meter."""

    def __init__(
        self,
        coordinator: SynergyCoordinator,
        entry: ConfigEntry[SynergyRuntimeData],
        service_point_id: str,
        key: str,
        period: Callable[[UsageSummary], UsagePeriod | None],
    ) -> None:
        super().__init__(
            coordinator,
            entry,
            SensorEntityDescription(
                key=key,
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                suggested_display_precision=3,
            ),
            service_point_id,
        )
        self._service_point_id = service_point_id
        self._period = period

    @property
    def _current_period(self) -> UsagePeriod | None:
        summary = self.coordinator.data.summaries.get(self._service_point_id)
        return self._period(summary) if summary is not None else None

    @property
    def native_value(self) -> Decimal | None:
        period = self._current_period
        return period.import_kwh if period is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        period = self._current_period
        if period is None:
            return None
        return {
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[SynergyRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up per-service-point consumption and account-level diagnostics."""

    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        SynergyStatusSensor(coordinator, entry, description, value)
        for description, value in _STATUS_SENSORS
    ]
    for service_point_id in coordinator.data.status.service_points:
        entities.append(SynergyGridImportSensor(coordinator, entry, service_point_id))
        entities.extend(
            SynergyPeriodSensor(coordinator, entry, service_point_id, key, period)
            for key, period in _PERIOD_SENSORS
        )
    async_add_entities(entities)
