"""Service actions resolve devices, not (hidden) entities."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.drift_beacon.const import DOMAIN

from .conftest import WORKSPACE_ID, FakeDriftBeacon, device_for


def _activity(hass: HomeAssistant, activity_id: str) -> str:
    return device_for(hass, f"{WORKSPACE_ID}:activity:{activity_id}").id


def _workspace(hass: HomeAssistant) -> str:
    return device_for(hass, WORKSPACE_ID).id


async def _call(hass: HomeAssistant, service: str, **target: object) -> None:
    await hass.services.async_call(DOMAIN, service, target, blocking=True)


@pytest.mark.parametrize(
    ("service", "method"),
    [
        ("track_activity", "TrackActivity"),
        ("pause_activity", "PauseSession"),
        ("pin_activity", "PinActivity"),
        ("unpin_activity", "UnpinActivity"),
        ("queue_activity", "QueueActivity"),
    ],
)
async def test_activity_services_take_activity_devices(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
    service: str,
    method: str,
) -> None:
    """Each activity service sends its RPC for the activity behind the device."""
    await _call(hass, service, device_id=_activity(hass, "reading"))
    assert server.calls_to(method) == [{"activityId": "reading"}]


async def test_track_is_decided_by_the_server(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Track always sends TrackActivity, so repeated presses cannot restart a session."""
    for _ in range(2):
        await _call(hass, "track_activity", device_id=_activity(hass, "reading"))
    assert server.calls_to("TrackActivity") == [{"activityId": "reading"}] * 2
    assert server.calls_to("StartSession") == []


async def test_workspace_services(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Stop and pause act on whatever is live, targeted at the workspace device."""
    await _call(hass, "stop_session", device_id=_workspace(hass))
    await _call(hass, "pause_session", device_id=_workspace(hass))
    assert server.calls_to("StopSession") == [{}]
    assert server.calls_to("PauseSession") == [{}]


async def test_pausing_what_is_not_live_does_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """NoLiveSession for pause_activity is the documented no-op."""
    server.errors["PauseSession"] = "NoLiveSession"
    await _call(hass, "pause_activity", device_id=_activity(hass, "reading"))


@pytest.mark.parametrize(
    "target",
    [
        {"entity_id": "switch.reading_session"},
        {"area_id": "office"},
        {"label_id": "focus"},
    ],
)
async def test_only_device_targets_are_accepted(
    hass: HomeAssistant, setup_integration: MockConfigEntry, target: dict[str, str]
) -> None:
    """Areas and labels could fan out across every activity; entities are not activities."""
    with pytest.raises(ServiceValidationError):
        await _call(hass, "track_activity", **target)


async def test_devices_must_match_the_service(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Activity services reject the workspace, workspace services reject activities."""
    with pytest.raises(ServiceValidationError, match="not a Drift Beacon activity"):
        await _call(hass, "pin_activity", device_id=_workspace(hass))
    with pytest.raises(ServiceValidationError, match="Choose its workspace device"):
        await _call(hass, "stop_session", device_id=_activity(hass, "reading"))

    other_entry = MockConfigEntry(domain="other")
    other_entry.add_to_hass(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=other_entry.entry_id, identifiers={("other", "x")}
    )
    for device_id in (other.id, "missing"):
        with pytest.raises(ServiceValidationError, match="not a Drift Beacon device"):
            await _call(hass, "pin_activity", device_id=device_id)


async def test_failures_are_reported_after_every_target_runs(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """One rejected target does not stop the others."""
    server.errors["QueueActivity"] = "ActivityArchived"
    with pytest.raises(HomeAssistantError, match="Some targets failed"):
        await _call(
            hass,
            "queue_activity",
            device_id=[_activity(hass, "reading"), _activity(hass, "water")],
        )
    assert len(server.calls_to("QueueActivity")) == 2
