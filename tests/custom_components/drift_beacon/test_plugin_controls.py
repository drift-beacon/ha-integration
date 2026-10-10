"""Plugin controls as select entities: options, the current value, choosing, and keeping them."""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any

import pytest
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.drift_beacon.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.drift_beacon.models import (
    STATE_LIMIT,
    ControlValue,
    PluginControl,
    PluginEntry,
    PluginOption,
    control_view,
    parse_plugin_controls,
    parse_plugin_values,
    same_value,
    shown_labels,
)
from custom_components.drift_beacon.select import PluginControlSelect

from .conftest import (
    WORKSPACE_ID,
    FakeDriftBeacon,
    entity_id_for,
    make_entry,
    make_snapshot,
    wait_for,
)

SELECT = "select.home_magic_cube_preset"
PRESET = {
    "plugin": "magic-cube",
    "pluginName": "Magic Cube",
    "control": "preset",
    "kind": "select",
    "title": "Preset",
    "icon": "mdi:cube-outline",
    "command": "selectPreset",
    "field": "preset",
    "choices": "preset",
    "nullable": True,
    "none": "No preset",
}
DEEP_WORK = {"value": "p_1", "label": "Deep work", "detail": "duel"}
CHORES = {"value": "p_2", "label": "Chores", "detail": "roulette"}


def values(
    options: list[dict[str, Any]] | None = None,
    value: Any = "p_2",
    *,
    status: str = "running",
    known: bool = True,
    plugin: str = "magic-cube",
) -> dict[str, Any]:
    """One plugin's entry of a PluginValues message; ``known=False`` leaves the value unknown."""
    listed = [DEEP_WORK, CHORES] if options is None else options
    running = status == "running"
    return {
        "plugin": plugin,
        "status": status,
        "choices": {"preset": {"options": listed} if running else None},
        "controls": {"preset": {"value": value} if running and known else None},
    }


@pytest.fixture
def with_preset(server: FakeDriftBeacon) -> None:
    """The server knows the cube's preset control, with two presets and Chores active."""
    server.plugin_controls = [PRESET]
    server.plugin_values = [values()]


def state_of(hass: HomeAssistant, entity_id: str = SELECT) -> State:
    """The entity's state, which must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} has no state"
    return state


async def choose(hass: HomeAssistant, option: str, entity_id: str = SELECT) -> None:
    """Choose an option as a dashboard or an automation does."""
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


# ----------------------------------------------------------------------
# What the two messages say
# ----------------------------------------------------------------------


def test_the_catalogue_keeps_select_controls_and_leaves_out_what_it_cannot_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A kind this integration doesn't know and a malformed entry cost nothing else."""
    controls = parse_plugin_controls(
        [
            PRESET,
            {**PRESET, "control": "speed", "kind": "dial"},
            {**PRESET, "control": "broken", "nullable": "yes"},
            {"kind": "select"},
            "nonsense",
            {
                "plugin": "lamp",
                "control": "scene",
                "kind": "select",
                "command": "setScene",
                "field": "scene",
                "choices": "scenes",
                "nullable": False,
                "none": "Ignored: it isn't nullable",
            },
            # The same control twice: the first is kept.
            {**PRESET, "title": "Again"},
        ]
    )
    assert [control.key for control in controls] == ["magic-cube/preset", "lamp/scene"]
    cube, lamp = controls
    assert (cube.title, cube.icon, cube.nullable, cube.none) == (
        "Preset",
        "mdi:cube-outline",
        True,
        "No preset",
    )
    # What the catalogue leaves out falls back to the ids, and a wording needs a nullable control.
    assert (lamp.plugin_name, lamp.title, lamp.icon, lamp.none) == (
        "lamp",
        "scene",
        None,
        None,
    )
    assert caplog.text.count("Ignoring a malformed plugin control") == 3
    with pytest.raises(TypeError):
        parse_plugin_controls({"controls": []})


