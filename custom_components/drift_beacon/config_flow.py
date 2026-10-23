"""Config flow for Drift Beacon: one entry per workspace, acting as the token's user."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import (
    DriftBeaconAuthError,
    DriftBeaconConnectionError,
    DriftBeaconError,
    DriftBeaconHubMismatchError,
    DriftBeaconSslError,
    DriftBeaconWorkspaceNotFoundError,
    async_get_connection_info,
    async_get_server_status,
    async_verify_hub,
    hub_https_port,
    hub_id_of,
)
from .const import (
    CONF_API_TOKEN,
    CONF_HTTPS_PORT,
    CONF_HUB_ID,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_WORKSPACE_ID,
    DEFAULT_PORT,
    DETECTION_TIMEOUT,
    DOMAIN,
    LOCAL_HUB_ADDRESSES,
)

_LOGGER = logging.getLogger(__name__)

PORT_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
)
TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
# The orders an address is asked in, one protocol at a time. The hub's HTTP port is asked over
# http first: a TLS handshake there can only fail, and the hub logs it as a parse error. Any
# other port may be the hub's HTTPS port or a proxy's, and is asked over https first.
HTTP_FIRST: tuple[str, ...] = ("http", "https")
HTTPS_FIRST: tuple[str, ...] = ("https", "http")


class NoServerFound(DriftBeaconConnectionError):
    """Nothing answering as a Drift Beacon server at the address."""


@dataclass(frozen=True, slots=True)
class DiscoveredHub:
    """A hub as its zeroconf announcement describes it."""

    hub_id: str
    # The DNS-SD instance name, "Drift Beacon <shortid>".
    name: str
    # The name the hub claimed, "driftbeacon.local".
    hostname: str
    # An IPv4 address: the client builds URLs without IPv6 brackets.
    host: str
    # The SRV port: the hub's plain HTTP port.
    port: int
    https_port: int | None


def _server_schema(host: str | None, port: int, verify_ssl: bool) -> dict[Any, Any]:
    # No host found and none given: an empty field, not a name that only resolves somewhere.
    host_key = (
        vol.Required(CONF_HOST, default=host) if host else vol.Required(CONF_HOST)
    )
    return {
        host_key: str,
        vol.Required(CONF_PORT, default=port): PORT_SELECTOR,
        vol.Required(CONF_VERIFY_SSL, default=verify_ssl): bool,
    }


def _txt_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _ipv4_address(discovery_info: ZeroconfServiceInfo) -> str | None:
    """The first usable IPv4 address the announcement carries, even when IPv6 came first."""
    for address in discovery_info.ip_addresses:
        if (
            isinstance(address, IPv4Address)
            and not address.is_link_local
            and not address.is_unspecified
        ):
            return str(address)
    return None


def _hub_data(document: dict[str, Any]) -> dict[str, Any]:
    """What an entry keeps of its hub, from the document the address answered with."""
    data: dict[str, Any] = {}
    if (hub_id := hub_id_of(document)) is not None:
        data[CONF_HUB_ID] = hub_id
    if (https_port := hub_https_port(document)) is not None:
        data[CONF_HTTPS_PORT] = https_port
    return data


class DriftBeaconConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Drift Beacon workspace from a workspace-scoped access token."""

    VERSION = 4

    def __init__(self) -> None:
        """Initialise flow state."""
        self._detected: tuple[str, int] | None = None
        self._detection_done = False
        self._discovered: DiscoveredHub | None = None

    async def _async_detect_protocol(
        self,
        host: str,
        port: int,
        verify_ssl: bool,
        protocols: tuple[str, ...],
    ) -> tuple[str, dict[str, Any]]:
        """Return the first of ``protocols`` that serves Drift Beacon, asking one at a time.

        The next protocol is asked only when the one before gets no Drift Beacon answer (an
        error, a timeout, or another server), so a hub that answers the first hears nothing
        else. With the protocol, the document the hub answered with: its info, or a status
        document from a hub from before ``/api/info``. When none answers, raises the TLS
        verification failure if there was one, else NoServerFound.
        """
        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        ssl_error: DriftBeaconSslError | None = None
        for protocol in protocols:
            try:
                document = await async_get_server_status(
                    session, protocol, host, port, timeout=DETECTION_TIMEOUT
                )
            except DriftBeaconSslError as err:
                ssl_error = ssl_error or err
            except DriftBeaconError:
                continue
            else:
                return protocol, document
        if ssl_error is not None:
            raise ssl_error
        raise NoServerFound(f"No Drift Beacon server at {host}:{port}")

    def _probe_order(
        self, port: int, entry_http_port: int | None = None
    ) -> tuple[str, ...]:
        """The order an address the user submits is asked in: its port decides.

        The hub's HTTP port is asked over http first: the default one, the SRV port of the
        hub discovered for this flow, the one the hub on this machine answered at, and
        ``entry_http_port``, the port of an entry set up over http, whatever host was typed
        with it. Any other port is asked over https first.
        """
        http_ports = {DEFAULT_PORT}
        if entry_http_port is not None:
            http_ports.add(entry_http_port)
        if self._discovered is not None:
            http_ports.add(self._discovered.port)
        if self._detected is not None:
            http_ports.add(self._detected[1])
        return HTTP_FIRST if port in http_ports else HTTPS_FIRST

    async def _async_detect_local_hub(self) -> tuple[str, int] | None:
        """Look for the Home Assistant app on this machine, once per flow."""
        if self._detection_done:
            return self._detected
        self._detection_done = True
        for host, port in LOCAL_HUB_ADDRESSES:
            try:
                # The hub's HTTP port, so http first. Only locating the server; the user's TLS
                # choice applies when connecting.
                # Not http only: a hub whose HTTPS port is remapped onto it is still found.
                await self._async_detect_protocol(
                    host, port, verify_ssl=False, protocols=HTTP_FIRST
                )
            except DriftBeaconError:
                continue
            self._detected = (host, port)
            break
        return self._detected

    async def _async_validate(
        self,
        host: str,
        port: int,
        verify_ssl: bool,
        api_token: str,
        hub_id: str | None = None,
        *,
        protocols: tuple[str, ...],
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        """Return the protocol, the hub's document and the token's identity.

        The address is asked over ``protocols``, in order, until one answers. With ``hub_id``,
        the token is sent only if the address answers as that hub. Raises a DriftBeaconError.
        """
        protocol, document = await self._async_detect_protocol(
            host, port, verify_ssl, protocols
        )
        if hub_id is not None and (found := hub_id_of(document)) != hub_id:
            raise DriftBeaconHubMismatchError(f"{host}:{port}", hub_id, found)
        info = await async_get_connection_info(
            async_get_clientsession(self.hass, verify_ssl=verify_ssl),
            protocol=protocol,
            host=host,
            port=port,
            api_token=api_token,
        )
        return protocol, document, info

    @staticmethod
    def _error_key(err: Exception) -> str:
        if isinstance(err, DriftBeaconAuthError):
            return "invalid_auth"
        if isinstance(err, DriftBeaconSslError):
            return "ssl_verification_failed"
        if isinstance(err, NoServerFound):
            return "invalid_server"
        if isinstance(err, DriftBeaconWorkspaceNotFoundError):
            return "workspace_not_found"
        if isinstance(err, DriftBeaconHubMismatchError):
            return "wrong_hub"
        if isinstance(err, DriftBeaconError):
            return "cannot_connect"
        _LOGGER.exception("Unexpected error validating Drift Beacon connection")
        return "unknown"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the server address and an access token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            verify_ssl = user_input[CONF_VERIFY_SSL]
            try:
                protocol, document, info = await self._async_validate(
                    host,
                    port,
                    verify_ssl,
                    user_input[CONF_API_TOKEN],
                    protocols=self._probe_order(port),
                )
            except Exception as err:  # noqa: BLE001 - mapped to a form error
                errors["base"] = self._error_key(err)
            else:
                await self.async_set_unique_id(info["workspaceId"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info["workspaceName"],
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PROTOCOL: protocol,
                        CONF_VERIFY_SSL: verify_ssl,
                        CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                        CONF_WORKSPACE_ID: info["workspaceId"],
                        CONF_USER_ID: info["userId"],
                        CONF_USER_NAME: info["userName"],
                        **_hub_data(document),
                    },
                )

        detected: tuple[str, int] | None = None
        host_default: str | None
        if user_input is not None:
            host_default = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            verify_ssl = user_input[CONF_VERIFY_SSL]
        elif self._discovered is not None:
            host_default, port = self._discovered.host, self._discovered.port
            verify_ssl = True
        else:
            detected = await self._async_detect_local_hub()
            host_default, port = detected or (None, DEFAULT_PORT)
            verify_ssl = True
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    **_server_schema(host_default, port, verify_ssl),
                    vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
                }
            ),
            description_placeholders={
                "detected": f"{detected[0]}:{detected[1]}" if detected else "",
            },
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """A hub announced itself: follow it if it's known, offer it if it's new.

        The announcement carries no credential: a new hub still needs an API key pasted.
        """
        properties = discovery_info.properties
        hub_id = properties.get("id")
        if not hub_id or not discovery_info.port:
            return self.async_abort(reason="invalid_discovery")
        # `proto` is the mobile app's wire contract, not this integration's (it talks to the hub's
        # integration socket), so a hub on another one is offered and followed all the same.
        if (host := _ipv4_address(discovery_info)) is None:
            return self.async_abort(reason="no_ipv4_address")
        hostname = discovery_info.hostname.rstrip(".")
        hub = DiscoveredHub(
            hub_id=hub_id,
            name=discovery_info.name.removesuffix(f".{discovery_info.type}")
            or hostname,
            hostname=hostname,
            host=host,
            port=discovery_info.port,
            https_port=_txt_int(properties.get("https")),
        )
        if self._async_follow_hub(hub):
            # Quietly, or the card would come back after every restart.
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(hub_id)
        # A hub the user chose to ignore.
        self._abort_if_unique_id_configured()
        self._discovered = hub
        self.context["title_placeholders"] = {"name": hub.name}
        return await self.async_step_zeroconf_confirm()

    @callback
    def _async_follow_hub(self, hub: DiscoveredHub) -> bool:
        """Point every entry of a known hub where it now answers; False for a new hub.

        Entries are workspaces, so one hub can have several. Discovery finds the HTTP port,
        so the entries move to plain HTTP (D8). An entry set up over HTTPS (a proxy, or the
        hub's HTTPS address) is never moved: the user chose that address, and discovery would
        undo the choice at every announcement. Its own retries recover it.
        """
        entries = [
            entry
            for entry in self._async_current_entries(include_ignore=False)
            if entry.data.get(CONF_HUB_ID) == hub.hub_id
        ]
        updates: dict[str, Any] = {
            CONF_HOST: hub.host,
            CONF_PORT: hub.port,
            CONF_PROTOCOL: "http",
        }
        if hub.https_port is not None:
            updates[CONF_HTTPS_PORT] = hub.https_port
        for entry in entries:
            if entry.data.get(CONF_PROTOCOL) == "https":
                continue
            changed = self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **updates}
            )
            # As Home Assistant does for a discovered unique id: an entry retrying setup is
            # tried again now that its hub is known to be up.
            if entry.state is ConfigEntryState.SETUP_RETRY or (
                changed and entry.state is ConfigEntryState.LOADED
            ):
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return bool(entries)

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask whether to set up the hub, then ask for an API key as when added by hand."""
        assert self._discovered is not None
        if user_input is not None:
            return await self.async_step_user()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "name": self._discovered.name,
                "host": self._discovered.hostname or self._discovered.host,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token for the same workspace and user."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = entry.data
            session = async_get_clientsession(
                self.hass, verify_ssl=data[CONF_VERIFY_SSL]
            )
            try:
                # The new token goes only to the entry's own hub.
                await async_verify_hub(
                    session,
                    data[CONF_PROTOCOL],
                    data[CONF_HOST],
                    data[CONF_PORT],
                    data.get(CONF_HUB_ID),
                )
                info = await async_get_connection_info(
                    session,
                    protocol=data[CONF_PROTOCOL],
                    host=data[CONF_HOST],
                    port=data[CONF_PORT],
                    api_token=user_input[CONF_API_TOKEN],
                )
            except Exception as err:  # noqa: BLE001 - mapped to a form error
                errors["base"] = self._error_key(err)
            else:
                if info["workspaceId"] != data[CONF_WORKSPACE_ID]:
                    errors["base"] = "wrong_workspace"
                elif info["userId"] != data[CONF_USER_ID]:
                    errors["base"] = "wrong_user"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                            CONF_USER_NAME: info["userName"],
                        },
                    )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR}),
            description_placeholders={
                "workspace": entry.title,
                "user": entry.data[CONF_USER_NAME],
            },
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server address or TLS verification, keeping the token."""
        entry = self._get_reconfigure_entry()
        data = entry.data
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            verify_ssl = user_input[CONF_VERIFY_SSL]
            # At the entry's own address, the protocol it has is asked first, and the other only
            # if that gets no answer: its HTTP port gets no TLS handshake. A new address is
            # asked as one added by hand, with the port of an entry over http as an HTTP port
            # whatever the host.
            if (host, port) == (data[CONF_HOST], data[CONF_PORT]):
                protocols = HTTP_FIRST if data[CONF_PROTOCOL] == "http" else HTTPS_FIRST
            else:
                protocols = self._probe_order(
                    port, data[CONF_PORT] if data[CONF_PROTOCOL] == "http" else None
                )
            try:
                # The kept token goes only to the entry's own hub, at its new address.
                protocol, document, info = await self._async_validate(
                    host,
                    port,
                    verify_ssl,
                    data[CONF_API_TOKEN],
                    data.get(CONF_HUB_ID),
                    protocols=protocols,
                )
            except Exception as err:  # noqa: BLE001 - mapped to a form error
                errors["base"] = self._error_key(err)
            else:
                await self.async_set_unique_id(info["workspaceId"])
                self._abort_if_unique_id_mismatch(reason="wrong_workspace")
                if info["userId"] != data[CONF_USER_ID]:
                    errors["base"] = "wrong_user"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_PROTOCOL: protocol,
                            CONF_VERIFY_SSL: verify_ssl,
                            **_hub_data(document),
                        },
                    )
            host_default, port_default, verify_default = host, port, verify_ssl
        else:
            host_default = data[CONF_HOST]
            port_default = data[CONF_PORT]
            verify_default = data[CONF_VERIFY_SSL]
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                _server_schema(host_default, port_default, verify_default)
            ),
            errors=errors,
        )
