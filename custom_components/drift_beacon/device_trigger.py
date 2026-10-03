"""Device triggers for activity devices, backed by the integration's bus events."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_automation import (
    DEVICE_TRIGGER_BASE_SCHEMA,
    InvalidDeviceAutomationConfig,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ACTIVITY_DEVICE_ID,
    DOMAIN,
    EVENT_ACTIVITY_MARKED,
    EVENT_ACTIVITY_PINNED,
    EVENT_ACTIVITY_UNPINNED,
    EVENT_SCHEDULE_TRIGGERED,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_STOPPED,
    MODEL_ACTIVITY,
)

# Trigger type -> (bus event, tracking types it applies to)
TRIGGERS: dict[str, tuple[str, frozenset[str]]] = {
    "session_started": (EVENT_SESSION_STARTED, frozenset({"span"})),
    "session_stopped": (EVENT_SESSION_STOPPED, frozenset({"span"})),
    "marked": (EVENT_ACTIVITY_MARKED, frozenset({"point"})),
    "pinned": (EVENT_ACTIVITY_PINNED, frozenset({"span", "point"})),
    "unpinned": (EVENT_ACTIVITY_UNPINNED, frozenset({"span", "point"})),
    "schedule_triggered": (EVENT_SCHEDULE_TRIGGERED, frozenset({"span", "point"})),
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGERS)}
)


def _activity_device(hass: HomeAssistant, device_id: str) -> dr.DeviceEntry | None:
    device = dr.async_get(hass).async_get(device_id)
    if device is None or device.model != MODEL_ACTIVITY:
        return None
    return device


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Reject triggers on devices that are not activities."""
    config = TRIGGER_SCHEMA(config)
    if _activity_device(hass, config[CONF_DEVICE_ID]) is None:
        raise InvalidDeviceAutomationConfig(
            f"Device {config[CONF_DEVICE_ID]} is not a Drift Beacon activity"
        )
    return config


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List the triggers that apply to an activity's tracking type."""
    device = _activity_device(hass, device_id)
    if device is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type, (_, tracking_types) in TRIGGERS.items()
        if device.model_id in tracking_types
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for the trigger's bus event on this activity's device."""
    event_type, _ = TRIGGERS[config[CONF_TYPE]]
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: event_type,
            event_trigger.CONF_EVENT_DATA: {
                ATTR_ACTIVITY_DEVICE_ID: config[CONF_DEVICE_ID]
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