def test_values_keep_unknown_empty_and_null_apart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A list or a value that is unknown is None; an empty list and a value of null are known."""
    parsed = parse_plugin_values(
        [
            values(),
            {
                "plugin": "lamp",
                "status": "running",
                "choices": {"scenes": {"options": []}, "moods": None},
                "controls": {
                    "scene": {"value": None},
                    "mood": None,
                    "level": {"value": 3},
                },
            },
            values(status="starting", plugin="fan"),
            {
                "plugin": "broken",
                "status": "running",
                "choices": {"a": {"options": [{}]}},
            },
            {
                "plugin": "worse",
                "status": "running",
                "controls": {"a": {"value": [True]}},
            },
            {"status": "running"},
        ]
    )
    assert list(parsed) == ["magic-cube", "lamp", "fan"]
    cube, lamp, fan = parsed.values()
    assert cube.choices["preset"] == (
        PluginOption("p_1", "Deep work", "duel"),
        PluginOption("p_2", "Chores", "roulette"),
    )
    assert cube.controls["preset"] == ControlValue("p_2")
    assert (lamp.choices["scenes"], lamp.choices["moods"]) == ((), None)
    assert lamp.controls == {
        "scene": ControlValue(None),
        "mood": None,
        "level": ControlValue(3),
    }
    assert (fan.status, fan.choices["preset"], fan.controls["preset"]) == (
        "starting",
        None,
        None,
    )
    assert caplog.text.count("Ignoring a plugin's malformed values") == 3
    with pytest.raises(TypeError):
        parse_plugin_values(None)


def test_a_value_is_compared_with_its_type() -> None:
    """The number 3 and the text "3" are two values; null is only null."""
    assert same_value("p_1", "p_1")
    assert same_value(3, 3)
    assert same_value(3, 3.0)
    assert same_value(None, None)
    assert not same_value(3, "3")
    assert not same_value("3", 3)
    assert not same_value(None, "")
    assert not same_value(0, None)
    assert not same_value("a", "A")


def test_labels_shown_are_unique() -> None:
    """A label two options share is told apart by its detail, and by its value when that fails."""

    def shown(*options: tuple[Any, str, str | None], reserved: str | None = None):
        return shown_labels(
            tuple(
                PluginOption(value, label, detail) for value, label, detail in options
            ),
            reserved,
        )

    assert shown(("p_1", "Deep work", "duel"), ("p_2", "Chores", None)) == (
        "Deep work",
        "Chores",
    )
    # By detail, only for the ones that need it.
    assert shown(
        ("p_1", "Focus", "duel"),
        ("p_2", "Focus", "roulette"),
        ("p_3", "Chores", "duel"),
    ) == ("Focus (duel)", "Focus (roulette)", "Chores")
    # The details are the same, or missing: by value.
    assert shown(("p_1", "Focus", "duel"), ("p_2", "Focus", "duel")) == (
        "Focus (p_1)",
        "Focus (p_2)",
    )
    assert shown(
        ("p_1", "Focus", None), (2, "Focus", "duel"), ("p_3", "Focus", "duel")
    ) == (
        "Focus (p_1)",
        "Focus (2)",
        "Focus (p_3)",
    )
    # An option that reads like the wording for null, and one that reads like another's way out.
    assert shown(
        ("p_1", "No preset", None), ("p_2", "Chores", None), reserved="No preset"
    ) == ("No preset (p_1)", "Chores")
    assert shown(
        ("a", "Focus (b)", None), ("b", "Focus", None), ("c", "Focus", None)
    ) == (
        "Focus (b)",
        "Focus (b) (b)",
        "Focus (c)",
    )
    for case in (
        (("p_1", "Focus", "x"), ("p_2", "Focus", "x"), ("p_3", "Focus (x)", None)),
        (("1", "A", None), (1, "A", None), ("A (1)", "A", None)),
    ):
        assert len(set(shown(*case, reserved="A"))) == len(case)
    # An option without a label is shown as its value; a label too long for a state is cut,
    # and two that are the same once cut are still told apart.
    assert shown(("p_1", "", None), ("p_2", "", "duel"), (3, "", None)) == (
        "p_1",
        "p_2",
        "3",
    )
    assert shown(("p_1", "", None), ("p_2", "p_1", None)) == ("p_1 (p_1)", "p_1")
    long = "x" * 400
    cut = shown(("p_1", long, None), ("p_2", long + "y", None), ("p_3", "Chores", None))
    assert all(name is not None and len(name) <= STATE_LIMIT for name in cut)
    assert len(set(cut)) == 3 and cut[2] == "Chores"
    assert shown() == ()


def test_no_option_is_longer_than_a_state_may_be() -> None:
    """Whatever is added to a label to tell it apart, the whole stays within the limit."""

    def shown(*options: tuple[Any, str, str | None], reserved: str | None = None):
        return shown_labels(
            tuple(
                PluginOption(value, label, detail) for value, label, detail in options
            ),
            reserved,
        )

    def fits(names: tuple[str | None, ...]) -> bool:
        offered = [name for name in names if name is not None]
        return len(set(offered)) == len(offered) and all(
            len(name) <= STATE_LIMIT for name in offered
        )

    # A long detail is cut to the room its label leaves, and still tells the two apart.
    by_detail = shown(("p_1", "Focus", "d" * 300), ("p_2", "Focus", "e" * 300))
    assert fits(by_detail) and None not in by_detail
    assert by_detail[0] == f"Focus ({'d' * 246}…)" and len(by_detail[0]) == STATE_LIMIT
    # Details that are the same once cut: by value, like any that are the same.
    same_cut = shown(("p_1", "Focus", "d" * 300), ("p_2", "Focus", "d" * 300 + "e"))
    assert same_cut == ("Focus (p_1)", "Focus (p_2)")
    # A value is never cut: the label gives way to it.
    value = "v" * 200
    by_value = shown((value, "x" * 400, None), ("p_2", "x" * 400, None))
    assert fits(by_value) and None not in by_value
    assert by_value[0] == f"{'x' * 51}… ({value})" and len(by_value[0]) == STATE_LIMIT
    assert by_value[1] == f"{'x' * 159}… (p_2)"
    # A value that leaves no room is not offered, with a label or without, and costs the
    # others nothing.
    huge = "v" * 300
    assert shown(
        (huge, "Focus", None), ("p_2", "Focus", None), ("p_3", "Chores", None)
    ) == (
        None,
        "Focus (p_2)",
        "Chores",
    )
    assert shown((huge, "", None), ("v" * STATE_LIMIT, "", None)) == (
        None,
        "v" * STATE_LIMIT,
    )
    # The value added again and again, for labels that read like each other's way out, ends.
    crowded = shown(
        *((f"{'Focus' + ' (b)' * n}", "Focus", None) for n in range(4)),
        ("b", "Focus", None),
        *((n, "Focus" + " (b)" * n, None) for n in range(1, 70)),
    )
    assert fits(crowded)


def test_a_control_never_shows_an_option_too_long_to_be_its_state() -> None:
    """The wording for null is cut too, and a value whose option isn't offered is unknown."""
    (control,) = parse_plugin_controls([{**PRESET, "none": "n" * 300}])
    huge = "v" * 300
    entry = PluginEntry.from_wire(
        values(
            options=[
                {"value": huge, "label": "Focus"},
                {"value": "p_2", "label": "Focus"},
            ],
            value=huge,
        )
    )
    view = control_view(control, entry, "None")
    assert view.names == ["Focus (p_2)", f"{'n' * 254}…"]
    assert all(len(name) <= STATE_LIMIT for name in view.names)
    assert view.current is None and view.value is not None and view.value.value == huge


