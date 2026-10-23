"""Config, discovery, reauth and reconfigure flows against the fake server."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_USER,
    SOURCE_ZEROCONF,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.drift_beacon import api, config_flow
from custom_components.drift_beacon.api import DriftBeaconSslError
from custom_components.drift_beacon.const import (
    CONF_API_TOKEN,
    CONF_HTTPS_PORT,
    CONF_HUB_ID,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_WORKSPACE_ID,
    DOMAIN,
)

from .conftest import HUB_ID, TOKEN, USER_ID, WORKSPACE_ID, FakeDriftBeacon, make_entry


def _user_input(server: FakeDriftBeacon, **overrides: object) -> dict[str, object]:
    return {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: server.port,
        CONF_VERIFY_SSL: True,
        CONF_API_TOKEN: TOKEN,
        **overrides,
    }


Probe = tuple[str, str, int]


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[Probe]:
    """Each (protocol, host, port) the flow asks which server it is, in order.

    What reached the hub is checked at the fake hub itself: its ``tls_hellos`` and
    ``unhandled`` connections.
    """
    asked: list[Probe] = []
    real = config_flow.async_get_server_status

    async def recording(
        session: Any, protocol: str, host: str, port: int, *, timeout: float
    ) -> dict[str, Any]:
        asked.append((protocol, host, port))
        return await real(session, protocol, host, port, timeout=timeout)

    monkeypatch.setattr(config_flow, "async_get_server_status", recording)
    return asked


@pytest.fixture
def https_fails_verification(
    probes: list[Probe], monkeypatch: pytest.MonkeyPatch
) -> list[Probe]:
    """As ``probes``, but asking over https fails TLS verification and reaches no hub.

    As against a hub's certificate that isn't trusted. Asking over http reaches the fake hub.
    """

    async def failing(
        session: Any, protocol: str, host: str, port: int, *, timeout: float
    ) -> dict[str, Any]:
        probes.append((protocol, host, port))
        if protocol == "https":
            raise DriftBeaconSslError("certificate verify failed")
        return await api.async_get_server_status(
            session, protocol, host, port, timeout=timeout
        )

    monkeypatch.setattr(config_flow, "async_get_server_status", failing)
    return probes


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
    assert result["data"][CONF_HUB_ID] == HUB_ID
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_the_hubs_http_port_typed_by_hand_gets_no_tls_handshake(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    probes: list[Probe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 9821 http is asked first; the hub answers it, so https is never asked."""
    monkeypatch.setattr(config_flow, "DEFAULT_PORT", server.port)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PROTOCOL] == "http"
    assert probes == [("http", "127.0.0.1", server.port)]
    assert (server.tls_hellos, server.unhandled) == (0, 0)
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_another_port_typed_by_hand_is_asked_over_https_first(
    hass: HomeAssistant, server: FakeDriftBeacon, probes: list[Probe]
) -> None:
    """It may be the hub's HTTPS port, or a proxy's: http is asked when https gets nothing."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PROTOCOL] == "http"
    assert probes == [
        ("https", "127.0.0.1", server.port),
        ("http", "127.0.0.1", server.port),
    ]
    # The fake hub sees the one handshake, first, and turns it away.
    assert (server.tls_hellos, server.unhandled) == (1, 1)
    assert server.accepted[0].tls_hello
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_9821_typed_by_hand_is_asked_over_https_when_http_gets_no_answer(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    probes: list[Probe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As when the hub's HTTPS port is remapped onto 9821: https is still asked there."""
    monkeypatch.setattr(config_flow, "DEFAULT_PORT", server.port)
    server.not_a_hub = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["errors"] == {"base": "invalid_server"}
    assert probes == [
        ("http", "127.0.0.1", server.port),
        ("https", "127.0.0.1", server.port),
    ]
    # The plain requests first, then the handshake, which reached the port.
    assert not server.accepted[0].tls_hello
    assert server.accepted[-1].tls_hello
    assert (server.tls_hellos, server.unhandled) == (1, 1)


