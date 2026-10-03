"""Device triggers on activity devices, and diagnostics."""

from __future__ import annotations

from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.drift_beacon.const import DOMAIN

from .conftest import (
    WORKSPACE_ID,
    FakeDriftBeacon,
    device_for,
    live_session,
    schedule_trigger,
    wait_for,
)


def _types(triggers: list[dict]) -> set[str]:
    return {trigger["type"] for trigger in triggers}


async def test_triggers_follow_the_tracking_type(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Timed activities offer session triggers; point activities offer marked; both, the rest."""
    reading = device_for(hass, f"{WORKSPACE_ID}:activity:reading")
    water = device_for(hass, f"{WORKSPACE_ID}:activity:water")
    workspace = device_for(hass, WORKSPACE_ID)
    ours = lambda triggers: [t for t in triggers if t["domain"] == DOMAIN]
    reading_triggers = ours(
        await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, reading.id
        )
    )
    water_triggers = ours(
        await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, water.id)
    )
    workspace_triggers = ours(
        await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, workspace.id
        )
    )
    assert _types(reading_triggers) == {
        "session_started",
        "session_stopped",
        "pinned",
        "unpinned",
        "schedule_triggered",
    }
    assert _types(water_triggers) == {
        "marked",
        "pinned",
        "unpinned",
        "schedule_triggered",
    }
    assert workspace_triggers == []


async def test_session_started_trigger_fires_for_its_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """The trigger listens to the bus event for exactly this activity device."""
    reading = device_for(hass, f"{WORKSPACE_ID}:activity:reading")
    calls: list[ServiceCall] = []
    hass.services.async_register("test", "record", calls.append)
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "triggers": {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": reading.id,
                    "type": "session_started",
                },
                "actions": {
                    "action": "test.record",
                    "data": {"session": "{{ trigger.event.data.session_id }}"},
                },
            }
        },
    )
    await server.push(
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")}
    )
    await wait_for(lambda: len(calls) == 1)
    assert calls[0].data["session"] == "s1"


async def test_schedule_triggered_trigger_fires_for_its_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """A schedule of this activity fires the trigger; another activity's does not."""
    reading = device_for(hass, f"{WORKSPACE_ID}:activity:reading")
    calls: list[ServiceCall] = []
    hass.services.async_register("test", "record", calls.append)
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "triggers": {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": reading.id,
                    "type": "schedule_triggered",
                },
                "actions": {
                    "action": "test.record",
                    "data": {
                        "occurrence": "{{ trigger.event.data.occurrence_id }}",
                        "behavior": "{{ trigger.event.data.behavior }}",
                    },
                },
            }
        },
    )
    await server.push(
        {"_tag": "ScheduleTriggered", "trigger": schedule_trigger("hydrate", "water")},
        {
            "_tag": "ScheduleTriggered",
            "trigger": schedule_trigger("morning", "reading", behavior="queue"),
        },
    )
    await wait_for(lambda: len(calls) == 1)
    assert calls[0].data == {"occurrence": "morning:2026-09-24", "behavior": "queue"}


async def test_diagnostics_redact_the_token(
    hass: HomeAssistant, hass_client, setup_integration: MockConfigEntry
) -> None:
    """The access token never appears in diagnostics."""
    assert await async_setup_component(hass, "diagnostics", {})
    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, setup_integration
    )
    assert diagnostics["entry"]["api_token"] == "**REDACTED**"
    assert diagnostics["state"]["activities"] == 2
    assert diagnostics["activity_devices"] == 2
