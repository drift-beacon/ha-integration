"""Setup, device model, unload, migration and device removal."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.drift_beacon import async_remove_config_entry_device
from custom_components.drift_beacon.const import DOMAIN

from .conftest import WORKSPACE_ID, FakeDriftBeacon, device_for


def _activity_device(hass: HomeAssistant, activity_id: str) -> dr.DeviceEntry | None:
    return device_for(hass, f"{WORKSPACE_ID}:activity:{activity_id}")


async def test_one_device_per_activity_under_the_workspace(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Activities are service devices linked to the workspace device, not its children."""
    workspace = device_for(hass, WORKSPACE_ID)
    assert workspace is not None
    assert workspace.name == "Home"
    assert workspace.model == "Workspace"
    assert workspace.entry_type is dr.DeviceEntryType.SERVICE

    reading = _activity_device(hass, "reading")
    water = _activity_device(hass, "water")
    assert reading is not None and water is not None
    assert (reading.name, reading.model, reading.model_id) == (
        "Reading",
        "Activity",
        "span",
    )
    assert (water.name, water.model, water.model_id) == ("Water", "Activity", "point")
    assert reading.via_device_id == workspace.id
    assert reading.entry_type is dr.DeviceEntryType.SERVICE


async def test_every_entity_is_hidden_and_named_from_its_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Nothing reaches auto-generated dashboards; names come from the device."""
    entities = er.async_entries_for_config_entry(
        er.async_get(hass), setup_integration.entry_id
    )
    by_id = {entry.entity_id: entry for entry in entities}
    assert set(by_id) == {
        "switch.reading_session",
        "switch.reading_pin",
        "sensor.reading_progress",
        "button.water_mark",
        "switch.water_pin",
        "sensor.water_progress",
        "sensor.home_current_session",
        "sensor.home_pinned_activity",
        "sensor.home_connected_user",
        "button.home_stop_session",
    }
    assert all(
        entry.hidden_by is er.RegistryEntryHider.INTEGRATION for entry in entities
    )
    assert by_id["sensor.home_connected_user"].entity_category == "diagnostic"
    assert (
        by_id["switch.reading_session"].device_id
        == _activity_device(hass, "reading").id
    )


async def test_unload_closes_the_connection(
    hass: HomeAssistant, setup_integration: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Unloading drops the socket and makes entities unavailable."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    assert server.subscribers == {}
    assert hass.states.get("switch.reading_session").state == "unavailable"


async def test_server_down_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """An unreachable server leaves the entry retrying."""
    await server.stop()
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_token_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """401 at the handshake asks the user for a new token."""
    server.handshake_status = 401
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_token_for_another_user_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """The entry acts for one user; a token that now belongs to someone else is rejected."""
    server.snapshot["userId"] = "someone-else"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_missing_workspace_retries_and_raises_an_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """404 can be a deleted workspace or one the server cannot load yet; keep retrying."""
    server.handshake_status = 404
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    issue_id = f"workspace_not_found_{config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)

    await hass.config_entries.async_remove(config_entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_entries_from_the_old_model_ask_to_be_re_added(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Version 3 entries cannot migrate; a repair issue explains what to do."""
    legacy = MockConfigEntry(
        domain=DOMAIN, version=3, title="Old", data=dict(config_entry.data)
    )
    legacy.add_to_hass(hass)
    await hass.config_entries.async_setup(legacy.entry_id)
    assert legacy.state is ConfigEntryState.MIGRATION_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_entry_{legacy.entry_id}")


async def test_devices_cannot_be_deleted_while_the_entry_is_not_loaded(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeDriftBeacon
) -> None:
    """Without a loaded entry there is no state to decide with, so nothing is deletable."""
    server.handshake_status = 503
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"{WORKSPACE_ID}:activity:gone")},
    )
    assert not await async_remove_config_entry_device(hass, config_entry, device)


async def test_only_devices_of_gone_activities_can_be_deleted(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The workspace device and live activities are protected from manual deletion."""
    registry = dr.async_get(hass)
    workspace = device_for(hass, WORKSPACE_ID)
    reading = _activity_device(hass, "reading")
    orphan = registry.async_get_or_create(
        config_entry_id=setup_integration.entry_id,
        identifiers={(DOMAIN, f"{WORKSPACE_ID}:activity:gone")},
    )
    entry = setup_integration
    assert not await async_remove_config_entry_device(hass, entry, workspace)
    assert not await async_remove_config_entry_device(hass, entry, reading)
    assert await async_remove_config_entry_device(hass, entry, orphan)
