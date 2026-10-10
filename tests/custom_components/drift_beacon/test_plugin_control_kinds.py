"""A plugin's toggle, range and trigger: a switch, a number and a button on the workspace device.

The select has its own file; what the kinds share (the reconciler, availability, writes only on
change) is tested there for the select and here once for each other platform.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.drift_beacon.models import (
    PluginEntry,
    parse_plugin_controls,
    same_value,
)

from .conftest import WORKSPACE_ID, FakeDriftBeacon, wait_for

POWER = {
    "plugin": "lamp",
    "pluginName": "Lamp",
    "control": "power",
    "kind": "toggle",
    "title": "Power",
    "icon": "mdi:power",
    "command": "setPower",
    "field": "on",
}
LEVEL = {
    "plugin": "lamp",
    "pluginName": "Lamp",
    "control": "level",
    "kind": "range",
    "title": "Level",
    "command": "setLevel",
    "field": "level",
    "min": 0,
    "max": 100,
    "integer": True,
    "step": 5,
    "unit": "%",
}
BLINK = {
    "plugin": "lamp",
    "pluginName": "Lamp",
    "control": "blink",
    "kind": "trigger",
    "title": "Blink",
    "command": "blink",
}
SWITCH = "switch.home_lamp_power"
NUMBER = "number.home_lamp_level"
BUTTON = "button.home_lamp_blink"
ENTITIES = {"toggle": SWITCH, "range": NUMBER, "trigger": BUTTON}


def lamp(
    power: Any = True, level: Any = 40, *, status: str = "running"
) -> dict[str, Any]:
    """The lamp's entry of a PluginValues message; None leaves a value unknown."""
    running = status == "running"

    def held(value: Any) -> dict[str, Any] | None:
        return {"value": value} if running and value is not None else None

    return {
        "plugin": "lamp",
        "status": status,
        "choices": {},
        "controls": {"power": held(power), "level": held(level)},
    }


@pytest.fixture
def with_lamp(server: FakeDriftBeacon) -> None:
    """The server knows the lamp's three controls: on, at 40."""
    server.plugin_controls = [POWER, LEVEL, BLINK]
    server.plugin_values = [lamp()]


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """The entity's state, which must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} has no state"
    return state


async def call(
    hass: HomeAssistant, domain: str, service: str, entity_id: str, **data: Any
) -> None:
    """Call an entity service as a dashboard or an automation does."""
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


# ----------------------------------------------------------------------
# What the catalogue says
# ----------------------------------------------------------------------


def test_the_catalogue_keeps_each_kind_with_its_own_parts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A toggle, a range and a trigger are read by kind; a malformed one costs only itself."""
    power, level, blink, plain = parse_plugin_controls(
        [
            POWER,
            LEVEL,
            BLINK,
            # No step, no unit, fractions: each is its own.
            {
                **LEVEL,
                "control": "warmth",
                "min": -1.5,
                "max": 1.5,
                "integer": False,
                "step": None,
                "unit": "",
            },
            # Left out, each with a warning: ends that aren't numbers or aren't apart, a step of
            # nothing, a toggle with no field, a trigger with no command.
            {**LEVEL, "control": "a", "min": "0"},
            {**LEVEL, "control": "b", "max": True},
            {**LEVEL, "control": "c", "min": 100},
            {**LEVEL, "control": "d", "max": float("inf")},
            {**LEVEL, "control": "e", "step": 0},
            {**{k: v for k, v in POWER.items() if k != "field"}, "control": "f"},
            {**{k: v for k, v in BLINK.items() if k != "command"}, "control": "g"},
        ]
    )
    assert (power.kind, power.command, power.field, power.icon) == (
        "toggle",
        "setPower",
        "on",
        "mdi:power",
    )
    assert (
        level.kind,
        level.min,
        level.max,
        level.integer,
        level.step,
        level.unit,
    ) == (
        "range",
        0,
        100,
        True,
        5,
        "%",
    )
    assert (blink.kind, blink.command, blink.field) == ("trigger", "blink", None)
    assert (plain.min, plain.max, plain.integer, plain.step, plain.unit) == (
        -1.5,
        1.5,
        False,
        None,
        None,
    )
    assert caplog.text.count("Ignoring a malformed plugin control") == 7


