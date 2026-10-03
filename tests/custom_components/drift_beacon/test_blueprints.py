"""The bundled blueprints, run as real automations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import automation
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml

BLUEPRINTS = (
    Path(__file__).parents[3] / "custom_components" / "drift_beacon" / "blueprints"
)


def _automation(name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Substitute a blueprint's inputs, validating both with Home Assistant."""
    blueprint = Blueprint(
        load_yaml(BLUEPRINTS / name),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    filled = BlueprintInputs(
        blueprint, {"use_blueprint": {"path": name, "input": inputs}}
    )
    filled.validate()
    return {"id": name, **filled.async_substitute()}


async def _setup(hass: HomeAssistant, config: dict[str, Any]) -> None:
    assert await async_setup_component(
        hass, automation.DOMAIN, {automation.DOMAIN: config}
    )
    await hass.async_block_till_done()


def _record(hass: HomeAssistant, domain: str, *services: str) -> list[ServiceCall]:
    calls: list[ServiceCall] = []
    for service in services:
        hass.services.async_register(domain, service, calls.append)
    return calls


def _event(event_type: str) -> list[dict[str, str]]:
    return [{"trigger": "event", "event_type": event_type}]


@pytest.mark.parametrize("name", sorted(p.name for p in BLUEPRINTS.glob("*.yaml")))
def test_blueprints_are_valid(name: str) -> None:
    """Every bundled blueprint passes Home Assistant's blueprint schema."""
    Blueprint(
        load_yaml(BLUEPRINTS / name),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )


ACTIVITY_SERVICES = [
    "track_activity",
    "pause_activity",
    "pin_activity",
    "queue_activity",
    "unpin_activity",
]


async def test_activity_controls_route_each_trigger_to_its_service(
    hass: HomeAssistant,
) -> None:
    """The flat trigger index picks the mapping; the activity device is the target."""
    calls = _record(hass, "drift_beacon", *ACTIVITY_SERVICES)
    await _setup(
        hass,
        _automation(
            "drift_beacon_activity_controls.yaml",
            {
                "activity": "activity-device",
                # Two track triggers, one disabled, so later mappings shift by position.
                "track_trigger": [*_event("t1"), {**_event("t2")[0], "enabled": False}],
                "pin_trigger": _event("p"),
                "unpin_trigger": _event("u"),
            },
        ),
    )
    for event_type in ("t1", "p", "u"):
        hass.bus.async_fire(event_type)
        await hass.async_block_till_done()
    assert [call.service for call in calls] == [
        "track_activity",
        "pin_activity",
        "unpin_activity",
    ]
    assert all(call.data["device_id"] == ["activity-device"] for call in calls)


async def test_activity_controls_manual_run_does_nothing(hass: HomeAssistant) -> None:
    """Triggering the automation by hand cannot fall through to an action."""
    calls = _record(hass, "drift_beacon", *ACTIVITY_SERVICES)
    config = _automation(
        "drift_beacon_activity_controls.yaml",
        {"activity": "activity-device", "track_trigger": _event("t")},
    )
    await _setup(hass, {**config, "alias": "Controls"})
    assert hass.states.get("automation.controls") is not None
    await hass.services.async_call(
        automation.DOMAIN,
        "trigger",
        {"entity_id": "automation.controls"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert calls == []


async def test_session_controls_target_the_workspace(hass: HomeAssistant) -> None:
    """Stop and pause need only the workspace, never an activity."""
    calls = _record(hass, "drift_beacon", "stop_session", "pause_session")
    await _setup(
        hass,
        _automation(
            "drift_beacon_session_controls.yaml",
            {
                "workspace": "workspace-device",
                "stop_trigger": _event("stop"),
                "pause_trigger": _event("pause"),
            },
        ),
    )
    for event_type in ("pause", "stop"):
        hass.bus.async_fire(event_type)
        await hass.async_block_till_done()
    assert [call.service for call in calls] == ["pause_session", "stop_session"]
    assert all(call.data["device_id"] == ["workspace-device"] for call in calls)


def _focus(
    state: str, color: list[int] | None, device: str = "workspace-device"
) -> dict:
    return {"workspace_device_id": device, "state": state, "color": color}


@pytest.mark.parametrize("disable_when", [None, []])
async def test_lighting_follows_focus(
    hass: HomeAssistant, disable_when: list | None
) -> None:
    """Live is bright, pinned is dim, idle is off; other workspaces are ignored."""
    calls = _record(hass, "light", "turn_on", "turn_off")
    inputs: dict[str, Any] = {
        "workspace": "workspace-device",
        "target_lights": {"entity_id": "light.desk"},
    }
    if disable_when is not None:
        inputs["disable_when"] = disable_when
    await _setup(hass, _automation("drift_beacon_activity_lighting.yaml", inputs))

    for data in (
        _focus("live", [255, 0, 0]),
        _focus("pinned", [0, 0, 255]),
        _focus("idle", None),
        _focus("live", [255, 0, 0], device="another-workspace"),
    ):
        hass.bus.async_fire("drift_beacon_focus_changed", data)
        await hass.async_block_till_done()

    assert [
        (c.service, c.data.get("rgb_color"), c.data.get("brightness_pct"))
        for c in calls
    ] == [
        ("turn_on", [255, 0, 0], 100),
        ("turn_on", [0, 0, 255], 5),
        ("turn_off", None, None),
    ]


async def test_lighting_respects_disable_conditions(hass: HomeAssistant) -> None:
    """While a disable condition holds, the lights are left alone."""
    calls = _record(hass, "light", "turn_on", "turn_off")
    hass.states.async_set("input_boolean.movie_mode", "on")
    await _setup(
        hass,
        _automation(
            "drift_beacon_activity_lighting.yaml",
            {
                "workspace": "workspace-device",
                "target_lights": {"entity_id": "light.desk"},
                "disable_when": [
                    {
                        "condition": "state",
                        "entity_id": "input_boolean.movie_mode",
                        "state": "on",
                    }
                ],
            },
        ),
    )
    hass.bus.async_fire("drift_beacon_focus_changed", _focus("live", [255, 0, 0]))
    await hass.async_block_till_done()
    assert calls == []

    hass.states.async_set("input_boolean.movie_mode", "off")
    hass.bus.async_fire("drift_beacon_focus_changed", _focus("live", [255, 0, 0]))
    await hass.async_block_till_done()
    assert [call.service for call in calls] == ["turn_on"]
