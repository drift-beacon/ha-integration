"""Entity state and actions."""

from __future__ import annotations

import pytest
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.switch import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import FakeDriftBeacon, activity, live_session, wait_for


async def _call(hass: HomeAssistant, domain: str, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_session_switch_starts_stops_and_never_restarts(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Turning on a live session is a no-op, because StartSession would restart it."""
    await _call(hass, "switch", SERVICE_TURN_ON, "switch.reading_session")
    assert server.calls_to("StartSession") == [{"activityId": "reading"}]

    await server.push(
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")}
    )
    await wait_for(lambda: hass.states.get("switch.reading_session").state == "on")
    state = hass.states.get("switch.reading_session")
    assert state.attributes["session_id"] == "s1"
    assert state.attributes["member_ids"] == ["user-1"]

    await _call(hass, "switch", SERVICE_TURN_ON, "switch.reading_session")
    assert len(server.calls_to("StartSession")) == 1

    await _call(hass, "switch", SERVICE_TURN_OFF, "switch.reading_session")
    assert server.calls_to("StopSession") == [{"activityId": "reading"}]


async def test_pin_switch_and_mark_button(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Pin/unpin and mark go to the server for the entity's own activity."""
    await _call(hass, "switch", SERVICE_TURN_ON, "switch.water_pin")
    await _call(hass, "switch", SERVICE_TURN_OFF, "switch.water_pin")
    await _call(hass, "button", SERVICE_PRESS, "button.water_mark")
    await _call(hass, "button", SERVICE_PRESS, "button.home_stop_session")
    assert server.calls_to("PinActivity") == [{"activityId": "water"}]
    assert server.calls_to("UnpinActivity") == [{"activityId": "water"}]
    assert server.calls_to("Mark") == [{"activityId": "water"}]
    assert server.calls_to("StopSession") == [{}]


async def test_server_rejections_surface_to_the_caller(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Automations see failures instead of a logged-and-swallowed error."""
    server.errors["Mark"] = "ActivityArchived"
    with pytest.raises(HomeAssistantError, match="ActivityArchived"):
        await _call(hass, "button", SERVICE_PRESS, "button.water_mark")


async def test_benign_rejections_are_no_ops(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Unpinning what is not pinned, or stopping what is not live, already holds."""
    server.errors["UnpinActivity"] = "NotPinned"
    server.errors["StopSession"] = "NoLiveSession"
    await _call(hass, "switch", SERVICE_TURN_OFF, "switch.water_pin")
    await _call(hass, "switch", SERVICE_TURN_OFF, "switch.reading_session")


async def test_workspace_sensors(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Current session and pinned activity name the activity and point at its device."""
    assert hass.states.get("sensor.home_current_session").state == "unknown"
    assert hass.states.get("sensor.home_connected_user").state == "Rich"

    await server.push(
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")},
        {
            "_tag": "PinnedActivityChanged",
            "pinnedActivity": {
                "activityId": "water",
                "pinnedAt": "2026-09-24T09:00:00Z",
            },
        },
    )
    await wait_for(
        lambda: hass.states.get("sensor.home_current_session").state == "Reading"
    )
    current = hass.states.get("sensor.home_current_session")
    assert current.attributes["activity_id"] == "reading"
    assert current.attributes["started_at"] == "2026-09-24T10:00:00.000Z"
    assert current.attributes["icon"] == "mdi:book"
    assert current.attributes["activity_device_id"]
    pinned = hass.states.get("sensor.home_pinned_activity")
    assert pinned.state == "Water"
    assert pinned.attributes["pinned_at"] == "2026-09-24T09:00:00Z"


async def test_progress_sensors(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Timed progress is a duration; point progress is a count in the activity's unit."""
    await server.push(
        {
            "_tag": "ActivityUpdated",
            "activity": activity(
                "reading", "Reading", progress={"current": 5400.4, "target": 7200}
            ),
        },
        {
            "_tag": "ActivityUpdated",
            "activity": activity(
                "water",
                "Water",
                "point",
                unit="glasses",
                progress={"current": 3, "target": 8},
            ),
        },
    )
    await wait_for(lambda: hass.states.get("sensor.water_progress").state == "3.0")
    reading = hass.states.get("sensor.reading_progress")
    assert reading.attributes["device_class"] == "duration"
    assert float(reading.state) == pytest.approx(1.5, abs=0.01)  # suggested unit: hours
    assert reading.attributes["target"] == 7200
    water = hass.states.get("sensor.water_progress")
    assert water.attributes["unit_of_measurement"] == "glasses"
    assert water.attributes["target"] == 8
    assert water.attributes["color"] == [51, 102, 153]


async def test_updates_only_rewrite_affected_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """A change to one activity leaves the other activities' states untouched."""
    untouched = ("sensor.water_progress", "switch.water_pin", "switch.reading_pin")
    before = {entity_id: hass.states.get(entity_id) for entity_id in untouched}
    await server.push(
        {
            "_tag": "ActivityUpdated",
            "activity": activity(
                "reading", "Reading", progress={"current": 60, "target": None}
            ),
        }
    )
    await wait_for(lambda: hass.states.get("sensor.reading_progress").state != "0.0")
    for entity_id, state in before.items():
        assert hass.states.get(entity_id).last_reported == state.last_reported