@pytest.mark.parametrize(
    ("http_port", "asked"),
    [(False, ("https", "http")), (True, ("http", "https"))],
    ids=["another port", "9821"],
)
async def test_a_tls_failure_is_reported_when_neither_protocol_answers(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    https_fails_verification: list[Probe],
    monkeypatch: pytest.MonkeyPatch,
    http_port: bool,
    asked: tuple[str, ...],
) -> None:
    """In either order the TLS failure is what the form shows, not that no hub is there."""
    if http_port:
        monkeypatch.setattr(config_flow, "DEFAULT_PORT", server.port)
    server.not_a_hub = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["errors"] == {"base": "ssl_verification_failed"}
    assert https_fails_verification == [
        (protocol, "127.0.0.1", server.port) for protocol in asked
    ]


async def test_http_is_still_asked_after_https_fails_tls_verification(
    hass: HomeAssistant, server: FakeDriftBeacon, https_fails_verification: list[Probe]
) -> None:
    """At another port https comes first; its TLS failure doesn't stop http being asked."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PROTOCOL] == "http"
    assert https_fails_verification == [
        ("https", "127.0.0.1", server.port),
        ("http", "127.0.0.1", server.port),
    ]
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_user_flow_detects_a_hub_from_before_the_info_document(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """A hub that answers only /api/device/status is still recognised."""
    server.legacy_status_only = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PROTOCOL] == "http"
    # It gives no hub id; the entry learns one when the hub has /api/info.
    assert CONF_HUB_ID not in result["data"]
    await hass.config_entries.async_unload(result["result"].entry_id)


def _defaults(result: FlowResult) -> dict[str, Any]:
    """The form's prefilled values, by field."""
    return {
        str(key): key.default()
        for key in result["data_schema"].schema
        if key.default is not vol.UNDEFINED
    }


