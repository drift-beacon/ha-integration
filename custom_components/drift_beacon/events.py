"""Derive Home Assistant bus events from the change between two workspace states.

Events are computed from whole chunks rather than single messages: the server sends a switch
between activities as ``SessionEnded`` then ``SessionStarted`` in one chunk, and deriving from the
settled state turns that into one focus change instead of a stop followed by a start.

Marks and schedule triggers are not derived: each comes from one message and is one event. A
Snapshot holds neither, so a reconnect reports none.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .const import (
    ATTR_ACTIVITY_DEVICE_ID,
    ATTR_ACTIVITY_ID,
    ATTR_ACTIVITY_NAME,
    ATTR_BEHAVIOR,
    ATTR_COLOR,
    ATTR_INITIAL,
    ATTR_KIND,
    ATTR_MARKED_AT,
    ATTR_MEMBER_IDS,
    ATTR_OCCURRENCE_ID,
    ATTR_PINNED_AT,
    ATTR_PREVIOUS_ACTIVITY_ID,
    ATTR_PREVIOUS_STATE,
    ATTR_SCHEDULE_ID,
    ATTR_SESSION_ID,
    ATTR_STARTED_AT,
    ATTR_STATE,
    ATTR_TRIGGERED_AT,
    ATTR_WORKSPACE_DEVICE_ID,
    ATTR_WORKSPACE_ID,
    EVENT_ACTIVITY_MARKED,
    EVENT_ACTIVITY_PINNED,
    EVENT_ACTIVITY_UNPINNED,
    EVENT_FOCUS_CHANGED,
    EVENT_SCHEDULE_TRIGGERED,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_STOPPED,
    FOCUS_IDLE,
    FOCUS_LIVE,
    FOCUS_PINNED,
)
from .models import PointMark, ScheduleTrigger, WorkspaceState


@dataclass(frozen=True, slots=True)
class Focus:
    """What the user is focused on: a live session beats a pin, which beats nothing.

    The colour is part of it, so recolouring the focused activity updates lights too.
    """

    state: str
    activity_id: str | None
    color: tuple[int, ...] | None = None


def focus_of(state: WorkspaceState) -> Focus:
    """Return the focus for a state."""
    if state.live_session is not None:
        mode, activity_id = FOCUS_LIVE, state.live_session.activity_id
    elif state.pinned is not None:
        mode, activity_id = FOCUS_PINNED, state.pinned.activity_id
    else:
        return Focus(FOCUS_IDLE, None)
    activity = state.activity(activity_id)
    color = activity.color if activity else None
    return Focus(mode, activity_id, tuple(color) if color else None)


type Event = tuple[str, dict[str, Any]]


class EventBuilder:
    """Builds event payloads, resolving activity names and device ids."""

    def __init__(
        self,
        workspace_id: str,
        workspace_device_id: Callable[[], str | None],
        activity_device_id: Callable[[str], str | None],
    ) -> None:
        """Bind the workspace identity and the device lookups."""
        self._workspace_id = workspace_id
        self._workspace_device_id = workspace_device_id
        self._activity_device_id = activity_device_id

    @property
    def _base(self) -> dict[str, Any]:
        return {
            ATTR_WORKSPACE_ID: self._workspace_id,
            ATTR_WORKSPACE_DEVICE_ID: self._workspace_device_id(),
        }

    def activity_data(
        self, activity_id: str | None, *states: WorkspaceState | None
    ) -> dict[str, Any]:
        """Describe an activity, looking it up in the newest state that still has it.

        An activity deleted or archived in the same chunk is still named from the older state.
        """
        activity = next(
            (a for s in states if s and (a := s.activity(activity_id))), None
        )
        return {
            ATTR_ACTIVITY_ID: activity_id,
            ATTR_ACTIVITY_DEVICE_ID: self._activity_device_id(activity_id)
            if activity_id
            else None,
            ATTR_ACTIVITY_NAME: activity.name if activity else None,
            ATTR_COLOR: activity.color if activity else None,
        }

    def transitions(
        self,
        prev: WorkspaceState | None,
        new: WorkspaceState,
        marks: tuple[PointMark, ...],
    ) -> list[Event]:
        """Session, pin and mark events between two states.

        With no previous state (the first snapshot) nothing has transitioned, so only marks are
        reported. After a reconnect ``prev`` is the last state seen, so changes made while
        disconnected are delivered.
        """
        events: list[Event] = []
        if prev is not None:
            before, after = prev.live_session, new.live_session
            if before is not None and (after is None or after.id != before.id):
                events.append(
                    (
                        EVENT_SESSION_STOPPED,
                        {
                            **self._base,
                            **self.activity_data(before.activity_id, new, prev),
                            ATTR_SESSION_ID: before.id,
                            ATTR_STARTED_AT: before.started_at,
                        },
                    )
                )
            if after is not None and (before is None or before.id != after.id):
                events.append(
                    (
                        EVENT_SESSION_STARTED,
                        {
                            **self._base,
                            **self.activity_data(after.activity_id, new, prev),
                            ATTR_SESSION_ID: after.id,
                            ATTR_STARTED_AT: after.started_at,
                            ATTR_MEMBER_IDS: list(after.member_ids),
                        },
                    )
                )

            pin_before, pin_after = prev.pinned, new.pinned
            before_id = pin_before.activity_id if pin_before else None
            after_id = pin_after.activity_id if pin_after else None
            if before_id is not None and before_id != after_id:
                events.append(
                    (
                        EVENT_ACTIVITY_UNPINNED,
                        {**self._base, **self.activity_data(before_id, new, prev)},
                    )
                )
            if pin_after is not None and after_id != before_id:
                events.append(
                    (
                        EVENT_ACTIVITY_PINNED,
                        {
                            **self._base,
                            **self.activity_data(after_id, new, prev),
                            ATTR_PINNED_AT: pin_after.pinned_at,
                        },
                    )
                )

        events.extend(
            (
                EVENT_ACTIVITY_MARKED,
                {
                    **self._base,
                    **self.activity_data(mark.activity_id, new, prev),
                    ATTR_SESSION_ID: mark.id,
                    ATTR_MARKED_AT: mark.marked_at,
                    ATTR_MEMBER_IDS: list(mark.member_ids),
                },
            )
            for mark in marks
        )
        return events

    def schedule_triggered(
        self, trigger: ScheduleTrigger, *states: WorkspaceState | None
    ) -> Event:
        """A schedule that pinned or queued its activity.

        The kind and behavior are passed on as the server sent them, known to this client or not.
        """
        return (
            EVENT_SCHEDULE_TRIGGERED,
            {
                **self._base,
                **self.activity_data(trigger.activity_id, *states),
                ATTR_SCHEDULE_ID: trigger.schedule_id,
                ATTR_OCCURRENCE_ID: trigger.id,
                ATTR_KIND: trigger.kind,
                ATTR_BEHAVIOR: trigger.behavior,
                ATTR_TRIGGERED_AT: trigger.triggered_at,
            },
        )

    def focus_changed(
        self,
        focus: Focus,
        previous: Focus | None,
        state: WorkspaceState,
        *,
        initial: bool,
    ) -> Event:
        """A focus change, or the initial focus used to resync after a restart."""
        return (
            EVENT_FOCUS_CHANGED,
            {
                **self._base,
                **self.activity_data(focus.activity_id, state),
                ATTR_STATE: focus.state,
                ATTR_PREVIOUS_STATE: previous.state if previous else None,
                ATTR_PREVIOUS_ACTIVITY_ID: previous.activity_id if previous else None,
                ATTR_INITIAL: initial,
            },
        )
