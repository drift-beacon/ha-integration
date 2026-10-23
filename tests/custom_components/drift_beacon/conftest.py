"""Fixtures: a fake Drift Beacon server speaking the real /api/ws JSON-RPC protocol."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.drift_beacon.const import (
    CONF_API_TOKEN,
    CONF_HUB_ID,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_WORKSPACE_ID,
    DOMAIN,
)

WORKSPACE_ID = "ws-1"
USER_ID = "user-1"
TOKEN = "crt_token"
HUB_ID = "hub"


def activity(
    activity_id: str,
    name: str,
    tracking_type: str = "span",
    **overrides: Any,
) -> dict[str, Any]:
    """A wire ``IntegrationActivityData`` record."""
    return {
        "id": activity_id,
        "name": name,
        "description": None,
        "categoryId": None,
        "labelIds": [],
        "sortOrder": 0,
        "color": "#336699",
        "icon": "mdi:book",
        "trackingType": tracking_type,
        "archived": False,
        "unit": None,
        "progress": {"current": 0, "target": None},
        "showCumulativeTimer": False,
        **overrides,
    }


def live_session(session_id: str, activity_id: str) -> dict[str, Any]:
    """A wire ``IntegrationLiveSessionData`` record."""
    return {
        "id": session_id,
        "activityId": activity_id,
        "memberIds": [USER_ID],
        "startTime": "2026-09-24T10:00:00.000Z",
    }


def schedule_trigger(
    schedule_id: str, activity_id: str, **overrides: Any
) -> dict[str, Any]:
    """A wire ``IntegrationScheduleTriggerData`` record: a daily schedule pinning its activity."""
    return {
        "id": f"{schedule_id}:2026-09-24",
        "scheduleId": schedule_id,
        "activityId": activity_id,
        "kind": "recurring",
        "behavior": "pin",
        "triggeredAt": "2026-09-24T09:00:00.000Z",
        **overrides,
    }


def make_snapshot(**overrides: Any) -> dict[str, Any]:
    """A Snapshot with a timed activity (Reading) and a point activity (Water)."""
    return {
        "workspaceId": WORKSPACE_ID,
        "workspaceName": "Home",
        "userId": USER_ID,
        "userName": "Rich",
        "activities": [
            activity("reading", "Reading"),
            activity("water", "Water", "point", unit="glasses"),
        ],
        "categories": [],
        "labels": [],
        "liveSessions": [],
        "pinnedActivity": None,
        "queuedActivities": [],
        "queueAutoAdvanceEnabled": True,
        "laterActivities": [],
        **overrides,
    }


@dataclass
class Connection:
    """A TCP connection the fake hub accepted: how it opened, and the requests it carried."""

    opening: bytes = b""
    requests: int = 0

    @property
    def tls_hello(self) -> bool:
        """It opened with a TLS handshake record (type 22, version 3.x): a ClientHello."""
        return self.opening[:2] == b"\x16\x03"


class _Recording(asyncio.Protocol):
    """aiohttp's handler for one connection, noting how the connection opened."""

    def __init__(self, handler: asyncio.Protocol, connection: Connection) -> None:
        self._handler = handler
        self._connection = connection

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._handler.connection_made(transport)

    def data_received(self, data: bytes) -> None:
        if len(self._connection.opening) < 2:
            self._connection.opening += data[: 2 - len(self._connection.opening)]
        self._handler.data_received(data)

    def eof_received(self) -> bool | None:
        return self._handler.eof_received()

    def connection_lost(self, exc: Exception | None) -> None:
        self._handler.connection_lost(exc)

    def pause_writing(self) -> None:
        self._handler.pause_writing()

    def resume_writing(self) -> None:
        self._handler.resume_writing()


