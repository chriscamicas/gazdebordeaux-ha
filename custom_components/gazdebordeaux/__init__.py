"""The Gaz de Bordeaux integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import AUTO, CONF_ELECTRICITY_HOUSE, CONF_GAS_HOUSE, DOMAIN, HOUSE, NONE
from .coordinator import GdbCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Gaz de Bordeaux from a config entry."""

    coordinator = GdbCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload when the options flow changes credentials or contract mapping.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries."""
    _LOGGER.debug("Migrating config entry from version %s", entry.version)

    if entry.version == 1:
        # v1 tracked a single gas house: either the free-text HOUSE path or,
        # when empty, whatever /users/me returned. Keep gas tracking as-is and
        # leave electricity off until the user opts in from the options flow.
        data = {k: v for k, v in entry.data.items() if k != HOUSE}
        data[CONF_GAS_HOUSE] = entry.data.get(HOUSE) or AUTO
        data[CONF_ELECTRICITY_HOUSE] = NONE
        hass.config_entries.async_update_entry(entry, data=data, version=2)

    _LOGGER.debug("Migration to version %s successful", entry.version)
    return True