def test_a_toggles_value_is_a_boolean_and_no_options() -> None:
    """True and false are kept as they are, and never pass for the numbers 1 and 0."""
    entry = PluginEntry.from_wire(lamp(power=False, level=1))
    assert (
        entry.controls["power"] is not None and entry.controls["power"].value is False
    )
    assert entry.controls["level"] is not None and entry.controls["level"].value == 1
    assert not same_value(1, True)
    assert not same_value(0, False)
    assert not same_value("true", True)


# ----------------------------------------------------------------------
# The entities
# ----------------------------------------------------------------------


async def test_each_control_is_an_entity_of_its_kind_on_the_workspace_device(
    hass: HomeAssistant,
    with_lamp: None,
    setup_integration: MockConfigEntry,
) -> None:
    """From the first chunk: a switch, a number with its ends, and a button."""
    power = state_of(hass, SWITCH)
    assert power.state == "on"
    assert power.attributes["friendly_name"] == "Home Lamp Power"
    assert power.attributes["icon"] == "mdi:power"
    assert (power.attributes["plugin"], power.attributes["control"]) == (
        "lamp",
        "power",
    )

    level = state_of(hass, NUMBER)
    assert level.state == "40"
    assert (
        level.attributes["min"],
        level.attributes["max"],
        level.attributes["step"],
    ) == (
        0,
        100,
        5,
    )
    assert level.attributes["unit_of_measurement"] == "%"
    assert level.attributes["mode"] == "slider"

    assert state_of(hass, BUTTON).state == STATE_UNKNOWN  # never pressed
    registry = er.async_get(hass)
    workspace = registry.async_get("button.home_stop_session")
    assert workspace is not None
    for kind, entity_id in ENTITIES.items():
        entry = registry.async_get(entity_id)
        assert entry is not None, kind
        control = {"toggle": "power", "range": "level", "trigger": "blink"}[kind]
        assert entry.unique_id == f"{WORKSPACE_ID}:control:lamp/{control}"
        # Meant for a dashboard, like the select.
        assert entry.hidden_by is None
        assert entry.device_id == workspace.device_id


async def test_setting_one_runs_its_command_with_the_value_wanted(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_lamp: None,
    setup_integration: MockConfigEntry,
) -> None:
    """True or false, never "the other"; a whole number for a field of them; nothing for a trigger."""
    server.results["RunPluginCommand"] = {"_tag": "Ok"}
    await call(hass, "switch", "turn_off", SWITCH)
    # Already on, and on is what is sent: a caller that says what it wants can't undo itself.
    await call(hass, "switch", "turn_on", SWITCH)
    await call(hass, "number", "set_value", NUMBER, value=75)
    await call(hass, "button", "press", BUTTON)
    assert server.calls_to("RunPluginCommand") == [
        {"plugin": "lamp", "command": "setPower", "input": {"on": False}},
        {"plugin": "lamp", "command": "setPower", "input": {"on": True}},
        {"plugin": "lamp", "command": "setLevel", "input": {"level": 75}},
        {"plugin": "lamp", "command": "blink", "input": {}},
    ]
    sent = server.calls_to("RunPluginCommand")[2]["input"]["level"]
    assert isinstance(sent, int) and not isinstance(sent, bool)
    # Not optimistic: nothing changed until the plugin published it.
    assert (state_of(hass, SWITCH).state, state_of(hass, NUMBER).state) == ("on", "40")
    await server.push_plugin_values([lamp(power=False, level=75)])
    await wait_for(lambda: state_of(hass, SWITCH).state == "off")
    assert state_of(hass, NUMBER).state == "75"

    # Home Assistant keeps a number to its ends itself, so nothing outside them is sent.
    with pytest.raises(ServiceValidationError):
        await call(hass, "number", "set_value", NUMBER, value=101)
    assert len(server.calls_to("RunPluginCommand")) == 4