class _RecordingSite(web.BaseSite):
    """A site on a free port of 127.0.0.1 that records every connection it accepts."""

    def __init__(self, runner: web.BaseRunner, fake: FakeDriftBeacon) -> None:
        super().__init__(runner)
        self._fake = fake

    @property
    def name(self) -> str:
        return "fake Drift Beacon hub"

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        await super().start()
        server = self._runner.server
        assert server is not None
        self._server = await asyncio.get_running_loop().create_server(
            lambda: self._fake._accept(server()), "127.0.0.1", 0
        )


class FakeDriftBeacon:
    """An aiohttp server on 127.0.0.1 implementing the integration endpoint.

    RPCs are recorded in ``calls``; ``results`` and ``errors`` script their replies, and
    ``push`` sends a chunk to every subscriber. As on the server, a ``ScheduleTriggered``
    reaches only the subscribers that asked for schedule events.

    Every TCP connection it accepts is recorded in ``accepted``, so a test can see what
    reached its plain HTTP port, not only what the integration meant to send: ``tls_hellos``
    counts the TLS handshakes that reached it, and ``unhandled`` the connections that carried
    no request it could read.
    """

    def __init__(self) -> None:
        """Start with the default snapshot."""
        self.snapshot = make_snapshot()
        self.handshake_status: int | None = None
        # The id /api/info gives, and what else it says (ports, addresses).
        self.hub_id = HUB_ID
        self.info: dict[str, Any] = {}
        self.info_requests = 0
        # Seconds /api/info takes to answer.
        self.info_delay = 0.0
        # A hub from before /api/info: it answers only the device status document.
        self.legacy_status_only = False
        # Another server at the port: it answers neither document.
        self.not_a_hub = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: dict[str, Any | Callable[[dict[str, Any]], Any]] = {
            "GetConnectionInfo": lambda _: {
                "workspaceId": self.snapshot["workspaceId"],
                "workspaceName": self.snapshot["workspaceName"],
                "userId": self.snapshot["userId"],
                "userName": self.snapshot["userName"],
            }
        }
        self.errors: dict[str, str] = {}
        # Sent after the Snapshot, in its chunk, to clients that ask for plugin commands.
        self.plugin_commands: list[dict[str, Any]] | None = None
        # Sent after those, to clients that ask for plugin controls: the catalogue, then the
        # values. None: the server doesn't know them yet, and sends neither.
        self.plugin_controls: list[dict[str, Any]] | None = None
        self.plugin_values: list[dict[str, Any]] | None = None
        # Sent last in the Snapshot's chunk: what happened just as the client subscribed.
        self.after_snapshot: list[dict[str, Any]] = []
        self.subscribers: dict[web.WebSocketResponse, int] = {}
        self._schedule_listeners: set[web.WebSocketResponse] = set()
        self._replies: set[asyncio.Task[None]] = set()
        self.connections = 0
        self.handshakes = 0
        self.accepted: list[Connection] = []
        self._connection_of: dict[asyncio.Protocol, Connection] = {}
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def tls_hellos(self) -> int:
        """How many connections opened with a TLS ClientHello."""
        return sum(connection.tls_hello for connection in self.accepted)

    @property
    def unhandled(self) -> int:
        """How many accepted connections carried no request the server could read."""
        return sum(connection.requests == 0 for connection in self.accepted)

    async def start(self) -> None:
        """Listen on a free local port."""
        app = web.Application(middlewares=[self._count_request])
        app.router.add_get("/api/ws", self._ws)
        app.router.add_get("/api/info", self._info)
        app.router.add_get("/api/device/status", self._status)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = _RecordingSite(self._runner, self)
        await site.start()
        self.port = site.port

    def _accept(self, handler: asyncio.Protocol) -> asyncio.Protocol:
        connection = Connection()
        self.accepted.append(connection)
        self._connection_of[handler] = connection
        return _Recording(handler, connection)

    @web.middleware
    async def _count_request(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        self._connection_of[request.protocol].requests += 1
        return await handler(request)

    async def stop(self) -> None:
        """Close every socket and stop listening."""
        for reply in self._replies:
            reply.cancel()
        await self.disconnect()
        if self._runner:
            await self._runner.cleanup()

    async def _info(self, request: web.Request) -> web.Response:
        self.info_requests += 1
        if self.info_delay:
            await asyncio.sleep(self.info_delay)
        if self.legacy_status_only or self.not_a_hub:
            raise web.HTTPNotFound
        return web.json_response(
            {
                "id": self.hub_id,
                "name": "Hub",
                "version": "0.2.51",
                "protocol": 1,
                "startup": "ready",
                **self.info,
            }
        )

    async def _status(self, request: web.Request) -> web.Response:
        if not self.legacy_status_only:
            raise web.HTTPNotFound
        return web.json_response(
            {"status": "healthy", "device": {"id": "hub", "name": "Hub"}}
        )

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        self.handshakes += 1
        if self.handshake_status is not None:
            return web.json_response({"error": "nope"}, status=self.handshake_status)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                frame = json.loads(message.data)
                await self._handle(ws, frame)
        finally:
            self.subscribers.pop(ws, None)
            self._schedule_listeners.discard(ws)
        return ws

    async def _handle(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        method, request_id = frame["method"], frame["id"]
        params = frame.get("params") or {}
        self.calls.append((method, params))
        if method == "Subscribe":
            self.subscribers[ws] = request_id
            first = [{"_tag": "Snapshot", **copy.deepcopy(self.snapshot)}]
            if (
                params.get("pluginCommands") is True
                and self.plugin_commands is not None
            ):
                first.append(
                    {
                        "_tag": "PluginCommands",
                        "commands": copy.deepcopy(self.plugin_commands),
                    }
                )
            if params.get("pluginControls") is True:
                if self.plugin_controls is not None:
                    first.append(
                        {
                            "_tag": "PluginControls",
                            "controls": copy.deepcopy(self.plugin_controls),
                        }
                    )
                if self.plugin_values is not None:
                    first.append(
                        {
                            "_tag": "PluginValues",
                            "plugins": copy.deepcopy(self.plugin_values),
                        }
                    )
            if params.get("scheduleEvents") is True:
                self._schedule_listeners.add(ws)
            first.extend(self._messages_for(ws, copy.deepcopy(self.after_snapshot)))
            await ws.send_json(
                {"jsonrpc": "2.0", "chunk": True, "id": request_id, "result": first}
            )
            return
        if method in self.errors:
            reason = self.errors[method]
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "_tag": "Cause",
                        "code": 0,
                        "message": f"{reason} happened",
                        "data": [
                            {
                                "_tag": "Fail",
                                "error": {
                                    "_tag": "IntegrationWsError",
                                    "reason": reason,
                                    "message": f"{reason} happened",
                                },
                            }
                        ],
                    },
                }
            )
            return
        result = self.results.get(method)
        if callable(result):
            result = result(params)
        if isinstance(result, asyncio.Future):
            # Answer when the test resolves it (or never), without holding up later frames.
            reply = asyncio.create_task(self._reply_later(ws, request_id, result))
            self._replies.add(reply)
            reply.add_done_callback(self._replies.discard)
            return
        await ws.send_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _reply_later(
        self, ws: web.WebSocketResponse, request_id: int, result: asyncio.Future[Any]
    ) -> None:
        value = await result
        if not ws.closed:
            await ws.send_json({"jsonrpc": "2.0", "id": request_id, "result": value})

    async def push_plugin_commands(self, commands: list[dict[str, Any]]) -> None:
        """Change the plugin commands, and send them to every subscriber."""
        self.plugin_commands = commands
        await self.push({"_tag": "PluginCommands", "commands": copy.deepcopy(commands)})

    async def push_plugin_controls(self, controls: list[dict[str, Any]]) -> None:
        """Change the catalogue of plugin controls, and send it to every subscriber."""
        self.plugin_controls = controls
        await self.push({"_tag": "PluginControls", "controls": copy.deepcopy(controls)})

    async def push_plugin_values(self, plugins: list[dict[str, Any]]) -> None:
        """Change every plugin's values, and send them to every subscriber."""
        self.plugin_values = plugins
        await self.push({"_tag": "PluginValues", "plugins": copy.deepcopy(plugins)})

    def _messages_for(
        self, ws: web.WebSocketResponse, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop the schedule events of a subscriber that didn't ask for them."""
        if ws in self._schedule_listeners:
            return messages
        return [m for m in messages if m.get("_tag") != "ScheduleTriggered"]

    async def push(self, *messages: dict[str, Any]) -> None:
        """Send one chunk holding ``messages`` to every subscriber."""
        for ws, request_id in list(self.subscribers.items()):
            chunk = self._messages_for(ws, list(messages))
            if not chunk:
                continue
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "chunk": True,
                    "id": request_id,
                    "result": chunk,
                }
            )

    async def send_raw(self, text: str) -> None:
        """Send an arbitrary text frame to every subscriber."""
        for ws in list(self.subscribers):
            await ws.send_str(text)

    async def end_stream(self) -> None:
        """Send the subscription's Exit, as the server does on Unsubscribe."""
        for ws, request_id in list(self.subscribers.items()):
            await ws.send_json({"jsonrpc": "2.0", "id": request_id})

    async def disconnect(self) -> None:
        """Drop every connection."""
        for ws in list(self.subscribers):
            await ws.close()
        self.subscribers.clear()
        self._schedule_listeners.clear()

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        """Params of every call to ``method``."""
        return [params for name, params in self.calls if name == method]


