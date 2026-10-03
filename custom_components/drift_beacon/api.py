"""JSON-RPC over WebSocket client for the Drift Beacon ``/api/ws`` integration endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import aiohttp
from homeassistant.helpers.json import json_dumps

from .const import HEARTBEAT_INTERVAL, REQUEST_TIMEOUT, STATUS_PATH, WS_PATH

_LOGGER = logging.getLogger(__name__)


class DriftBeaconError(Exception):
    """Base error for Drift Beacon client failures."""


class DriftBeaconConnectionError(DriftBeaconError):
    """The server could not be reached, or the connection dropped."""


class DriftBeaconNoAnswerError(DriftBeaconConnectionError):
    """A request was sent but not answered: it timed out, or the connection dropped.

    The server may have acted on it.
    """


class DriftBeaconSslError(DriftBeaconConnectionError):
    """TLS certificate verification failed."""


class DriftBeaconAuthError(DriftBeaconError):
    """The API token was rejected (401/403)."""


class DriftBeaconWorkspaceNotFoundError(DriftBeaconError):
    """The token's workspace no longer exists on the server (404)."""


class DriftBeaconRpcError(DriftBeaconError):
    """The server answered an RPC with a typed failure."""

    def __init__(self, method: str, reason: str, message: str) -> None:
        """Initialise with the server's ``IntegrationWsError`` reason."""
        super().__init__(f"{method} failed: {reason}: {message}")
        self.method = method
        self.reason = reason
        self.message = message


def base_url(protocol: str, host: str, port: int) -> str:
    """Return the HTTP base URL for a server."""
    return f"{protocol}://{host}:{port}"


def ws_url(protocol: str, host: str, port: int) -> str:
    """Return the WebSocket URL for the integration endpoint."""
    scheme = "wss" if protocol == "https" else "ws"
    return f"{scheme}://{host}:{port}{WS_PATH}"


