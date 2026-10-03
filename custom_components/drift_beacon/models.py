"""Immutable workspace state and the reducer that applies server stream messages to it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

_LOGGER = logging.getLogger(__name__)

type TrackingType = Literal["span", "point"]


def parse_color(value: Any) -> list[int] | None:
    """Return ``[r, g, b]`` for ``#rgb``, ``#rrggbb`` or ``#rrggbbaa`` strings, else None.

    The server does not normalise colours, so anything unexpected degrades to None rather
    than raising inside the stream handler.
    """
    if not isinstance(value, str):
        return None
    digits = value.strip().removeprefix("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    elif len(digits) == 8:
        digits = digits[:6]
    if len(digits) != 6:
        return None
    try:
        return [int(digits[i : i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Activity:
    """An active (not archived) activity, workspace-wide."""

    id: str
    name: str
    tracking_type: TrackingType
    icon: str | None
    color: list[int] | None
    description: str | None
    category_id: str | None
    unit: str | None
    # Completed-session seconds (span) or mark count (point) for the progress period.
    progress: float
    target: float | None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> Activity:
        """Parse an ``IntegrationActivityData`` record."""
        progress = data.get("progress") or {}
        tracking_type = data.get("trackingType")
        return cls(
            id=data["id"],
            name=data["name"],
            tracking_type="point" if tracking_type == "point" else "span",
            icon=data.get("icon") or None,
            color=parse_color(data.get("color")),
            description=data.get("description"),
            category_id=data.get("categoryId"),
            unit=data.get("unit"),
            progress=float(progress.get("current") or 0),
            target=progress.get("target"),
        )


@dataclass(frozen=True, slots=True)
class Category:
    """An activity category."""

    id: str
    name: str

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> Category:
        """Parse an ``IntegrationCategoryData`` record."""
        return cls(id=data["id"], name=data["name"])


@dataclass(frozen=True, slots=True)
class LiveSession:
    """The connection user's live span session."""

    id: str
    activity_id: str
    member_ids: tuple[str, ...]
    started_at: str

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> LiveSession:
        """Parse an ``IntegrationLiveSessionData`` record."""
        return cls(
            id=data["id"],
            activity_id=data["activityId"],
            member_ids=tuple(data.get("memberIds") or ()),
            started_at=data["startTime"],
        )


@dataclass(frozen=True, slots=True)
class PinnedActivity:
    """The connection user's pinned activity slot."""

    activity_id: str
    pinned_at: str

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> PinnedActivity:
        """Parse an ``IntegrationPinnedActivityData`` record."""
        return cls(activity_id=data["activityId"], pinned_at=data["pinnedAt"])


@dataclass(frozen=True, slots=True)
class PointMark:
    """A mark of a point activity by the connection user."""

    id: str
    activity_id: str
    marked_at: str
    member_ids: tuple[str, ...]

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> PointMark:
        """Parse an ``IntegrationPointMarkData`` record."""
        return cls(
            id=data["id"],
            activity_id=data["activityId"],
            marked_at=data["markedAt"],
            member_ids=tuple(data.get("memberIds") or ()),
        )


