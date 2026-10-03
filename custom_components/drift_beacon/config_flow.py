"""Config flow for Drift Beacon: one entry per workspace, acting as the token's user."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    DriftBeaconAuthError,
    DriftBeaconConnectionError,
    DriftBeaconError,
    DriftBeaconSslError,
    DriftBeaconWorkspaceNotFoundError,
    async_get_connection_info,
    async_get_server_status,
)
from .const import (
    CONF_API_TOKEN,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_WORKSPACE_ID,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DETECTION_CANDIDATES,
    DETECTION_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PORT_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
)
TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class NoServerFound(DriftBeaconConnectionError):
    """Nothing answering as a Drift Beacon server at the address."""


def _server_schema(host: str, port: int, verify_ssl: bool) -> dict[Any, Any]:
    return {
        vol.Required(CONF_HOST, default=host): str,
        vol.Required(CONF_PORT, default=port): PORT_SELECTOR,
        vol.Required(CONF_VERIFY_SSL, default=verify_ssl): bool,
    }


class DriftBeaconConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Drift Beacon workspace from a workspace-scoped access token."""

    VERSION = 4

    def __init__(self) -> None:
        """Initialise flow state."""
        self._detected: tuple[str, int] | None = None
        self._detection_done = False

    async def _async_detect_protocol(
        self, host: str, port: int, verify_ssl: bool
    ) -> str:
        """Return ``https`` or ``http``, whichever serves Drift Beacon (https preferred)."""
        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        results = await asyncio.gather(
            *(
                async_get_server_status(
                    session, protocol, host, port, timeout=DETECTION_TIMEOUT
                )
                for protocol in ("https", "http")
            ),
            return_exceptions=True,
        )
        for protocol, result in zip(("https", "http"), results, strict=True):
            if not isinstance(result, BaseException):
                return protocol
        if isinstance(results[0], DriftBeaconSslError):
            raise results[0]
        raise NoServerFound(f"No Drift Beacon server at {host}:{port}")

    async def _async_detect_local_addon(self) -> tuple[str, int] | None:
        """Look for the Drift Beacon add-on at its usual addresses, once per flow."""
        if self._detection_done:
            return self._detected
        self._detection_done = True
        for host, port in DETECTION_CANDIDATES:
            try:
                # Only locating the server; the user's TLS choice applies when connecting.
                await self._async_detect_protocol(host, port, verify_ssl=False)
            except DriftBeaconError:
                continue
            self._detected = (host, port)
            break
        return self._detected

    async def _async_validate(
        self, host: str, port: int, verify_ssl: bool, api_token: str
    ) -> tuple[str, dict[str, str]]:
        """Return the protocol and the token's identity, or raise a DriftBeaconError."""
        protocol = await self._async_detect_protocol(host, port, verify_ssl)
        info = await async_get_connection_info(
            async_get_clientsession(self.hass, verify_ssl=verify_ssl),
            protocol=protocol,
            host=host,
            port=port,
            api_token=api_token,
        )
        return protocol, info

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
                protocol, info = await self._async_validate(
                    host, port, verify_ssl, user_input[CONF_API_TOKEN]
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
                    },
                )

        detected = await self._async_detect_local_addon()
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            verify_ssl = user_input[CONF_VERIFY_SSL]
        else:
            host, port = detected or (DEFAULT_HOST, DEFAULT_PORT)
            verify_ssl = True
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    **_server_schema(host, port, verify_ssl),
                    vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
                }
            ),
            description_placeholders={
                "detected": f"{detected[0]}:{detected[1]}" if detected else "",
            },
            errors=errors,
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
            try:
                info = await async_get_connection_info(
                    async_get_clientsession(
                        self.hass, verify_ssl=data[CONF_VERIFY_SSL]
                    ),
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
            try:
                protocol, info = await self._async_validate(
                    host, port, verify_ssl, data[CONF_API_TOKEN]
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