async def test_the_form_finds_the_home_assistant_app_on_this_machine(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    probes: list[Probe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hub answering over loopback fills in the address; nothing else is guessed.

    It is the hub's HTTP port, so it gets no TLS handshake: not when the form opens, and not
    when the address is sent unchanged.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert CONF_HOST not in _defaults(result)

    monkeypatch.setattr(
        "custom_components.drift_beacon.config_flow.LOCAL_HUB_ADDRESSES",
        (("127.0.0.1", server.port),),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    defaults = _defaults(result)
    assert (defaults[CONF_HOST], defaults[CONF_PORT]) == ("127.0.0.1", server.port)
    assert server.handshakes == 0
    assert probes == [("http", "127.0.0.1", server.port)]
    assert (server.tls_hellos, server.unhandled) == (0, 0)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert probes == [("http", "127.0.0.1", server.port)] * 2
    assert (server.tls_hellos, server.unhandled) == (0, 0)
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_the_loopback_port_is_asked_over_https_once_when_http_gets_no_answer(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    probes: list[Probe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not http only: a hub whose HTTPS port is remapped onto it can still be found."""
    monkeypatch.setattr(
        "custom_components.drift_beacon.config_flow.LOCAL_HUB_ADDRESSES",
        (("127.0.0.1", server.port),),
    )
    server.not_a_hub = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert CONF_HOST not in _defaults(result)
    assert probes == [
        ("http", "127.0.0.1", server.port),
        ("https", "127.0.0.1", server.port),
    ]
    # The one https try reached the port as a handshake, and nothing else followed it.
    assert server.accepted[-1].tls_hello
    assert (server.tls_hellos, server.unhandled) == (1, 1)


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
    """Reauth replaces the token and reloads, at the entry's address and protocol."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Asked who it is once, over the entry's own protocol.
    assert server.info_requests == 1
    await hass.async_block_till_done()
    # Neither reauth nor the reload after it sent the hub's HTTP port a TLS handshake.
    assert (server.tls_hellos, server.unhandled) == (0, 0)
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


async def test_reconfigure_asks_the_entry_address_over_its_own_protocol(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    config_entry: MockConfigEntry,
    probes: list[Probe],
) -> None:
    """Only TLS verification changes: the entry's HTTP port gets no TLS handshake."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: False},
    )
    assert result["reason"] == "reconfigure_successful"
    assert probes == [("http", "127.0.0.1", server.port)]
    await hass.async_block_till_done()
    assert (server.tls_hellos, server.unhandled) == (0, 0)
    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    ("http_port", "asked", "tls_hellos"),
    [(True, ("http",), 0), (False, ("https", "http"), 1)],
    ids=["9821", "another port"],
)
async def test_reconfigure_asks_a_new_address_as_one_added_by_hand(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    probes: list[Probe],
    monkeypatch: pytest.MonkeyPatch,
    http_port: bool,
    asked: tuple[str, ...],
    tls_hellos: int,
) -> None:
    """The port decides: http first at 9821, https first at any other, which may be TLS."""
    if http_port:
        monkeypatch.setattr(config_flow, "DEFAULT_PORT", server.port)
    entry = make_entry(server)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PORT: 1})
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: True},
    )
    assert result["reason"] == "reconfigure_successful"
    assert probes == [(protocol, "127.0.0.1", server.port) for protocol in asked]
    await hass.async_block_till_done()
    assert (server.tls_hellos, server.unhandled) == (tls_hellos, tls_hellos)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_asks_the_entry_http_port_over_http_at_a_new_host(
    hass: HomeAssistant, server: FakeDriftBeacon, probes: list[Probe]
) -> None:
    """Only the host changes: the entry's HTTP port, not 9821, still gets no TLS handshake."""
    entry = make_entry(server)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_HOST: "192.168.1.80"}
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: True},
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "127.0.0.1"
    assert probes == [("http", "127.0.0.1", server.port)]
    await hass.async_block_till_done()
    assert (server.tls_hellos, server.unhandled) == (0, 0)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_falls_back_to_the_other_protocol_only(
    hass: HomeAssistant, server: FakeDriftBeacon, probes: list[Probe]
) -> None:
    """The same address no longer answers over the entry's protocol: only the other is asked."""
    entry = make_entry(server)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_PROTOCOL: "https"}
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: True},
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PROTOCOL] == "http"
    assert probes == [
        ("https", "127.0.0.1", server.port),
        ("http", "127.0.0.1", server.port),
    ]
    # The entry's own https once, not again after http.
    assert (server.tls_hellos, server.unhandled) == (1, 1)
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_falls_back_to_the_other_protocol_only_after_a_timeout(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    config_entry: MockConfigEntry,
    probes: list[Probe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry's http times out at its own address: https is asked, and http not again."""
    monkeypatch.setattr(config_flow, "DETECTION_TIMEOUT", 0.1)
    server.info_delay = 0.5
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: True},
    )
    assert result["errors"] == {"base": "invalid_server"}
    assert probes == [
        ("http", "127.0.0.1", server.port),
        ("https", "127.0.0.1", server.port),
    ]
    assert server.info_requests == 1
    assert (server.tls_hellos, server.unhandled) == (1, 1)


# ---------------------------------------------------------------------------------------------
# Zeroconf discovery
# ---------------------------------------------------------------------------------------------


def _discovery(
    server: FakeDriftBeacon,
    *,
    addresses: list[IPv4Address | IPv6Address] | None = None,
    port: int | None = None,
    **txt: str | None,
) -> ZeroconfServiceInfo:
    """The hub's announcement as Home Assistant hands it over; ``txt`` overrides TXT keys."""
    addresses = addresses or [ip_address("127.0.0.1")]
    properties = {
        "txtvers": "1",
        "id": HUB_ID,
        "proto": "1",
        "ver": "0.3.0",
        "https": "9820",
        "fp": "kUN46OEDB0UFdufCAXq7cuGSngqtorakyt+CmtJEKoY=",
        **txt,
    }
    return ZeroconfServiceInfo(
        ip_address=addresses[0],
        ip_addresses=addresses,
        port=server.port if port is None else port,
        hostname="driftbeacon.local.",
        type="_drift-beacon._tcp.local.",
        name="Drift Beacon a1b2._drift-beacon._tcp.local.",
        properties={
            key: value for key, value in properties.items() if value is not None
        },
    )


