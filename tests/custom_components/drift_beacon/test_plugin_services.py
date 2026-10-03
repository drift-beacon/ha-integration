"""Plugin commands as service actions: registration, calls, errors, and keeping them stable."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED
from homeassistant.core import Event, HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.service import async_get_cached_service_description
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.drift_beacon.const import DOMAIN
from custom_components.drift_beacon.plugin_services import (
    STORAGE_KEY,
    PluginCommand,
    command_input,
    describe_fields,
    service_name,
)

from .conftest import (
    WORKSPACE_ID,
    FakeDriftBeacon,
    device_for,
    live_session,
    make_entry,
    make_snapshot,
    wait_for,
)

SELECT_PRESET: dict[str, Any] = {
    "plugin": "magic-cube",
    "pluginName": "Magic Cube",
    "command": "selectPreset",
    "title": "Select preset",
    "description": "Switch the cube to a saved preset",
    "input": {
        "type": "object",
        "properties": {
            "preset": {"type": ["string", "null"], "description": "Its name or id"}
        },
        "required": ["preset"],
    },
    "output": {
        "type": "object",
        "properties": {"activePresetId": {"type": ["string", "null"]}},
    },
}
RESET: dict[str, Any] = {
    "plugin": "magic-cube",
    "pluginName": "Magic Cube",
    "command": "reset",
    "title": "Reset",
}
SELECT = "magic_cube_select_preset"
RESET_SERVICE = "magic_cube_reset"


@pytest.fixture
def with_commands(server: FakeDriftBeacon) -> None:
    """The server offers magic-cube's commands (list it before setup_integration)."""
    server.plugin_commands = [SELECT_PRESET, RESET]


def workspace_device(hass: HomeAssistant, workspace_id: str = WORKSPACE_ID) -> str:
    device = device_for(hass, workspace_id)
    assert device is not None
    return device.id


async def call(
    hass: HomeAssistant, service: str, data: dict[str, Any], *, response: bool = False
) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=response
    )


# ----------------------------------------------------------------------
# Names and fields
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plugin", "command", "name"),
    [
        ("magic-cube", "selectPreset", "magic_cube_select_preset"),
        ("cube", "reset", "cube_reset"),
        ("hue2", "setRgb", "hue2_set_rgb"),
        ("a--b-", "go", "a_b_go"),
        ("9lives", "nap", "9lives_nap"),
    ],
)
def test_service_names(plugin: str, command: str, name: str) -> None:
    """A service is named after the plugin's manifest id and the command, in snake case."""
    assert service_name(plugin, command) == name
    assert cv.service(f"{DOMAIN}.{name}")


@pytest.mark.parametrize(
    ("plugin", "command"), [("cube", "select-preset"), ("@@", "go")]
)
def test_no_name_scripts_couldnt_call(plugin: str, command: str) -> None:
    """A name Home Assistant's scripts would refuse gives no service."""
    assert service_name(plugin, command) is None


@pytest.mark.parametrize(
    ("kind", "value", "sent"),
    [
        (["string", "null"], 5, "5"),
        (["string", "null"], 1.5, "1.5"),
        (["string", "null"], True, "true"),
        (["string", "number"], 5, 5),
        (["string", "boolean"], True, True),
        ("integer", 5, 5),
        ("string", "Focus", "Focus"),
    ],
)
def test_text_fields_get_text(kind: Any, value: Any, sent: Any) -> None:
    """A number or boolean for a field that takes only text is sent as text; targets aren't input."""
    command = PluginCommand.from_wire(
        {
            "plugin": "cube",
            "command": "go",
            "input": {"type": "object", "properties": {"field": {"type": kind}}},
        }
    )
    data = {"field": value, "device_id": ["d1"], "other": 1}
    assert command_input(data, command) == {"field": sent, "other": 1}