@dataclass(frozen=True, slots=True)
class ScheduleTrigger:
    """One of the connection user's schedules pinning or queueing its activity."""

    # The occurrence: the server gives a repeat of it the same id.
    id: str
    schedule_id: str
    activity_id: str
    # "once", "recurring" or "sinceLast"; the server can add kinds.
    kind: str
    # "pin", or "queue" (front of Next) or "queueBack" (its back); the server can add behaviors.
    behavior: str
    triggered_at: str

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> ScheduleTrigger:
        """Parse an ``IntegrationScheduleTriggerData`` record.

        Every field must be a string: a trigger is turned into a bus event later, outside the
        reducer's per-message error handling, so anything else is refused here. ``kind`` and
        ``behavior`` are accepted whatever string they hold.
        """
        fields = {
            name: data[key]
            for name, key in (
                ("id", "id"),
                ("schedule_id", "scheduleId"),
                ("activity_id", "activityId"),
                ("kind", "kind"),
                ("behavior", "behavior"),
                ("triggered_at", "triggeredAt"),
            )
        }
        for name, value in fields.items():
            if not isinstance(value, str):
                raise TypeError(f"{name} is not a string")
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """Everything Home Assistant knows about one workspace, for the connection user.

    Treated as immutable: the reducer returns a new instance, so the coordinator can diff the
    state before and after a chunk to derive events.
    """

    workspace_id: str
    workspace_name: str
    user_id: str
    user_name: str
    activities: MappingProxyType[str, Activity]
    categories: MappingProxyType[str, Category]
    live_session: LiveSession | None
    pinned: PinnedActivity | None
    # Marks seen since the previous chunk; the coordinator turns them into events and clears them.
    marks: tuple[PointMark, ...] = field(default=())
    # Schedules that fired since the previous chunk, carried and cleared the same way. A Snapshot
    # never holds any: the server sends each as it happens and never again.
    schedule_triggers: tuple[ScheduleTrigger, ...] = field(default=())

    @classmethod
    def from_snapshot(cls, msg: dict[str, Any]) -> WorkspaceState:
        """Build the full state from a ``Snapshot`` message."""
        live_sessions = msg.get("liveSessions") or []
        pinned = msg.get("pinnedActivity")
        return cls(
            workspace_id=msg["workspaceId"],
            workspace_name=msg.get("workspaceName") or msg["workspaceId"],
            user_id=msg["userId"],
            user_name=msg.get("userName") or msg["userId"],
            activities=MappingProxyType(
                {a["id"]: Activity.from_wire(a) for a in msg.get("activities") or []}
            ),
            categories=MappingProxyType(
                {c["id"]: Category.from_wire(c) for c in msg.get("categories") or []}
            ),
            # Starting a session ends the member's others, so there is at most one.
            live_session=LiveSession.from_wire(live_sessions[0])
            if live_sessions
            else None,
            pinned=PinnedActivity.from_wire(pinned) if pinned else None,
        )

    def activity(self, activity_id: str | None) -> Activity | None:
        """Return an activity by id, if it is active."""
        return self.activities.get(activity_id) if activity_id else None


def _with_activity(state: WorkspaceState, activity: Activity) -> WorkspaceState:
    return replace(
        state, activities=MappingProxyType({**state.activities, activity.id: activity})
    )


def apply_message(state: WorkspaceState, msg: dict[str, Any]) -> WorkspaceState:
    """Apply one incremental stream message and return the new state.

    Unknown tags are ignored so the server can add messages without breaking older clients.
    """
    match msg.get("_tag"):
        case "ActivityCreated" | "ActivityUpdated":
            return _with_activity(state, Activity.from_wire(msg["activity"]))
        case "ActivityDeleted":
            # Sent for deletion and for archiving alike.
            activities = dict(state.activities)
            activities.pop(msg["activityId"], None)
            return replace(state, activities=MappingProxyType(activities))
        case "CategoryCreated" | "CategoryUpdated":
            category = Category.from_wire(msg["category"])
            return replace(
                state,
                categories=MappingProxyType(
                    {**state.categories, category.id: category}
                ),
            )
        case "CategoryDeleted":
            categories = dict(state.categories)
            categories.pop(msg["categoryId"], None)
            return replace(state, categories=MappingProxyType(categories))
        case "SessionStarted":
            return replace(state, live_session=LiveSession.from_wire(msg["session"]))
        case "SessionEnded":
            # Only clear the slot if it still holds the session that ended.
            if state.live_session and state.live_session.id == msg["sessionId"]:
                return replace(state, live_session=None)
            return state
        case "PinnedActivityChanged":
            pinned = msg.get("pinnedActivity")
            return replace(
                state, pinned=PinnedActivity.from_wire(pinned) if pinned else None
            )
        case "PointMarked":
            return replace(
                state, marks=(*state.marks, PointMark.from_wire(msg["session"]))
            )
        case "ScheduleTriggered":
            return replace(
                state,
                schedule_triggers=(
                    *state.schedule_triggers,
                    ScheduleTrigger.from_wire(msg["trigger"]),
                ),
            )
        case _:
            return state