async def async_get_server_status(
    session: aiohttp.ClientSession,
    protocol: str,
    host: str,
    port: int,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Fetch the public device status document used to recognise a Drift Beacon server.

    TLS verification follows ``session`` (``async_get_clientsession(hass, verify_ssl)``).
    """
    try:
        async with session.get(
            f"{base_url(protocol, host, port)}{STATUS_PATH}",
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
    except aiohttp.ClientConnectorCertificateError as err:
        raise DriftBeaconSslError(str(err)) from err
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise DriftBeaconConnectionError(str(err)) from err
    if not isinstance(data, dict) or not isinstance(data.get("device"), dict):
        raise DriftBeaconConnectionError("Not a Drift Beacon server")
    return data


def _rpc_failure(method: str, error: Any) -> DriftBeaconRpcError:
    """Build an error from a JSON-RPC ``error`` object.

    Typed failures arrive as ``{"_tag": "Cause", "data": [{"_tag": "Fail", "error": {...}}]}``.
    Anything else (schema defects, unknown methods) is reported as ``InternalError``.
    """
    if isinstance(error, dict):
        for part in error.get("data") or []:
            failure = part.get("error") if isinstance(part, dict) else None
            if isinstance(failure, dict) and failure.get("reason"):
                return DriftBeaconRpcError(
                    method, str(failure["reason"]), str(failure.get("message", ""))
                )
        return DriftBeaconRpcError(
            method, "InternalError", str(error.get("message", error))
        )
    return DriftBeaconRpcError(method, "InternalError", str(error))


class DriftBeaconClient:
    """One authenticated WebSocket connection.

    ``connect`` opens the socket and starts a reader task. RPC responses resolve pending
    futures; stream chunks for the subscription are queued for :meth:`subscribe`.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        protocol: str,
        host: str,
        port: int,
        api_token: str,
    ) -> None:
        """Store connection settings; nothing is opened until :meth:`connect`.

        TLS verification follows ``session`` (``async_get_clientsession(hass, verify_ssl)``).
        """
        self._session = session
        self._url = ws_url(protocol, host, port)
        self._api_token = api_token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._subscription_id: int | None = None
        self._chunks: asyncio.Queue[list[dict[str, Any]] | BaseException] = (
            asyncio.Queue()
        )

    @property
    def connected(self) -> bool:
        """Return whether the socket is open."""
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """Open the socket, mapping handshake failures to typed errors."""
        try:
            # ws_connect's own timeout only covers receive/close, so bound the handshake here.
            async with asyncio.timeout(REQUEST_TIMEOUT):
                self._ws = await self._session.ws_connect(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    heartbeat=HEARTBEAT_INTERVAL,
                    timeout=aiohttp.ClientWSTimeout(ws_close=REQUEST_TIMEOUT),
                )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise DriftBeaconAuthError(err.message) from err
            if err.status == 404:
                raise DriftBeaconWorkspaceNotFoundError(err.message) from err
            raise DriftBeaconConnectionError(
                f"Handshake failed ({err.status}): {err.message}"
            ) from err
        except aiohttp.ClientConnectorCertificateError as err:
            raise DriftBeaconSslError(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DriftBeaconConnectionError(str(err)) from err
        self._reader = asyncio.create_task(self._read_loop(), name="drift_beacon_ws")

    async def close(self) -> None:
        """Close the socket and fail everything still waiting on it."""
        # Stop the reader first: closing while it is still receiving lets aiohttp re-arm its
        # heartbeat timer after the close has cancelled it.
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            with suppress(Exception):
                await ws.close()
        self._fail_all(DriftBeaconConnectionError("Connection closed"))

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        """Call a method and return its ``result`` (None for void methods).

        Once the request is sent, a timeout or a dropped connection raises
        :class:`DriftBeaconNoAnswerError`: the server may have acted on it.
        """
        request_id, future = self._register(method)
        try:
            await self._send(method, params, request_id)
            try:
                async with asyncio.timeout(timeout):
                    return await future
            except TimeoutError as err:
                raise DriftBeaconNoAnswerError(f"{method} timed out") from err
            except DriftBeaconConnectionError as err:
                raise DriftBeaconNoAnswerError(f"{method}: {err}") from err
        finally:
            self._pending.pop(request_id, None)

    async def subscribe(
        self, *, plugin_commands: bool = False, schedule_events: bool = False
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Subscribe and yield each chunk (a list of messages); the first holds the Snapshot.

        With ``plugin_commands``, the server also sends ``PluginCommands`` messages, and with
        ``schedule_events``, ``ScheduleTriggered`` messages (servers from before either ignore
        the request).
        """
        request_id = self._allocate_id()
        self._subscription_id = request_id
        params: dict[str, Any] = {}
        if plugin_commands:
            params["pluginCommands"] = True
        if schedule_events:
            params["scheduleEvents"] = True
        await self._send("Subscribe", params, request_id)
        while True:
            item = await self._chunks.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    def _allocate_id(self) -> int:
        if not self.connected:
            raise DriftBeaconConnectionError("Not connected")
        self._next_id += 1
        return self._next_id

    def _register(self, method: str) -> tuple[int, asyncio.Future[Any]]:
        request_id = self._allocate_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        return request_id, future

    async def _send(
        self, method: str, params: dict[str, Any] | None, request_id: int
    ) -> None:
        if self._ws is None or self._ws.closed:
            raise DriftBeaconConnectionError("Not connected")
        _LOGGER.debug("RPC %s (id=%d)", method, request_id)
        try:
            # Home Assistant's encoder: dates from YAML go as ISO text, not a TypeError.
            await self._ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                    "id": request_id,
                },
                dumps=json_dumps,
            )
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as err:
            raise DriftBeaconConnectionError(str(err)) from err

    async def _read_loop(self) -> None:
        assert self._ws is not None
        error: BaseException = DriftBeaconConnectionError("Connection closed by server")
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(message.data)
                elif message.type == aiohttp.WSMsgType.ERROR:
                    error = DriftBeaconConnectionError(str(self._ws.exception()))
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the socket is gone either way
            error = DriftBeaconConnectionError(str(err))
        self._fail_all(error)

    def _dispatch(self, raw: str) -> None:
        """Route one frame. A malformed frame is logged and skipped, never fatal."""
        try:
            frame = json.loads(raw)
        except ValueError:
            _LOGGER.warning("Ignoring non-JSON frame from Drift Beacon")
            return
        for item in frame if isinstance(frame, list) else [frame]:
            if not isinstance(item, dict):
                continue
            request_id = item.get("id")
            if request_id is not None and request_id == self._subscription_id:
                self._dispatch_subscription(item)
                continue
            pending = (
                self._pending.get(request_id) if isinstance(request_id, int) else None
            )
            if pending is None:
                if "error" in item:
                    _LOGGER.warning("Drift Beacon reported an error: %s", item["error"])
                continue
            method, future = pending
            if future.done():
                continue
            if "error" in item:
                future.set_exception(_rpc_failure(method, item["error"]))
            else:
                future.set_result(item.get("result"))

    def _dispatch_subscription(self, item: dict[str, Any]) -> None:
        if item.get("chunk"):
            messages = item.get("result")
            if isinstance(messages, list):
                self._chunks.put_nowait([m for m in messages if isinstance(m, dict)])
            return
        # A non-chunk frame for the subscription id is the stream's Exit.
        self._subscription_id = None
        if "error" in item:
            self._chunks.put_nowait(_rpc_failure("Subscribe", item["error"]))
        else:
            self._chunks.put_nowait(
                DriftBeaconConnectionError("Subscription ended by server")
            )

    def _fail_all(self, error: BaseException) -> None:
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        if self._subscription_id is not None:
            self._subscription_id = None
            self._chunks.put_nowait(error)


async def async_get_connection_info(
    session: aiohttp.ClientSession,
    *,
    protocol: str,
    host: str,
    port: int,
    api_token: str,
) -> dict[str, str]:
    """Validate a token and return its ``workspaceId/workspaceName/userId/userName``."""
    client = DriftBeaconClient(
        session,
        protocol=protocol,
        host=host,
        port=port,
        api_token=api_token,
    )
    await client.connect()
    try:
        result = await client.rpc("GetConnectionInfo")
    finally:
        await client.close()
    keys = ("workspaceId", "workspaceName", "userId", "userName")
    if not isinstance(result, dict) or not all(result.get(k) for k in keys):
        raise DriftBeaconConnectionError("Invalid GetConnectionInfo response")
    return {k: str(result[k]) for k in keys}
