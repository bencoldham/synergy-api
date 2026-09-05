"""WA Synergy Home Assistant integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SynergyServiceClient
from .const import CONF_API_TOKEN
from .coordinator import SynergyCoordinator

_PLATFORMS = (Platform.SENSOR,)


@dataclass(slots=True)
class SynergyRuntimeData:
    """Runtime objects owned by one config entry."""

    client: SynergyServiceClient
    coordinator: SynergyCoordinator


SynergyConfigEntry = ConfigEntry[SynergyRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SynergyConfigEntry) -> bool:
    """Set up WA Synergy from a config entry."""

    client = SynergyServiceClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_API_TOKEN],
    )
    coordinator = SynergyCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = SynergyRuntimeData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_update_options(hass: HomeAssistant, entry: SynergyConfigEntry) -> None:
    """Recreate local tariff entities when the selected plan changes."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SynergyConfigEntry) -> bool:
    """Unload a WA Synergy config entry."""

    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