def test_a_control_resolves_against_its_plugins_values() -> None:
    """Options, the current one and availability, for each thing that can be unknown."""
    (control,) = parse_plugin_controls([PRESET])

    def view(entry: dict[str, Any] | None, of: PluginControl = control):
        parsed = None if entry is None else PluginEntry.from_wire(entry)
        return control_view(of, parsed, "None")

    known = view(values())
    assert known.available
    assert known.options == (
        ("Deep work", "p_1"),
        ("Chores", "p_2"),
        ("No preset", None),
    )
    assert (known.current, known.value) == ("Chores", ControlValue("p_2"))
    assert view(values(value=None)).current == "No preset"
    # A value the list doesn't hold, and one of another type: nothing is current.
    assert view(values(value="p_9")).current is None
    assert view(values([{"value": 2, "label": "Two"}], "2")).current is None
    assert view(values([{"value": 2, "label": "Two"}], 2)).current == "Two"
    # Unknown: the value, the list, the plugin.
    unpublished = view(values(known=False))
    assert (unpublished.available, unpublished.current, unpublished.value) == (
        True,
        None,
        None,
    )
    # With the list unknown, the wording for null is the one option: clearing needs no list.
    no_list = view({**values(), "choices": {"preset": None}})
    assert (no_list.available, no_list.names, no_list.current) == (
        True,
        ["No preset"],
        None,
    )
    cleared = view({**values(value=None), "choices": {"preset": None}})
    assert (cleared.names, cleared.current) == (["No preset"], "No preset")
    nothing = view(None)
    assert (nothing.available, nothing.names, nothing.current) == (
        True,
        ["No preset"],
        None,
    )
    empty = view(values([], None))
    assert (empty.options, empty.current) == ((("No preset", None),), "No preset")
    for status in ("starting", "unavailable", "something-new"):
        assert not view(values(status=status)).available

    # Not nullable: null isn't offered, and a value of null selects nothing.
    (strict,) = parse_plugin_controls([{**PRESET, "nullable": False}])
    assert view(values(), strict).names == ["Deep work", "Chores"]
    assert view(None, strict).options == ()
    assert view(values(value=None), strict).current is None
    # Nullable without the plugin's wording: the integration's own.
    (unworded,) = parse_plugin_controls([{**PRESET, "none": None}])
    assert view(values(value=None), unworded).current == "None"
    assert view(values(value=None), unworded).names[-1] == "None"