def device_for(hass: HomeAssistant, identifier: str) -> dr.DeviceEntry | None:
    """Look a Drift Beacon device up by identifier, across config entries."""
    return next(
        (
            device
            for device in dr.async_get(hass).devices
            if (DOMAIN, identifier) in device.identifiers
        ),
        None,
    )


def entity_id_for(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    """Entity id registered for a Drift Beacon unique id."""
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def wait_for(predicate: Callable[[], bool], timeout: float = 3) -> None:
    """Wait for something the WebSocket reader does outside Home Assistant's task tracking."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from this repository."""


@pytest.fixture(autouse=True)
def no_local_hub_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the config flow from probing this machine's own hub port."""
    monkeypatch.setattr(
        "custom_components.drift_beacon.config_flow.LOCAL_HUB_ADDRESSES", ()
    )


@pytest.fixture(autouse=True)
def fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnect immediately in tests."""
    monkeypatch.setattr(
        "custom_components.drift_beacon.coordinator.RECONNECT_MIN_DELAY", 0.01
    )


@pytest.fixture
async def server(socket_enabled: None) -> AsyncGenerator[FakeDriftBeacon]:
    """A running fake server (Home Assistant's test plugin blocks sockets by default)."""
    fake = FakeDriftBeacon()
    await fake.start()
    yield fake
    await fake.stop()


def make_entry(
    server: FakeDriftBeacon,
    workspace_id: str = WORKSPACE_ID,
    title: str = "Home",
    hub_id: str | None = HUB_ID,
) -> MockConfigEntry:
    """A config entry for ``workspace_id`` on ``server`` (its snapshot must name it).

    ``hub_id`` None makes an entry from before hub ids.
    """
    data: dict[str, Any] = {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: server.port,
        CONF_PROTOCOL: "http",
        CONF_VERIFY_SSL: True,
        CONF_API_TOKEN: TOKEN,
        CONF_WORKSPACE_ID: workspace_id,
        CONF_USER_ID: USER_ID,
        CONF_USER_NAME: "Rich",
    }
    if hub_id is not None:
        data[CONF_HUB_ID] = hub_id
    return MockConfigEntry(
        domain=DOMAIN, version=4, title=title, unique_id=workspace_id, data=data
    )


@pytest.fixture
def config_entry(server: FakeDriftBeacon) -> MockConfigEntry:
    """A config entry pointing at the fake server."""
    return make_entry(server)


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the entry against the fake server, and unload it afterwards."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    if config_entry.state.recoverable:
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()
