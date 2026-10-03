"""Config, reauth and reconfigure flows against the fake server."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.drift_beacon.const import (
    CONF_API_TOKEN,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_WORKSPACE_ID,
    DOMAIN,
)

from .conftest import TOKEN, USER_ID, WORKSPACE_ID, FakeDriftBeacon


def _user_input(server: FakeDriftBeacon, **overrides: object) -> dict[str, object]:
    return {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: server.port,
        CONF_VERIFY_SSL: True,
        CONF_API_TOKEN: TOKEN,
        **overrides,
    }


async def test_user_flow_creates_a_workspace_entry(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """The token decides the workspace and user; plain http is detected."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["result"].unique_id == WORKSPACE_ID
    assert result["data"][CONF_PROTOCOL] == "http"
    assert result["data"][CONF_WORKSPACE_ID] == WORKSPACE_ID
    assert result["data"][CONF_USER_ID] == USER_ID
    await hass.config_entries.async_unload(result["result"].entry_id)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, "invalid_auth"), (403, "invalid_auth"), (503, "cannot_connect")],
)
async def test_user_flow_maps_handshake_errors(
    hass: HomeAssistant, server: FakeDriftBeacon, status: int, error: str
) -> None:
    """A rejected token is an auth error; a server outage is not."""
    server.handshake_status = status
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_user_flow_without_a_server(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Nothing answering as Drift Beacon is reported as the wrong address."""
    port = server.port
    await server.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_user_input(server), CONF_PORT: port}
    )
    assert result["errors"] == {"base": "invalid_server"}


async def test_one_entry_per_workspace(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """Adding the same workspace again aborts."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_accepts_a_new_token_for_the_same_user(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """Reauth replaces the token and reloads."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [("workspaceId", "ws-2", "wrong_workspace"), ("userId", "user-2", "wrong_user")],
)
async def test_reauth_rejects_a_token_for_someone_else(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    config_entry: MockConfigEntry,
    field: str,
    value: str,
    error: str,
) -> None:
    """A token for another workspace or user would silently change who the entry acts for."""
    config_entry.add_to_hass(hass)
    server.snapshot[field] = value
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN}
    )
    assert result["errors"] == {"base": error}


async def test_reconfigure_changes_the_address(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """Host, port and TLS verification can change without re-adding the entry."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_VERIFY_SSL] is False
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reconfigure_refuses_a_different_workspace(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """Pointing the entry at a server where the token means another workspace aborts."""
    config_entry.add_to_hass(hass)
    server.snapshot["workspaceId"] = "ws-2"
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_workspace"
