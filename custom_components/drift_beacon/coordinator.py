"""Push coordinator: owns the WebSocket connection, the workspace state and the actions."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DriftBeaconAuthError,
    DriftBeaconClient,
    DriftBeaconError,
    DriftBeaconNoAnswerError,
    DriftBeaconRpcError,
    DriftBeaconWorkspaceNotFoundError,
)
from .const import (
    CONF_API_TOKEN,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_WORKSPACE_ID,
    DOMAIN,
    PLUGIN_COMMAND_TIMEOUT,
    REASON_NO_LIVE_SESSION,
    REASON_NOT_PINNED,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    REQUEST_TIMEOUT,
    STABLE_CONNECTION_TIME,
    UNAVAILABLE_GRACE_PERIOD,
    WORKSPACE_MISSING_RETRY_DELAY,
)
from .devices import DeviceManager
from .events import EventBuilder, Focus, focus_of
from .models import PointMark, ScheduleTrigger, WorkspaceState, apply_message

_LOGGER = logging.getLogger(__name__)

type DriftBeaconConfigEntry = ConfigEntry[DriftBeaconCoordinator]

ISSUE_WORKSPACE_NOT_FOUND = "workspace_not_found"

# How many schedule occurrences are remembered, to announce one the server repeats only once.
REMEMBERED_OCCURRENCES = 256

# Plugin command failures that are the call's fault, or mean the plugin can't take it here.
REJECTED_CODES = frozenset(
    {"invalid", "not-found", "not-installed", "disabled", "incompatible"}
)
# The command may have run: say so, so nobody runs it twice by mistake.
MAY_HAVE_RUN_CODES = frozenset({"timeout", "stopped"})


def workspace_issue_id(entry_id: str) -> str:
    """Repair issue raised while an entry's workspace cannot be found."""
    return f"{ISSUE_WORKSPACE_NOT_FOUND}_{entry_id}"


class IdentityMismatchError(DriftBeaconError):
    """The token now resolves to a different workspace or user than the entry was set up for."""