async def _discover(hass: HomeAssistant, info: ZeroconfServiceInfo) -> FlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )


async def test_a_new_hub_is_confirmed_then_asks_for_an_api_key(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Confirm, then the usual form at the discovered address; the key is pasted, not found."""
    server.info = {"ports": {"https": 9820, "http": server.port}}
    result = await _discover(hass, _discovery(server))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {
        "name": "Drift Beacon a1b2",
        "host": "driftbeacon.local",
    }
    [flow] = hass.config_entries.flow.async_progress()
    assert flow["context"]["title_placeholders"] == {"name": "Drift Beacon a1b2"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "user"
    defaults = _defaults(result)
    assert (defaults[CONF_HOST], defaults[CONF_PORT]) == ("127.0.0.1", server.port)
    assert server.handshakes == 0

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == WORKSPACE_ID
    assert result["data"][CONF_HUB_ID] == HUB_ID
    assert result["data"][CONF_HTTPS_PORT] == 9820
    assert result["data"][CONF_PROTOCOL] == "http"
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_a_discovered_hub_gets_no_tls_handshake(
    hass: HomeAssistant, server: FakeDriftBeacon, probes: list[Probe]
) -> None:
    """Its SRV port is its HTTP port, asked over http first: the hub answers, so only that."""
    result = await _discover(hass, _discovery(server))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    # A wrong key first: the form shown again still holds the discovered address.
    server.handshake_status = 401
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["errors"] == {"base": "invalid_auth"}
    server.handshake_status = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PROTOCOL] == "http"
    assert probes == [("http", "127.0.0.1", server.port)] * 2
    assert (server.tls_hellos, server.unhandled) == (0, 0)
    await hass.config_entries.async_unload(result["result"].entry_id)


@pytest.mark.parametrize(
    ("announced", "asked", "tls_hellos"),
    [
        ({"port": 9900}, ("https", "http"), 1),
        ({"addresses": [ip_address("192.168.1.80")]}, ("http",), 0),
    ],
    ids=["port", "host"],
)
async def test_the_port_decides_in_the_discovered_form(
    hass: HomeAssistant,
    server: FakeDriftBeacon,
    probes: list[Probe],
    announced: dict[str, Any],
    asked: tuple[str, ...],
    tls_hellos: int,
) -> None:
    """Any host at the announced port is asked over http first; another port as by hand."""
    result = await _discover(hass, _discovery(server, **announced))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    defaults = _defaults(result)
    assert (defaults[CONF_HOST], defaults[CONF_PORT]) != ("127.0.0.1", server.port)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert probes == [(protocol, "127.0.0.1", server.port) for protocol in asked]
    assert (server.tls_hellos, server.unhandled) == (tls_hellos, tls_hellos)
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_a_known_hub_moves_every_entry_with_its_id_and_shows_no_card(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Each workspace on the hub follows it to its new address and ports; others stay put."""
    home = make_entry(server)
    home.add_to_hass(hass)
    work = make_entry(server, "ws-2", "Work")
    work.add_to_hass(hass)
    other = make_entry(server, "ws-3", "Elsewhere", hub_id="another-hub")
    other.add_to_hass(hass)

    info = _discovery(
        server, addresses=[ip_address("192.168.1.80")], port=9900, https="9800"
    )
    for _ in range(2):
        # As after every restart.
        result = await _discover(hass, info)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"
    assert hass.config_entries.flow.async_progress() == []

    for entry in (home, work):
        assert entry.data[CONF_HOST] == "192.168.1.80"
        assert entry.data[CONF_PORT] == 9900
        assert entry.data[CONF_PROTOCOL] == "http"
        assert entry.data[CONF_HTTPS_PORT] == 9800
        assert entry.data[CONF_API_TOKEN] == TOKEN
    assert other.data[CONF_HOST] == "127.0.0.1"
    assert other.data[CONF_PORT] == server.port


async def test_an_entry_set_up_over_https_is_never_moved_by_discovery(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """A proxy or the hub's HTTPS address is the user's choice: discovery leaves it alone."""
    proxied = make_entry(server)
    proxied.add_to_hass(hass)
    data = {
        **proxied.data,
        CONF_HOST: "drift.example.com",
        CONF_PORT: 443,
        CONF_PROTOCOL: "https",
    }
    hass.config_entries.async_update_entry(proxied, data=data)

    result = await _discover(
        hass, _discovery(server, addresses=[ip_address("192.168.1.80")], port=9900)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(proxied.data) == data


async def test_an_entry_that_lost_its_hub_follows_it_to_its_new_port(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """A retrying entry is set up again at once at the address discovery found."""
    entry = make_entry(server)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PORT: 1})
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    result = await _discover(hass, _discovery(server))
    assert result["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_PORT] == server.port
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_ipv4_address_is_taken_over_an_ipv6_one(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Home Assistant may list IPv6 first; the client builds URLs without brackets."""
    info = _discovery(
        server, addresses=[ip_address("fd00::5"), ip_address("127.0.0.1")]
    )
    assert info.host == "fd00::5"
    result = await _discover(hass, info)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert _defaults(result)[CONF_HOST] == "127.0.0.1"


async def test_a_hub_with_only_ipv6_addresses_is_not_offered(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Nothing to connect to: no card."""
    result = await _discover(
        hass, _discovery(server, addresses=[ip_address("fd00::5")])
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ipv4_address"


@pytest.mark.parametrize("proto", ["2", "0", "one", None])
async def test_a_hub_on_another_mobile_protocol_is_still_followed(
    hass: HomeAssistant, server: FakeDriftBeacon, proto: str | None
) -> None:
    """`proto` is the mobile app's contract: a bump there mustn't hide the hub from Home Assistant."""
    entry = make_entry(server)
    entry.add_to_hass(hass)
    result = await _discover(
        hass, _discovery(server, addresses=[ip_address("192.168.1.80")], proto=proto)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.80"


async def test_an_announcement_without_a_hub_id_is_skipped(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Without an id there is no telling which hub it is."""
    result = await _discover(hass, _discovery(server, id=None))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery"


async def test_a_hub_the_user_ignored_shows_no_card(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """Ignoring the card keeps the hub's id, so it doesn't come back."""
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id=HUB_ID).add_to_hass(
        hass
    )
    result = await _discover(hass, _discovery(server))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------------------------
# The hub id in reauth and reconfigure
# ---------------------------------------------------------------------------------------------


async def test_reauth_sends_no_key_to_another_hub(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """The entry's address now answers as another hub: the new key isn't sent there."""
    config_entry.add_to_hass(hass)
    server.hub_id = "another-hub"
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN}
    )
    assert result["errors"] == {"base": "wrong_hub"}
    assert server.handshakes == 0


async def test_reconfigure_sends_no_key_to_another_hub(
    hass: HomeAssistant, server: FakeDriftBeacon, config_entry: MockConfigEntry
) -> None:
    """A new address must answer as the entry's hub before the kept key goes there."""
    config_entry.add_to_hass(hass)
    server.hub_id = "another-hub"
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: True},
    )
    assert result["errors"] == {"base": "wrong_hub"}
    assert server.handshakes == 0


async def test_reconfigure_gives_an_entry_from_before_hub_ids_its_hub(
    hass: HomeAssistant, server: FakeDriftBeacon
) -> None:
    """With no hub id to compare, the hub at the new address becomes the entry's."""
    entry = make_entry(server, hub_id=None)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: server.port, CONF_VERIFY_SSL: True},
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HUB_ID] == HUB_ID
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