def test_fields_get_selectors_home_assistant_accepts() -> None:
    """Each input property becomes a field with a selector that fits its schema."""
    fields = describe_fields(
        {
            "type": "object",
            "properties": {
                "preset": {"type": ["string", "null"], "title": "Preset"},
                "color": {"type": "string", "enum": ["red", "blue", None]},
                "level": {"type": "integer", "minimum": 0, "maximum": 10},
                "count": {"type": "integer", "default": 1},
                "ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "on": {"type": "boolean"},
                "note": {"type": "string", "description": "Anything"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "mixed": {"type": ["string", "number"]},
            },
            "required": ["preset", "level"],
        }
    )
    assert fields == {
        "preset": {"name": "Preset", "required": True, "selector": {"text": {}}},
        "color": {
            "name": "color",
            "required": False,
            "selector": {"select": {"options": ["red", "blue"]}},
        },
        "level": {
            "name": "level",
            "required": True,
            "selector": {"number": {"min": 0, "max": 10, "step": 1, "mode": "slider"}},
        },
        "count": {
            "name": "count",
            "required": False,
            "default": 1,
            "selector": {"number": {"step": 1, "mode": "box"}},
        },
        "ratio": {
            "name": "ratio",
            "required": False,
            "selector": {"number": {"min": 0, "max": 1, "step": "any", "mode": "box"}},
        },
        "on": {"name": "on", "required": False, "selector": {"boolean": {}}},
        "note": {
            "name": "note",
            "description": "Anything",
            "required": False,
            "selector": {"text": {}},
        },
        "tags": {"name": "tags", "required": False, "selector": {"object": {}}},
        "mixed": {"name": "mixed", "required": False, "selector": {"object": {}}},
    }
    for field in fields.values():
        selector.validate_selector(field["selector"])
    assert describe_fields(None) == {}


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


async def test_services_come_with_the_first_snapshot(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """The integration asks for plugin commands and registers them while the entry sets up."""
    assert server.calls_to("Subscribe") == [
        {"pluginCommands": True, "scheduleEvents": True}
    ]
    assert hass.services.has_service(DOMAIN, SELECT)
    assert hass.services.supports_response(DOMAIN, SELECT) is SupportsResponse.OPTIONAL
    assert hass.services.supports_response(DOMAIN, RESET_SERVICE) is (
        SupportsResponse.NONE
    )
    assert async_get_cached_service_description(hass, DOMAIN, SELECT) == {
        "name": "Magic Cube: Select preset",
        "description": "Switch the cube to a saved preset",
        "fields": {
            "preset": {
                "name": "preset",
                "description": "Its name or id",
                "required": True,
                "selector": {"text": {}},
            }
        },
        "target": {"device": [{"integration": DOMAIN, "model": "Workspace"}]},
        "response": {"optional": True},
    }


async def test_a_server_without_plugin_commands_adds_none(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    setup_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """An older server sends no list: that's not an empty one, so nothing is stored."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage
    assert not hass.services.has_service(DOMAIN, SELECT)


async def test_the_lists_are_stored(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Each workspace's list is stored until its entry is removed."""

    async def saved() -> Any:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
        await hass.async_block_till_done()
        return hass_storage[STORAGE_KEY]["data"]

    # An entry that went while HA was saving: forgotten when the lists are read.
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"entries": {"gone": [RESET]}},
    }
    server.plugin_commands = [SELECT_PRESET, RESET]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await saved() == {"entries": {config_entry.entry_id: [SELECT_PRESET, RESET]}}
    await hass.config_entries.async_remove(config_entry.entry_id)
    assert await saved() == {"entries": {}}


async def test_only_what_changed_is_registered_again(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """A new list re-registers only the services it changes; a reload changes none."""
    changes: list[tuple[str, str]] = []

    @callback
    def record(event: Event) -> None:
        changes.append((event.event_type, event.data["service"]))

    for event_type in (EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED):
        hass.bus.async_listen(event_type, record)
    await server.push_plugin_commands([{**SELECT_PRESET, "title": "Choose"}, RESET])
    await wait_for(lambda: bool(changes))
    await hass.async_block_till_done()
    assert changes == [(EVENT_SERVICE_REGISTERED, SELECT)]
    changes.clear()
    assert await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert changes == []


async def test_services_follow_the_list(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """A new list adds, updates and removes services."""
    renamed = {**SELECT_PRESET, "title": "Choose preset", "output": None}
    await server.push_plugin_commands([renamed])
    await wait_for(lambda: not hass.services.has_service(DOMAIN, RESET_SERVICE))
    description = async_get_cached_service_description(hass, DOMAIN, SELECT)
    assert description is not None
    assert description["name"] == "Magic Cube: Choose preset"
    assert "response" not in description
    assert hass.services.supports_response(DOMAIN, SELECT) is SupportsResponse.NONE


async def test_reconnecting_changes_no_services(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """The same list after a reconnect registers and removes nothing."""
    changes: list[str] = []
    for event in (EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED):
        hass.bus.async_listen(event, lambda e: changes.append(e.event_type))
    coordinator = setup_integration.runtime_data
    server.snapshot = make_snapshot(liveSessions=[live_session("s1", "reading")])
    await server.disconnect()
    await wait_for(lambda: coordinator.data.live_session is not None)
    await hass.async_block_till_done()
    assert server.connections == 2
    assert changes == []
    assert hass.services.has_service(DOMAIN, SELECT)


async def test_a_server_still_starting_keeps_the_services(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """A Snapshot without a list (the server doesn't know it yet) changes no services."""
    coordinator = setup_integration.runtime_data
    server.plugin_commands = None
    server.snapshot = make_snapshot(liveSessions=[live_session("s1", "reading")])
    await server.disconnect()
    await wait_for(lambda: coordinator.data.live_session is not None)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, SELECT)
    assert hass.services.has_service(DOMAIN, RESET_SERVICE)


async def test_a_bad_command_costs_only_itself(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """A command that can't be described is skipped; the entry and the others still work."""
    broken = {
        **RESET,
        "command": "broken",
        "input": {"type": "object", "properties": 3},
    }
    server.plugin_commands = [broken, SELECT_PRESET]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SELECT)
    assert not hass.services.has_service(DOMAIN, "magic_cube_broken")

    await server.push_plugin_commands([broken, RESET])
    await wait_for(lambda: hass.services.has_service(DOMAIN, RESET_SERVICE))
    assert not hass.services.has_service(DOMAIN, SELECT)
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_names_nobody_can_have(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """Built-in names stay built-in; a shared name goes to none; unusable fields skip the command."""

    def command(plugin: str, name: str) -> dict[str, Any]:
        return {"plugin": plugin, "pluginName": plugin, "command": name, "title": name}

    server.plugin_commands = [
        {**command("track", "activity"), "output": {"type": "string"}},
        command("a-b", "cD"),
        command("a", "bCD"),
        {
            **command("lamp", "toggle"),
            "input": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
            },
        },
        command("lamp", "blink"),
    ]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # Had the plugin's command taken it, it would answer with a response.
    assert hass.services.supports_response(DOMAIN, "track_activity") is (
        SupportsResponse.NONE
    )
    assert not hass.services.has_service(DOMAIN, "a_b_c_d")
    assert not hass.services.has_service(DOMAIN, "lamp_toggle")
    assert hass.services.has_service(DOMAIN, "lamp_blink")
    await hass.config_entries.async_unload(config_entry.entry_id)


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------


async def test_a_call_runs_the_command_with_its_fields(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """The fields are the input; the plugin's output is the response."""
    server.results["RunPluginCommand"] = {
        "_tag": "Ok",
        "output": {"activePresetId": "focus"},
    }
    device = workspace_device(hass)
    response = await call(
        hass, SELECT, {"device_id": device, "preset": "Focus"}, response=True
    )
    assert response == {"output": {"activePresetId": "focus"}}
    # A number from YAML or a template, for a text field.
    assert await call(hass, SELECT, {"device_id": device, "preset": 5}) is None
    server.results["RunPluginCommand"] = {"_tag": "Ok"}
    await call(hass, RESET_SERVICE, {"device_id": device})
    assert server.calls_to("RunPluginCommand") == [
        {
            "plugin": "magic-cube",
            "command": "selectPreset",
            "input": {"preset": "Focus"},
        },
        {"plugin": "magic-cube", "command": "selectPreset", "input": {"preset": "5"}},
        {"plugin": "magic-cube", "command": "reset", "input": {}},
    ]


async def test_a_date_from_yaml_is_sent_as_text(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """YAML turns `2026-09-29` into a date: it goes as ISO text, and the server checks it."""
    server.results["RunPluginCommand"] = {"_tag": "Ok"}
    data = {"device_id": workspace_device(hass), "preset": date(2026, 9, 29)}
    await call(hass, SELECT, data)
    assert server.calls_to("RunPluginCommand") == [
        {
            "plugin": "magic-cube",
            "command": "selectPreset",
            "input": {"preset": "2026-09-29"},
        }
    ]


async def test_a_call_needs_a_workspace_device(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """Activity devices aren't targets."""
    activity = device_for(hass, f"{WORKSPACE_ID}:activity:reading")
    assert activity is not None
    with pytest.raises(ServiceValidationError, match="activity"):
        await call(hass, RESET_SERVICE, {"device_id": activity.id})
    assert server.calls_to("RunPluginCommand") == []


REJECTED = ["invalid", "not-found", "not-installed", "disabled", "incompatible"]
FAILED = ["unavailable", "unsupported", "loop", "failed", "brand-new"]
MAY_HAVE_RUN = ["timeout", "stopped"]


@pytest.mark.parametrize("code", REJECTED + FAILED + MAY_HAVE_RUN)
async def test_failures_say_what_happened(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
    code: str,
) -> None:
    """Bad calls are validation errors; timeout and stopped say the command may have run."""
    server.results["RunPluginCommand"] = {
        "_tag": "Failed",
        "code": code,
        "message": "It said no",
    }
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, RESET_SERVICE, {"device_id": workspace_device(hass)})
    assert isinstance(raised.value, ServiceValidationError) is (code in REJECTED)
    assert str(raised.value) == (
        f"magic-cube can't run reset: It said no ({code})"
        if code in REJECTED
        else "magic-cube reset may have run, but Drift Beacon couldn't confirm it: It said no"
        if code in MAY_HAVE_RUN
        else f"magic-cube reset failed: It said no ({code})"
    )


async def test_failures_before_the_command_reached_the_server(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
) -> None:
    """An older server doesn't know the call; without a connection nothing is sent."""
    device = workspace_device(hass)
    server.errors["RunPluginCommand"] = "InternalError"
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, RESET_SERVICE, {"device_id": device})
    assert str(raised.value) == (
        "Drift Beacon rejected RunPluginCommand: InternalError happened (InternalError)"
    )
    server.handshake_status = 503
    await server.disconnect()
    await wait_for(lambda: server.handshakes >= 2)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, RESET_SERVICE, {"device_id": device})
    assert str(raised.value) == "Drift Beacon is not connected right now"
    assert len(server.calls_to("RunPluginCommand")) == 1


async def test_no_answer_may_have_run(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    with_commands: None,
    setup_integration: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an answer, in time or before the connection drops, the command may have run."""
    monkeypatch.setattr(
        "custom_components.drift_beacon.coordinator.PLUGIN_COMMAND_TIMEOUT", 0.1
    )
    server.results["RunPluginCommand"] = asyncio.get_running_loop().create_future()
    device = workspace_device(hass)
    with pytest.raises(HomeAssistantError, match="may have run"):
        await call(hass, RESET_SERVICE, {"device_id": device})

    monkeypatch.setattr(
        "custom_components.drift_beacon.coordinator.PLUGIN_COMMAND_TIMEOUT", 15
    )
    pending = hass.async_create_task(call(hass, RESET_SERVICE, {"device_id": device}))
    await wait_for(lambda: len(server.calls_to("RunPluginCommand")) == 2)
    await server.disconnect()
    with pytest.raises(HomeAssistantError, match="may have run"):
        await pending


# ----------------------------------------------------------------------
# Several workspaces, and keeping services while a workspace is away
# ----------------------------------------------------------------------


async def test_several_workspaces(
    hass: HomeAssistant, server: FakeDriftBeacon, socket_enabled: None
) -> None:
    """Services are the union of the workspaces' lists; each call goes to its own workspace."""
    office = FakeDriftBeacon()
    office.snapshot = make_snapshot(workspaceId="ws-2", workspaceName="Office")
    await office.start()
    try:
        server.plugin_commands = [SELECT_PRESET, RESET]
        # An older version there: the first entry, home, describes the shared service.
        office.plugin_commands = [{**SELECT_PRESET, "title": "Pick", "output": None}]
        home, work = make_entry(server), make_entry(office, "ws-2", "Office")
        for entry in (home, work):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        home_device, work_device = (
            workspace_device(hass),
            workspace_device(hass, "ws-2"),
        )
        both = {"device_id": [home_device, work_device], "preset": "Focus"}
        description = async_get_cached_service_description(hass, DOMAIN, SELECT)
        assert description is not None
        assert description["name"] == "Magic Cube: Select preset"
        assert hass.services.supports_response(DOMAIN, SELECT) is (
            SupportsResponse.OPTIONAL
        )

        with pytest.raises(ServiceValidationError, match="Office"):
            await call(hass, RESET_SERVICE, {"device_id": work_device})
        for fake in (server, office):
            fake.results["RunPluginCommand"] = {"_tag": "Ok"}
        await call(hass, SELECT, both)
        assert len(server.calls_to("RunPluginCommand")) == 1
        assert len(office.calls_to("RunPluginCommand")) == 1
        with pytest.raises(ServiceValidationError, match="one workspace"):
            await call(hass, SELECT, both, response=True)

        # Unloading keeps a workspace's services: calls fail until it's back.
        await hass.config_entries.async_unload(home.entry_id)
        assert hass.services.has_service(DOMAIN, RESET_SERVICE)
        with pytest.raises(HomeAssistantError, match="not loaded"):
            await call(hass, RESET_SERVICE, {"device_id": home_device})
        # Removing it drops what only it offered.
        await hass.config_entries.async_remove(home.entry_id)
        assert not hass.services.has_service(DOMAIN, RESET_SERVICE)
        description = async_get_cached_service_description(hass, DOMAIN, SELECT)
        assert description is not None
        assert description["name"] == "Magic Cube: Pick"
        assert hass.services.supports_response(DOMAIN, SELECT) is SupportsResponse.NONE
        # So does disabling the last one.
        await hass.config_entries.async_set_disabled_by(
            work.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert not hass.services.has_service(DOMAIN, SELECT)
    finally:
        await office.stop()


async def test_services_wait_for_a_server_that_is_down(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """After a restart, the last list is registered even before the server answers."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"entries": {config_entry.entry_id: [SELECT_PRESET]}},
    }
    server.handshake_status = 503
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.services.has_service(DOMAIN, SELECT)
    with pytest.raises(HomeAssistantError, match="not loaded"):
        await call(
            hass, SELECT, {"device_id": workspace_device(hass), "preset": "Focus"}
        )
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_disabling_a_retrying_entry_drops_its_services(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """An entry disabled while it waits for its server loses its stored services too."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"entries": {config_entry.entry_id: [SELECT_PRESET]}},
    }
    server.handshake_status = 503
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.services.has_service(DOMAIN, SELECT)
    await hass.config_entries.async_set_disabled_by(
        config_entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SELECT)