class DriftBeaconCoordinator(DataUpdateCoordinator[WorkspaceState]):
    """Holds the workspace state; the server pushes every change, nothing is polled."""

    config_entry: DriftBeaconConfigEntry

    def __init__(self, hass: HomeAssistant, entry: DriftBeaconConfigEntry) -> None:
        """Set up state; call :meth:`async_start` to connect."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=None,
        )
        self.workspace_id: str = entry.data[CONF_WORKSPACE_ID]
        self.user_id: str = entry.data[CONF_USER_ID]
        self.devices = DeviceManager(hass, entry, self.workspace_id)
        self._client: DriftBeaconClient | None = None
        self._stream: AsyncIterator[list[dict[str, Any]]] | None = None
        self._cancel_unavailable: CALLBACK_TYPE | None = None
        self._last_focus: Focus | None = None
        # Ids of the schedule occurrences already announced, across reconnects.
        self._announced_occurrences: deque[str] = deque(maxlen=REMEMBERED_OCCURRENCES)
        self._closing = False
        # Where the workspace's plugin commands go (the entry's setup sets it).
        self.on_plugin_commands: Callable[[list[Any]], None] | None = None
        self._events = EventBuilder(
            self.workspace_id,
            lambda: self.devices.workspace_device_id,
            self.devices.activity_device_id,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Connect and apply the first snapshot, then keep the connection in the background.

        Raises the config entry exceptions Home Assistant expects from setup.
        """
        try:
            await self._async_open()
        except (DriftBeaconAuthError, IdentityMismatchError) as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except DriftBeaconWorkspaceNotFoundError as err:
            # The server also answers 404 while it cannot load the workspace, which can recover.
            self._async_create_workspace_issue()
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="workspace_not_found"
            ) from err
        except (DriftBeaconError, TimeoutError) as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err

        self.config_entry.async_on_unload(
            async_at_started(self.hass, self._async_fire_initial_focus)
        )
        self.config_entry.async_create_background_task(
            self.hass, self._async_run(), f"{DOMAIN} {self.workspace_id} connection"
        )

    async def _async_update_data(self) -> WorkspaceState:
        """Serve ``homeassistant.update_entity``: the pushed state is always current."""
        if self.data is None or self._client is None or not self._client.connected:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="not_connected"
            )
        return self.data

    async def async_shutdown(self) -> None:
        """Close the connection when the entry unloads."""
        self._closing = True
        await super().async_shutdown()
        self._cancel_unavailable_timer()
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _new_client(self) -> DriftBeaconClient:
        data = self.config_entry.data
        return DriftBeaconClient(
            async_get_clientsession(self.hass, verify_ssl=data[CONF_VERIFY_SSL]),
            protocol=data[CONF_PROTOCOL],
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            api_token=data[CONF_API_TOKEN],
        )

    async def _async_open(self) -> None:
        """Connect, subscribe and apply the chunk carrying the Snapshot."""
        client = self._new_client()
        try:
            await client.connect()
            stream = client.subscribe(plugin_commands=True, schedule_events=True)
            async with asyncio.timeout(REQUEST_TIMEOUT):
                first = await anext(stream)
            if not first or first[0].get("_tag") != "Snapshot":
                raise DriftBeaconError("Subscription did not start with a Snapshot")
            self._check_identity(first[0])
        except BaseException:
            await client.close()
            raise
        self._client, self._stream = client, stream
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self._apply_chunk(first)

    def _check_identity(self, snapshot: dict[str, Any]) -> None:
        if (
            snapshot.get("workspaceId") != self.workspace_id
            or snapshot.get("userId") != self.user_id
        ):
            raise IdentityMismatchError(
                "The access token now belongs to a different workspace or user"
            )

    async def _async_run(self) -> None:
        """Consume the stream; reconnect with backoff until auth fails or the entry unloads."""
        attempt = 0
        opened_at = time.monotonic()
        while True:
            if self._stream is not None:
                try:
                    async for chunk in self._stream:
                        try:
                            self._apply_chunk(chunk)
                        except IdentityMismatchError:
                            raise
                        except Exception:
                            # A bug in handling one update must not cost the connection.
                            _LOGGER.exception("Error applying a Drift Beacon update")
                except DriftBeaconError as err:
                    _LOGGER.debug("Drift Beacon stream ended: %s", err)
                finally:
                    if self._client is not None:
                        await self._client.close()
                    self._client = self._stream = None
                if self._closing:
                    return
                self._async_connection_lost()
                # Only a connection that stayed up resets the backoff, so a server that
                # accepts and immediately drops the subscription is not hammered.
                if time.monotonic() - opened_at >= STABLE_CONNECTION_TIME:
                    attempt = 0

            delay = min(RECONNECT_MIN_DELAY * 2**attempt, RECONNECT_MAX_DELAY)
            delay *= random.uniform(0.8, 1.2)
            attempt += 1
            await asyncio.sleep(delay)
            try:
                await self._async_open()
            except DriftBeaconAuthError, IdentityMismatchError:
                _LOGGER.warning(
                    "Drift Beacon rejected the access token; reauthentication required"
                )
                self._async_mark_unavailable()
                self.config_entry.async_start_reauth(self.hass)
                return
            except DriftBeaconWorkspaceNotFoundError:
                self._async_create_workspace_issue()
                await asyncio.sleep(WORKSPACE_MISSING_RETRY_DELAY)
            except (DriftBeaconError, TimeoutError) as err:
                log = _LOGGER.warning if attempt == 1 else _LOGGER.debug
                log("Cannot reconnect to Drift Beacon (attempt %d): %s", attempt, err)
            else:
                if self._closing:
                    # Unloaded while reconnecting; the snapshot was not applied.
                    if self._client is not None:
                        await self._client.close()
                    self._client = self._stream = None
                    return
                opened_at = time.monotonic()
                if attempt > 1:
                    _LOGGER.info("Reconnected to Drift Beacon")

    @property
    def _issue_id(self) -> str:
        return workspace_issue_id(self.config_entry.entry_id)

    @callback
    def _async_create_workspace_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_WORKSPACE_NOT_FOUND,
            translation_placeholders={"title": self.config_entry.title},
        )

    @callback
    def _async_connection_lost(self) -> None:
        """Keep entities available briefly, so a quick reconnect does not flap them."""
        self._cancel_unavailable_timer()
        self._cancel_unavailable = async_call_later(
            self.hass, UNAVAILABLE_GRACE_PERIOD, self._async_grace_expired
        )

    @callback
    def _async_grace_expired(self, _now: Any) -> None:
        self._cancel_unavailable = None
        self._async_mark_unavailable()

    @callback
    def _async_mark_unavailable(self) -> None:
        self._cancel_unavailable_timer()
        if self.last_update_success:
            self.async_set_update_error(DriftBeaconError("Disconnected"))

    def _cancel_unavailable_timer(self) -> None:
        if self._cancel_unavailable is not None:
            self._cancel_unavailable()
            self._cancel_unavailable = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @callback
    def _apply_chunk(self, messages: list[dict[str, Any]]) -> None:
        """Reduce one chunk, publish the result once, then fire events and sync devices."""
        if self._closing:
            return
        prev = self.data
        state = prev
        full = False
        commands: list[Any] | None = None
        for msg in messages:
            try:
                if msg.get("_tag") == "Snapshot":
                    self._check_identity(msg)
                    state = WorkspaceState.from_snapshot(msg)
                    full = True
                elif msg.get("_tag") == "PluginCommands":
                    listed = msg["commands"]
                    if not isinstance(listed, list):
                        raise TypeError("commands is not a list")
                    commands = listed
                elif state is not None:
                    state = apply_message(state, msg)
            except IdentityMismatchError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError) as err:
                # One malformed message must not cost the connection.
                _LOGGER.warning(
                    "Ignoring malformed %s message: %r", msg.get("_tag"), err
                )
        if state is None:
            return

        marks, triggers = state.marks, state.schedule_triggers
        if marks or triggers:
            state = replace(state, marks=(), schedule_triggers=())
        self._cancel_unavailable_timer()
        if state is not prev or not self.last_update_success:
            self.async_set_updated_data(state)
        # Events first: they look up device ids that the sync may remove.
        self._async_fire_events(prev, state, marks)
        self._async_fire_schedule_triggers(prev, state, triggers)
        self.devices.async_sync(prev, state, full=full)
        # A chunk without a list (an older server, or one that doesn't know it yet) keeps the last.
        if commands is not None and self.on_plugin_commands is not None:
            try:
                self.on_plugin_commands(commands)
            except Exception:
                _LOGGER.exception("Error updating Drift Beacon plugin services")

    @callback
    def _async_fire_events(
        self,
        prev: WorkspaceState | None,
        state: WorkspaceState,
        marks: tuple[PointMark, ...],
    ) -> None:
        for event_type, data in self._events.transitions(prev, state, marks):
            self.hass.bus.async_fire(event_type, data)
        if self.hass.state is not CoreState.running:
            # The first focus is announced once Home Assistant has started, when automations
            # are listening (see _async_fire_initial_focus).
            return
        focus = focus_of(state)
        if self._last_focus is None or focus != self._last_focus:
            self.hass.bus.async_fire(
                *self._events.focus_changed(
                    focus,
                    self._last_focus,
                    state,
                    initial=self._last_focus is None,
                )
            )
            self._last_focus = focus

    @callback
    def _async_fire_schedule_triggers(
        self,
        prev: WorkspaceState | None,
        state: WorkspaceState,
        triggers: tuple[ScheduleTrigger, ...],
    ) -> None:
        """Announce the schedules that fired: once per occurrence, and only from a message.

        Nothing here is derived from state, so a reconnect's Snapshot announces none. They go
        last because the server sends a trigger after the changes it made: the events for those
        have fired by now, in this chunk or an earlier one.
        """
        for trigger in triggers:
            if trigger.id in self._announced_occurrences:
                _LOGGER.debug("Ignoring repeated schedule occurrence %s", trigger.id)
                continue
            try:
                event = self._events.schedule_triggered(trigger, state, prev)
            except Exception:
                # One trigger that can't be announced must not cost the ones after it, and isn't
                # remembered as announced.
                _LOGGER.exception(
                    "Could not announce schedule occurrence %s", trigger.id
                )
                continue
            self._announced_occurrences.append(trigger.id)
            self.hass.bus.async_fire(*event)

    @callback
    def _async_fire_initial_focus(self, _hass: HomeAssistant) -> None:
        """Announce the current focus once, so lights resync after a restart."""
        if self.data is None or self._last_focus is not None:
            return
        focus = focus_of(self.data)
        self.hass.bus.async_fire(
            *self._events.focus_changed(focus, None, self.data, initial=True)
        )
        self._last_focus = focus

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _async_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        ignore: tuple[str, ...] = (),
        timeout: float = REQUEST_TIMEOUT,
        no_answer: Callable[[str], HomeAssistantError] | None = None,
    ) -> Any:
        """Call ``method``; ``no_answer`` builds the error for a request sent but not answered."""
        client = self._client
        if client is None or not client.connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            )
        try:
            return await client.rpc(method, params, timeout=timeout)
        except DriftBeaconRpcError as err:
            if err.reason in ignore:
                return None
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={
                    "action": method,
                    "reason": err.reason,
                    "message": err.message,
                },
            ) from err
        except DriftBeaconError as err:
            if no_answer is not None and isinstance(err, DriftBeaconNoAnswerError):
                raise no_answer(str(err)) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            ) from err

    async def async_track(self, activity_id: str) -> None:
        """Stop a live span, start an idle one, or mark a point activity."""
        await self._async_rpc("TrackActivity", {"activityId": activity_id})

    async def async_start_session(self, activity_id: str) -> None:
        """Start a span activity (restarts it if already live; callers guard)."""
        await self._async_rpc("StartSession", {"activityId": activity_id})

    async def async_stop_session(self, activity_id: str) -> None:
        """Stop the live session of an activity; nothing live is not an error."""
        await self._async_rpc(
            "StopSession", {"activityId": activity_id}, ignore=(REASON_NO_LIVE_SESSION,)
        )

    async def async_pause_session(self, activity_id: str) -> None:
        """End an activity's live session and pin it; nothing live is not an error."""
        await self._async_rpc(
            "PauseSession",
            {"activityId": activity_id},
            ignore=(REASON_NO_LIVE_SESSION,),
        )

    async def async_mark(self, activity_id: str) -> None:
        """Record one occurrence of a point activity."""
        await self._async_rpc("Mark", {"activityId": activity_id})

    async def async_pin(self, activity_id: str) -> None:
        """Pin an activity; the previous pin moves to the front of the queue."""
        await self._async_rpc("PinActivity", {"activityId": activity_id})

    async def async_unpin(self, activity_id: str) -> None:
        """Unpin an activity if it is the pinned one."""
        await self._async_rpc(
            "UnpinActivity", {"activityId": activity_id}, ignore=(REASON_NOT_PINNED,)
        )

    async def async_queue(self, activity_id: str) -> None:
        """Add an activity to the back of the queue."""
        await self._async_rpc("QueueActivity", {"activityId": activity_id})

    async def async_stop_current_session(self) -> None:
        """Stop whatever session is live for the connection user."""
        await self._async_rpc("StopSession")

    async def async_pause_current_session(self) -> None:
        """Pause whatever session is live for the connection user."""
        await self._async_rpc("PauseSession")

    async def async_run_plugin_command(
        self, plugin: str, command: str, data: dict[str, Any]
    ) -> Any:
        """Run a plugin's command in this workspace and return its output."""
        names = {"plugin": plugin, "command": command}

        def may_have_run(message: str) -> HomeAssistantError:
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="plugin_command_may_have_run",
                translation_placeholders={**names, "message": message},
            )

        # An older server doesn't know the method: its failure is action_failed.
        answer = await self._async_rpc(
            "RunPluginCommand",
            {"plugin": plugin, "command": command, "input": data},
            timeout=PLUGIN_COMMAND_TIMEOUT,
            no_answer=may_have_run,
        )
        if isinstance(answer, dict) and answer.get("_tag") == "Ok":
            return answer.get("output")
        failure = answer if isinstance(answer, dict) else {}
        code = str(failure.get("code") or "failed")
        message = str(failure.get("message") or "no explanation")
        if code in MAY_HAVE_RUN_CODES:
            raise may_have_run(message)
        placeholders = {**names, "code": code, "message": message}
        if code in REJECTED_CODES:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="plugin_command_rejected",
                translation_placeholders=placeholders,
            )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="plugin_command_failed",
            translation_placeholders=placeholders,
        )
