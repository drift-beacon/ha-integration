"""Diagnostics for Drift Beacon config entries and devices."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_API_TOKEN
from .coordinator import DriftBeaconConfigEntry

TO_REDACT = {CONF_API_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DriftBeaconConfigEntry
) -> dict[str, Any]:
    """Connection settings (token redacted) and a summary of the workspace state."""
    coordinator = entry.runtime_data
    state = coordinator.data
    activities = list(state.activities.values()) if state else []
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "connected": coordinator.last_update_success,
        "workspace_device_id": coordinator.devices.workspace_device_id,
        "activity_devices": sum(
            coordinator.devices.activity_id_for_device(device) is not None
            for device in dr.async_entries_for_config_entry(
                dr.async_get(hass), entry.entry_id
            )
        ),
        "state": {
            "activities": len(activities),
            "timed_activities": sum(a.tracking_type == "span" for a in activities),
            "point_activities": sum(a.tracking_type == "point" for a in activities),
            "categories": len(state.categories) if state else 0,
            "live_session": state.live_session is not None if state else None,
            "pinned": state.pinned is not None if state else None,
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: DriftBeaconConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """The activity a device represents, as Home Assistant currently sees it."""
    coordinator = entry.runtime_data
    activity_id = coordinator.devices.activity_id_for_device(device)
    activity = coordinator.data.activity(activity_id) if coordinator.data else None
    return {
        "activity_id": activity_id,
        "activity": None
        if activity is None
        else {
            "tracking_type": activity.tracking_type,
            "progress": activity.progress,
            "target": activity.target,
            "has_category": activity.category_id is not None,
        },
        "is_live": bool(
            activity_id
            and coordinator.data
            and coordinator.data.live_session
            and coordinator.data.live_session.activity_id == activity_id
        ),
        "is_pinned": bool(
            activity_id
            and coordinator.data
            and coordinator.data.pinned
            and coordinator.data.pinned.activity_id == activity_id
        ),
    }