async def test_a_range_of_fractions_sends_them_and_steps_by_a_hundredth(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    setup_integration: MockConfigEntry,
) -> None:
    """Without a step: 1 for whole numbers, a hundredth of the range otherwise."""
    warmth = {
        **LEVEL,
        "control": "warmth",
        "title": "Warmth",
        "min": 0,
        "max": 2,
        "integer": False,
    }
    del warmth["step"], warmth["unit"]
    whole = {**LEVEL, "control": "count", "title": "Count"}
    del whole["step"]
    await server.push_plugin_controls([warmth, whole])
    await server.push_plugin_values(
        [
            {
                "plugin": "lamp",
                "status": "running",
                "choices": {},
                "controls": {"warmth": {"value": 0.5}, "count": {"value": 3}},
            }
        ]
    )
    await wait_for(lambda: hass.states.get("number.home_lamp_warmth") is not None)
    await wait_for(lambda: state_of(hass, "number.home_lamp_warmth").state == "0.5")
    assert state_of(hass, "number.home_lamp_warmth").attributes[
        "step"
    ] == pytest.approx(0.02)
    assert (
        "unit_of_measurement"
        not in state_of(hass, "number.home_lamp_warmth").attributes
    )
    assert state_of(hass, "number.home_lamp_count").attributes["step"] == 1
    server.results["RunPluginCommand"] = {"_tag": "Ok"}
    await call(hass, "number", "set_value", "number.home_lamp_warmth", value=1.25)
    # The field is the catalogue entry's, whatever the control is called.
    assert server.calls_to("RunPluginCommand")[-1]["input"] == {"level": 1.25}


async def test_a_refused_call_raises_as_a_plugin_action_does(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_lamp: None,
    setup_integration: MockConfigEntry,
) -> None:
    """The call's fault is a validation error; a plugin that failed is not; the state stays."""
    server.results["RunPluginCommand"] = {
        "_tag": "Failed",
        "code": "invalid",
        "message": "Invalid input for setPower",
    }
    with pytest.raises(ServiceValidationError, match="Invalid input"):
        await call(hass, "switch", "turn_off", SWITCH)
    server.results["RunPluginCommand"] = {
        "_tag": "Failed",
        "code": "unavailable",
        "message": "lamp isn't running",
    }
    for domain, service, entity_id, data in (
        ("switch", "turn_off", SWITCH, {}),
        ("number", "set_value", NUMBER, {"value": 10}),
        ("button", "press", BUTTON, {}),
    ):
        with pytest.raises(HomeAssistantError, match="isn't running") as raised:
            await call(hass, domain, service, entity_id, **data)
        assert not isinstance(raised.value, ServiceValidationError)
    assert (state_of(hass, SWITCH).state, state_of(hass, NUMBER).state) == ("on", "40")


async def test_what_is_unknown_and_what_is_unavailable(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_lamp: None,
    setup_integration: MockConfigEntry,
) -> None:
    """Unavailable only while the plugin is known not to run, the button too."""
    await server.push_plugin_values([lamp(power=None, level=None)])
    await wait_for(lambda: state_of(hass, SWITCH).state == STATE_UNKNOWN)
    assert state_of(hass, NUMBER).state == STATE_UNKNOWN
    assert state_of(hass, BUTTON).state != STATE_UNAVAILABLE

    # A value of another type than the kind's is unknown, not on or 1.
    for power, level in ((1, True), ("on", "40")):
        await server.push_plugin_values([lamp()])
        await wait_for(lambda: state_of(hass, SWITCH).state == "on")
        await server.push_plugin_values([lamp(power=power, level=level)])
        await wait_for(lambda: state_of(hass, SWITCH).state == STATE_UNKNOWN)
        assert state_of(hass, NUMBER).state == STATE_UNKNOWN

    for status in ("starting", "unavailable"):
        await server.push_plugin_values([lamp(status=status)])
        await wait_for(lambda: state_of(hass, BUTTON).state == STATE_UNAVAILABLE)
        assert state_of(hass, SWITCH).state == STATE_UNAVAILABLE
        assert state_of(hass, NUMBER).state == STATE_UNAVAILABLE
        await server.push_plugin_values([lamp()])
        await wait_for(lambda: state_of(hass, SWITCH).state == "on")
        assert state_of(hass, BUTTON).state != STATE_UNAVAILABLE

    # A plugin with no entry is neither running nor not running: available, with nothing known.
    await server.push_plugin_values([])
    await wait_for(lambda: state_of(hass, SWITCH).state == STATE_UNKNOWN)
    assert state_of(hass, NUMBER).state == STATE_UNKNOWN
    assert state_of(hass, BUTTON).state != STATE_UNAVAILABLE