# ----------------------------------------------------------------------
# The entity
# ----------------------------------------------------------------------


def test_a_select_over_a_fixed_set_reads_like_any_other() -> None:
    """The hub tells a field's declared options as a list of the control's own; nothing differs."""
    wire = {
        "plugin": "tp-contract",
        "pluginName": "TP Contract",
        "control": "hue",
        "kind": "select",
        "title": "Hue",
        "icon": "mdi:palette",
        "command": "setTone",
        "field": "to",
        # A name no manifest can declare: it is only ever a key to look the options up by.
        "choices": "control:hue",
        "nullable": True,
        "none": "No tone",
    }
    (control,) = parse_plugin_controls([wire])
    entry = PluginEntry.from_wire(
        {
            "plugin": "tp-contract",
            "status": "running",
            "choices": {
                "control:hue": {
                    "options": [
                        {"value": "warm", "label": "Warm", "detail": "Amber"},
                        {"value": "cool", "label": "Cool"},
                    ]
                }
            },
            "controls": {"hue": {"value": "cool"}},
        }
    )
    view = control_view(control, entry, "None")
    assert view.available
    assert view.options == (("Warm", "warm"), ("Cool", "cool"), ("No tone", None))
    assert (view.current, view.value) == ("Cool", ControlValue("cool"))


