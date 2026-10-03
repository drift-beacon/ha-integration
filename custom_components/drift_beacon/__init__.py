"""The Drift Beacon integration."""

from __future__ import annotations

import logging
from functools import partial

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .api import base_url
from .const import CONF_PROTOCOL, DOMAIN, PLATFORMS
from .coordinator import (
    DriftBeaconConfigEntry,
    DriftBeaconCoordinator,
    workspace_issue_id,
)
from .plugin_services import PLUGIN_SERVICES, PluginServices
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Entries from before the device-per-activity model cannot be migrated.
CURRENT_VERSION = 4


def legacy_issue_id(entry_id: str) -> str:
    """Repair issue for an entry from before the device-per-activity model."""
    return f"legacy_entry_{entry_id}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide service actions, and the last known plugin commands."""
    async_setup_services(hass)
    plugin_services = PluginServices(hass)
    hass.data[PLUGIN_SERVICES] = plugin_services
    await plugin_services.async_load()
    async_dispatcher_connect(
        hass, SIGNAL_CONFIG_ENTRY_CHANGED, plugin_services.async_entry_changed
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Refuse entries from the old entity model and tell the user to add them again."""
    if entry.version < CURRENT_VERSION:
        ir.async_create_issue(
            hass,
            DOMAIN,
            legacy_issue_id(entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="legacy_entry",
            translation_placeholders={"title": entry.title},
        )
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DriftBeaconConfigEntry) -> bool:
    """Connect to the workspace and set up its devices."""
    coordinator = DriftBeaconCoordinator(hass, entry)
    coordinator.on_plugin_commands = partial(
        hass.data[PLUGIN_SERVICES].async_set, entry.entry_id
    )
    # Activity devices link to the workspace device's id, so it must exist first.
    coordinator.devices.async_register_workspace(
        entry.title,
        base_url(
            entry.data[CONF_PROTOCOL], entry.data[CONF_HOST], entry.data[CONF_PORT]
        ),
    )
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: DriftBeaconConfigEntry
) -> bool:
    """Unload the platforms, then close the connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Its plugin services stay (calls fail until it's back), unless it was disabled.
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: DriftBeaconConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting activity devices whose activity is gone; never the workspace."""
    if entry.state is not ConfigEntryState.LOADED:
        return False
    coordinator = entry.runtime_data
    activity_id = coordinator.devices.activity_id_for_device(device)
    return activity_id is not None and coordinator.data.activity(activity_id) is None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the entry's repair issues and plugin services once it is removed."""
    ir.async_delete_issue(hass, DOMAIN, legacy_issue_id(entry.entry_id))
    ir.async_delete_issue(hass, DOMAIN, workspace_issue_id(entry.entry_id))
    if (plugin_services := hass.data.get(PLUGIN_SERVICES)) is not None:
        plugin_services.async_drop(entry.entry_id)