@pytest.mark.parametrize("kind", ["toggle", "range", "trigger"])
async def test_a_control_that_leaves_and_returns_keeps_its_entity(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_lamp: None,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """Each platform runs the reconciler the select does: the registry entry is kept."""
    entity_id = ENTITIES[kind]
    others = [control for control in (POWER, LEVEL, BLINK) if control["kind"] != kind]
    (own,) = (control for control in (POWER, LEVEL, BLINK) if control["kind"] == kind)
    registry = er.async_get(hass)
    registry.async_update_entity(entity_id, name="Mine")
    await hass.async_block_till_done()

    await server.push_plugin_controls(others)
    await wait_for(lambda: state_of(hass, entity_id).attributes.get("restored") is True)
    assert state_of(hass, entity_id).state == STATE_UNAVAILABLE
    # The others stayed as they were.
    for other in others:
        assert "restored" not in state_of(hass, ENTITIES[other["kind"]]).attributes

    await server.push_plugin_controls([*others, own])
    await wait_for(lambda: "restored" not in state_of(hass, entity_id).attributes)
    kept = registry.async_get(entity_id)
    assert kept is not None and kept.name == "Mine"
    # Leaving and returning within one chunk ends with the entity there.
    await server.push(
        {"_tag": "PluginControls", "controls": others},
        {"_tag": "PluginControls", "controls": [*others, own]},
    )
    await hass.async_block_till_done()
    await wait_for(lambda: "restored" not in state_of(hass, entity_id).attributes)
    assert "does not generate unique IDs" not in caplog.text
    assert "ERROR" not in caplog.text


async def test_a_control_that_changes_kind_changes_platform(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_lamp: None,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One name, another kind: one of the new kind takes the control, and nothing is left of the old."""
    registry = er.async_get(hass)
    as_trigger = {
        **BLINK,
        "control": "power",
        "title": "Power",
        "command": "togglePower",
    }
    await server.push_plugin_controls([as_trigger, LEVEL, BLINK])
    await wait_for(lambda: hass.states.get("button.home_lamp_power") is not None)
    # It will not come back as a switch, so its entry there is not kept as one that may.
    await wait_for(lambda: registry.async_get(SWITCH) is None)
    assert hass.states.get(SWITCH) is None
    server.results["RunPluginCommand"] = {"_tag": "Ok"}
    await call(hass, "button", "press", "button.home_lamp_power")
    assert server.calls_to("RunPluginCommand") == [
        {"plugin": "lamp", "command": "togglePower", "input": {}}
    ]
    # The others, and a control that only left, are as they were: an entry kept for its return.
    assert state_of(hass, NUMBER).state == "40"
    await server.push_plugin_controls([as_trigger, BLINK])
    await wait_for(lambda: state_of(hass, NUMBER).attributes.get("restored") is True)
    assert registry.async_get(NUMBER) is not None

    # And back: a switch again, with the button's entry gone in its turn.
    await server.push_plugin_controls([POWER, BLINK])
    await wait_for(lambda: hass.states.get(SWITCH) is not None)
    await wait_for(lambda: registry.async_get("button.home_lamp_power") is None)
    assert state_of(hass, SWITCH).state == "on"
    assert "does not generate unique IDs" not in caplog.text
    assert "ERROR" not in caplog.text


async def test_an_entry_left_by_a_kind_changed_before_a_restart_is_removed(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_lamp: None,
    config_entry: MockConfigEntry,
) -> None:
    """Home Assistant was off while the kind changed: the old platform's entry goes at setup."""
    registry = er.async_get(hass)
    config_entry.add_to_hass(hass)
    unique = f"{WORKSPACE_ID}:control:lamp/power"
    old = registry.async_get_or_create(
        "select", "drift_beacon", unique, config_entry=config_entry
    )
    # Another control's entry on that platform, which the catalogue doesn't list at all, stays.
    gone = registry.async_get_or_create(
        "select",
        "drift_beacon",
        f"{WORKSPACE_ID}:control:lamp/scene",
        config_entry=config_entry,
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await wait_for(lambda: hass.states.get(SWITCH) is not None)
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(gone.entity_id) is not None
    assert registry.async_get(SWITCH) is not None
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