async def test_a_control_is_a_select_on_the_workspace_device(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """From the first chunk: its options, the current one, its value, and where it lives."""
    state = state_of(hass)
    assert state.state == "Chores"
    assert state.attributes[ATTR_OPTIONS] == ["Deep work", "Chores", "No preset"]
    assert state.attributes["value"] == "p_2"
    assert (state.attributes["plugin"], state.attributes["control"]) == (
        "magic-cube",
        "preset",
    )
    assert state.attributes["friendly_name"] == "Home Magic Cube Preset"
    assert state.attributes["icon"] == "mdi:cube-outline"
    registry = er.async_get(hass)
    entry = registry.async_get(SELECT)
    assert entry is not None
    assert entry.unique_id == f"{WORKSPACE_ID}:control:magic-cube/preset"
    # Unlike the activities' entities, a control's is meant for a dashboard.
    assert entry.hidden_by is None
    workspace = registry.async_get("button.home_stop_session")
    assert workspace is not None and entry.device_id == workspace.device_id
    assert server.calls_to("Subscribe") == [
        {"pluginCommands": True, "scheduleEvents": True, "pluginControls": True}
    ]


async def test_choosing_an_option_runs_the_controls_command_with_its_value(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """The value travels, with its type; the state follows the server, not the choice."""
    server.results["RunPluginCommand"] = {"_tag": "Ok"}
    await choose(hass, "Deep work")
    await choose(hass, "No preset")
    assert server.calls_to("RunPluginCommand") == [
        {"plugin": "magic-cube", "command": "selectPreset", "input": {"preset": "p_1"}},
        {"plugin": "magic-cube", "command": "selectPreset", "input": {"preset": None}},
    ]
    # Not optimistic: nothing changed until the plugin published it.
    assert state_of(hass).state == "Chores"
    await server.push_plugin_values([values(value="p_1")])
    await wait_for(lambda: state_of(hass).state == "Deep work")
    await server.push_plugin_values([values(value=None)])
    await wait_for(lambda: state_of(hass).state == "No preset")
    assert state_of(hass).attributes["value"] is None

    # A list of numbers sends the number, and two options with one label are told apart.
    await server.push_plugin_values(
        [
            values(
                [
                    {"value": 1, "label": "Side", "detail": "work"},
                    {"value": 2, "label": "Side", "detail": "rest"},
                ],
                2,
            )
        ]
    )
    await wait_for(lambda: state_of(hass).state == "Side (rest)")
    assert state_of(hass).attributes[ATTR_OPTIONS] == [
        "Side (work)",
        "Side (rest)",
        "No preset",
    ]
    await choose(hass, "Side (work)")
    assert server.calls_to("RunPluginCommand")[-1]["input"] == {"preset": 1}

    with pytest.raises(ServiceValidationError):
        await choose(hass, "Chores")
    assert len(server.calls_to("RunPluginCommand")) == 3


async def test_a_refused_choice_raises_as_a_plugin_action_does(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """The call's fault is a validation error; a plugin that failed is not."""
    server.results["RunPluginCommand"] = {
        "_tag": "Failed",
        "code": "not-found",
        "message": 'No preset "p_1"',
    }
    with pytest.raises(ServiceValidationError, match='No preset "p_1"'):
        await choose(hass, "Deep work")
    server.results["RunPluginCommand"] = {
        "_tag": "Failed",
        "code": "unavailable",
        "message": "magic-cube isn't running",
    }
    with pytest.raises(HomeAssistantError, match="isn't running") as raised:
        await choose(hass, "Deep work")
    assert not isinstance(raised.value, ServiceValidationError)
    assert state_of(hass).state == "Chores"


async def test_what_is_unknown_and_what_is_unavailable(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """Unavailable only while the plugin is known not to run; everything else is unknown."""
    await server.push_plugin_values([values(status="starting")])
    await wait_for(lambda: state_of(hass).state == STATE_UNAVAILABLE)
    await server.push_plugin_values([values(status="unavailable")])
    await server.push_plugin_values([values(known=False)])
    await wait_for(lambda: state_of(hass).state == STATE_UNKNOWN)
    state = state_of(hass)
    assert state.attributes[ATTR_OPTIONS] == ["Deep work", "Chores", "No preset"]
    assert "value" not in state.attributes

    # A value the list no longer holds: still the control's value, and no option is current.
    await server.push_plugin_values([values([DEEP_WORK], "p_2")])
    await wait_for(
        lambda: state_of(hass).attributes[ATTR_OPTIONS][0:2] != ["Deep work", "Chores"]
    )
    assert (state_of(hass).state, state_of(hass).attributes["value"]) == (
        STATE_UNKNOWN,
        "p_2",
    )

    # No entry for the plugin: neither running nor not, so it stays available with nothing known.
    await server.push_plugin_values([values()])
    await wait_for(lambda: state_of(hass).state == "Chores")
    await server.push_plugin_values([])
    await wait_for(lambda: state_of(hass).state == STATE_UNKNOWN)
    assert state_of(hass).attributes[ATTR_OPTIONS] == ["No preset"]

    # A value of null on a control that doesn't offer null selects nothing.
    await server.push_plugin_controls([{**PRESET, "nullable": False, "none": None}])
    await server.push_plugin_values([values(value=None)])
    await wait_for(lambda: state_of(hass).attributes.get("value", 0) is None)
    assert state_of(hass).state == STATE_UNKNOWN
    assert state_of(hass).attributes[ATTR_OPTIONS] == ["Deep work", "Chores"]


async def test_state_is_written_only_when_something_of_the_control_changed(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same values again, another plugin's values and an activity's change write nothing."""
    # Counted where the entity writes: Home Assistant itself drops a write that changes nothing.
    writes: list[str] = []
    write = PluginControlSelect.async_write_ha_state

    def counted(self: PluginControlSelect) -> None:
        writes.append(self.entity_id)
        write(self)

    monkeypatch.setattr(PluginControlSelect, "async_write_ha_state", counted)
    coordinator = setup_integration.runtime_data
    await server.push_plugin_values([values()])
    await server.push_plugin_values([values(), values(plugin="lamp", value="p_1")])
    await server.push_plugin_controls([PRESET])
    await server.push({"_tag": "ActivityDeleted", "activityId": "water"})
    await wait_for(lambda: coordinator.data.activity("water") is None)
    await hass.async_block_till_done()
    assert writes == []
    assert "lamp" in coordinator.data.plugin_values

    await server.push_plugin_values([values(value="p_1")])
    await wait_for(lambda: state_of(hass).state == "Deep work")
    await hass.async_block_till_done()
    assert writes == [SELECT]


async def test_a_control_that_leaves_and_returns_keeps_its_entity(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """Gone from Home Assistant while its plugin is disabled, with its registry entry kept."""
    registry = er.async_get(hass)
    registry.async_update_entity(
        SELECT, name="Cube preset", new_entity_id="select.cube"
    )
    await hass.async_block_till_done()
    assert state_of(hass, "select.cube").state == "Chores"

    await server.push_plugin_controls([])
    await server.push_plugin_values([])
    # Home Assistant shows an entity it kept in the registry as restored and unavailable: no
    # longer provided, and the user's to delete.
    await wait_for(
        lambda: state_of(hass, "select.cube").attributes.get("restored") is True
    )
    assert state_of(hass, "select.cube").state == STATE_UNAVAILABLE
    kept = registry.async_get("select.cube")
    assert kept is not None and kept.name == "Cube preset"

    await server.push_plugin_controls([{**PRESET, "title": "Active preset"}])
    await server.push_plugin_values([values(value="p_1")])
    await wait_for(lambda: state_of(hass, "select.cube").state == "Deep work")
    assert "restored" not in state_of(hass, "select.cube").attributes
    assert entity_id_for(
        hass, "select", f"{WORKSPACE_ID}:control:magic-cube/preset"
    ) == ("select.cube")
    # Leaving and returning within one chunk ends with the entity there.
    await server.push(
        {"_tag": "PluginControls", "controls": []},
        {"_tag": "PluginControls", "controls": [PRESET]},
    )
    await wait_for(lambda: len(server.subscribers) == 1)
    await hass.async_block_till_done()
    await wait_for(lambda: state_of(hass, "select.cube").state == "Deep work")
    assert "restored" not in state_of(hass, "select.cube").attributes


async def test_a_control_that_leaves_and_returns_at_once(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chunks read in one turn of the loop, with nothing awaited between them."""
    coordinator = setup_integration.runtime_data
    gone = {"_tag": "PluginControls", "controls": []}
    back = {"_tag": "PluginControls", "controls": [PRESET]}
    changes: list[tuple[str, Any]] = []
    hass.bus.async_listen(
        "state_changed",
        lambda event: (
            changes.append(
                (
                    event.data["new_state"].state,
                    event.data["new_state"].attributes.get("restored"),
                )
            )
            if event.data["entity_id"] == SELECT and event.data["new_state"]
            else None
        ),
    )

    # Its plugin's values go with it, in one chunk: it says nothing of them on its way out.
    coordinator._apply_chunk([gone, {"_tag": "PluginValues", "plugins": []}])
    await hass.async_block_till_done()
    assert changes == [(STATE_UNAVAILABLE, True)]

    coordinator._apply_chunk([back])
    coordinator._apply_chunk([{"_tag": "PluginValues", "plugins": [values()]}])
    coordinator._apply_chunk([gone])
    coordinator._apply_chunk([back])
    await hass.async_block_till_done()
    assert state_of(hass).state == "Chores"
    assert "restored" not in state_of(hass).attributes

    coordinator._apply_chunk([gone])
    coordinator._apply_chunk([back])
    coordinator._apply_chunk([gone])
    await hass.async_block_till_done()
    assert state_of(hass).attributes.get("restored") is True
    coordinator._apply_chunk([back])
    await hass.async_block_till_done()

    # And still the one that follows the server.
    await server.push_plugin_values([values(value="p_1")])
    await wait_for(lambda: state_of(hass).state == "Deep work")
    assert "does not generate unique IDs" not in caplog.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_a_control_that_returns_waits_for_its_removal(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A removal that takes a while: the entity is added once it has gone, and not after an unload."""
    coordinator = setup_integration.runtime_data
    release = asyncio.Event()

    async def slow(self: PluginControlSelect) -> None:
        await release.wait()

    monkeypatch.setattr(PluginControlSelect, "async_will_remove_from_hass", slow)
    gone = {"_tag": "PluginControls", "controls": []}
    back = {"_tag": "PluginControls", "controls": [PRESET]}

    coordinator._apply_chunk([gone])
    coordinator._apply_chunk([back])
    coordinator._apply_chunk(
        [{"_tag": "PluginValues", "plugins": [values(value="p_1")]}]
    )
    await asyncio.sleep(0)
    # Still the one on its way out: nothing was added beside it.
    assert "does not generate unique IDs" not in caplog.text
    release.set()
    await hass.async_block_till_done()
    assert state_of(hass).state == "Deep work"
    assert "restored" not in state_of(hass).attributes

    # The same, with the entry unloaded before the removal ends: nothing is added to a platform
    # that is gone, and the next setup owns the entity.
    release.clear()
    coordinator._apply_chunk([gone])
    coordinator._apply_chunk([back])
    unload = hass.async_create_task(
        hass.config_entries.async_unload(setup_integration.entry_id)
    )
    await asyncio.sleep(0)
    release.set()
    assert await unload
    await hass.async_block_till_done()
    assert state_of(hass).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_setup(setup_integration.entry_id)
    await hass.async_block_till_done()
    await server.push_plugin_values([values(value="p_2")])
    await wait_for(lambda: state_of(hass).state == "Chores")
    assert "does not generate unique IDs" not in caplog.text


async def test_an_entry_unloaded_as_a_control_returns_starts_clean(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing is added to a platform that is gone, so the next setup owns the entity."""
    coordinator = setup_integration.runtime_data
    coordinator._apply_chunk([{"_tag": "PluginControls", "controls": []}])
    coordinator._apply_chunk([{"_tag": "PluginControls", "controls": [PRESET]}])
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass).state == STATE_UNAVAILABLE

    assert await hass.config_entries.async_setup(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass).state == "Chores"
    await server.push_plugin_values([values(value="p_1")])
    await wait_for(lambda: state_of(hass).state == "Deep work")
    assert "does not generate unique IDs" not in caplog.text


async def test_a_select_the_user_disabled_leaves_quietly(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant never added it, so there is nothing to remove when its plugin goes."""
    registry = er.async_get(hass)
    registry.async_update_entity(SELECT, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SELECT) is None

    await server.push_plugin_controls([])
    await server.push_plugin_controls([PRESET])
    await server.push_plugin_controls([])
    await wait_for(lambda: setup_integration.runtime_data.data.plugin_controls == ())
    await hass.async_block_till_done()
    assert registry.async_get(SELECT) is not None
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert "exception" not in caplog.text.lower()


async def test_a_value_or_an_icon_alone_is_a_change(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """Neither shows in the option, and each is still written."""
    await server.push_plugin_values([values(value="p_8")])
    await wait_for(lambda: state_of(hass).attributes.get("value") == "p_8")
    assert state_of(hass).state == STATE_UNKNOWN
    await server.push_plugin_values([values(value="p_9")])
    await wait_for(lambda: state_of(hass).attributes.get("value") == "p_9")
    assert state_of(hass).state == STATE_UNKNOWN

    await server.push_plugin_controls([{**PRESET, "icon": "mdi:dice-5"}])
    await wait_for(lambda: state_of(hass).attributes["icon"] == "mdi:dice-5")


async def test_a_new_control_and_a_renamed_one(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """A control added later gets its entity; a new title renames the one there."""
    scene = {
        **PRESET,
        "plugin": "lamp",
        "pluginName": "Lamp",
        "control": "scene",
        "title": "Lamp scene",
        "icon": None,
        "nullable": False,
        "none": None,
    }
    await server.push_plugin_controls([PRESET, scene])
    await wait_for(lambda: hass.states.get("select.home_lamp_scene") is not None)
    lamp = state_of(hass, "select.home_lamp_scene")
    # Nothing is known of the lamp yet, and its title already names it.
    assert (lamp.state, lamp.attributes[ATTR_OPTIONS]) == (STATE_UNKNOWN, [])
    assert lamp.attributes["friendly_name"] == "Home Lamp scene"
    assert "icon" not in lamp.attributes

    await server.push_plugin_controls([{**PRESET, "title": "Mode"}, scene])
    await wait_for(
        lambda: state_of(hass).attributes["friendly_name"] == "Home Magic Cube Mode"
    )
    assert state_of(hass).state == "Chores"


async def test_no_control_until_the_server_knows_them(
    hass: HomeAssistant, server: FakeDriftBeacon, setup_integration: MockConfigEntry
) -> None:
    """A server that hasn't decided the plugins sends neither message, and nothing is made."""
    assert hass.states.async_entity_ids("select") == []
    assert setup_integration.runtime_data.data.plugin_controls is None
    # Values before any catalogue are kept, and wait for it.
    await server.push_plugin_values([values()])
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("select") == []
    await server.push_plugin_controls([PRESET])
    await wait_for(lambda: hass.states.get(SELECT) is not None)
    assert state_of(hass).state == "Chores"


async def test_a_reconnect_keeps_what_was_held_until_the_server_says_otherwise(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """A Snapshot drops neither message; the next of each replaces it."""
    coordinator = setup_integration.runtime_data
    # The server restarted and doesn't know the plugins yet.
    server.plugin_controls = server.plugin_values = None
    server.snapshot = make_snapshot(workspaceName="Renamed")
    await server.disconnect()
    await wait_for(lambda: coordinator.data.workspace_name == "Renamed")
    await hass.async_block_till_done()
    assert state_of(hass).state == "Chores"
    assert state_of(hass).attributes[ATTR_OPTIONS] == [
        "Deep work",
        "Chores",
        "No preset",
    ]

    await server.push_plugin_controls([PRESET])
    await server.push_plugin_values([values(value="p_1")])
    await wait_for(lambda: state_of(hass).state == "Deep work")

    # While it is away past the grace, the entity is unavailable like every other.
    server.plugin_controls, server.plugin_values = [PRESET], [values(value="p_1")]
    coordinator.async_set_update_error(RuntimeError("gone"))
    assert state_of(hass).state == STATE_UNAVAILABLE


async def test_each_workspace_has_its_own_options(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Two entries, two selects of one control, each with its workspace's list and command."""
    office = FakeDriftBeacon()
    office.snapshot = make_snapshot(workspaceId="ws-2", workspaceName="Office")
    await office.start()
    try:
        server.plugin_controls = office.plugin_controls = [PRESET]
        server.plugin_values = [values()]
        office.plugin_values = [values([{"value": "o_1", "label": "Review"}], "o_1")]
        for fake in (server, office):
            fake.results["RunPluginCommand"] = {"_tag": "Ok"}
        home, work = make_entry(server), make_entry(office, "ws-2", "Office")
        for entry in (home, work):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        there = "select.office_magic_cube_preset"
        assert state_of(hass).state == "Chores"
        assert state_of(hass, there).state == "Review"
        assert state_of(hass, there).attributes[ATTR_OPTIONS] == ["Review", "No preset"]
        await choose(hass, "Review", there)
        assert office.calls_to("RunPluginCommand") == [
            {
                "plugin": "magic-cube",
                "command": "selectPreset",
                "input": {"preset": "o_1"},
            }
        ]
        assert server.calls_to("RunPluginCommand") == []
        with pytest.raises(ServiceValidationError):
            await choose(hass, "Review")
    finally:
        for entry in hass.config_entries.async_entries("drift_beacon"):
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await office.stop()


async def test_a_malformed_message_costs_nothing(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the connection nor what was held."""
    await server.push({"_tag": "PluginValues", "plugins": "nope"})
    await server.push({"_tag": "PluginControls"})
    await server.push({"_tag": "PluginValues", "plugins": [values(value="p_1")]})
    await wait_for(lambda: state_of(hass).state == "Deep work")
    assert "Ignoring malformed PluginValues message" in caplog.text
    assert "Ignoring malformed PluginControls message" in caplog.text
    assert server.connections == 1


async def test_diagnostics_name_controls_and_statuses_but_no_labels_or_values(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_preset: None,
    setup_integration: MockConfigEntry,
) -> None:
    """Which controls there are and whether their plugins run; never what the user named."""
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert diagnostics["state"]["plugin_controls"] == ["magic-cube/preset"]
    assert diagnostics["state"]["plugin_values"] == {
        "magic-cube": {"status": "running", "lists": {"preset": 2}, "controls_known": 1}
    }
    text = str(diagnostics)
    for private in ("Deep work", "Chores", "p_1", "p_2"):
        assert private not in text


def test_the_values_held_are_read_only() -> None:
    """What a message said can't be changed in place by whoever reads it."""
    parsed = parse_plugin_values([values()])
    assert isinstance(parsed, MappingProxyType)
    with pytest.raises(TypeError):
        parsed["lamp"] = parsed["magic-cube"]  # type: ignore[index]
