"""Diagnostics for the WA Synergy integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant

from . import SynergyRuntimeData


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry[SynergyRuntimeData],
) -> dict[str, Any]:
    """Return safe config-entry and companion-service diagnostics."""

    snapshot = entry.runtime_data.coordinator.data
    return {
        "config": {CONF_URL: entry.data[CONF_URL]},
        "status": asdict(snapshot.status),
        "imported_point_count": len(snapshot.statistics),
    }
