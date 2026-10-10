"""Immutable workspace state and the reducer that applies server stream messages to it."""

from __future__ import annotations

import logging
from collections import Counter
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
    # The goal in the unit of ``progress``; None with no goal and for a timed activity whose
    # goal is a number of sessions.
    target: float | None
    # "duration" or "count" (marks, or sessions for a timed activity); None with no goal, and
    # from a server older than the field.
    goal_type: str | None = None
    # Progress and goal in the unit of ``goal_type``: seconds, or completed marks or sessions.
    goal_progress: float | None = None
    goal_target: float | None = None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> Activity:
        """Parse an ``IntegrationActivityData`` record."""
        progress = data.get("progress") or {}
        goal = data.get("goal") or {}
        goal_progress = data.get("goalProgress") or {}
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
            goal_type=goal.get("type"),
            goal_progress=goal_progress.get("current"),
            goal_target=goal_progress.get("target"),
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


type ControlScalar = str | int | float


def _scalar(value: Any, what: str) -> ControlScalar:
    """A string or a number as sent; a boolean is neither."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"{what} is not a string or a number")
    return value


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} is not a string")
    return value


def same_value(a: ControlScalar | None, b: ControlScalar | None) -> bool:
    """Whether two values are the same with their type: the number 3 is not the text "3"."""
    if a is None or b is None:
        return a is b
    return isinstance(a, str) == isinstance(b, str) and a == b


@dataclass(frozen=True, slots=True)
class PluginControl:
    """One select control of a plugin, as the server's catalogue lists it."""

    plugin: str
    plugin_name: str
    control: str
    title: str
    icon: str | None
    # How to set it: this command, with this one field holding an option's value.
    command: str
    field: str
    # The name of the list its options come from, in the plugin's values.
    choices: str
    # Whether null is one of its options, and the plugin's wording for it.
    nullable: bool
    none: str | None

    @property
    def key(self) -> str:
        """How a control is stored and told apart: ``<plugin>/<control>``."""
        return f"{self.plugin}/{self.control}"

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> PluginControl | None:
        """Build from a catalogue entry; None for a kind this integration doesn't know."""
        if raw.get("kind") != "select":
            return None
        icon, none = raw.get("icon"), raw.get("none")
        nullable = raw.get("nullable")
        if not isinstance(nullable, bool):
            raise TypeError("nullable is not a boolean")
        return cls(
            plugin=_text(raw["plugin"], "plugin"),
            plugin_name=_text(raw.get("pluginName") or raw["plugin"], "pluginName"),
            control=_text(raw["control"], "control"),
            title=_text(raw.get("title") or raw["control"], "title"),
            icon=icon if isinstance(icon, str) else None,
            command=_text(raw["command"], "command"),
            field=_text(raw["field"], "field"),
            choices=_text(raw["choices"], "choices"),
            nullable=nullable,
            none=none if nullable and isinstance(none, str) and none else None,
        )


@dataclass(frozen=True, slots=True)
class PluginOption:
    """One option of a choices list: the value is stored and sent, the label is shown."""

    value: ControlScalar
    label: str
    detail: str | None

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> PluginOption:
        """Build from an option of a ``PluginValues`` list."""
        detail = raw.get("detail")
        return cls(
            value=_scalar(raw["value"], "value"),
            label=_text(raw["label"], "label"),
            detail=detail if isinstance(detail, str) and detail else None,
        )


@dataclass(frozen=True, slots=True)
class ControlValue:
    """A control's current value, wrapped: a value of None is known, and means nothing is selected."""

    value: ControlScalar | None


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """What one plugin's lists and controls hold now.

    Three things are kept apart, as on the wire: a list or a control that is None is unknown
    (not published, or the plugin isn't running), an empty list is known and empty, and a
    ``ControlValue`` holding None is known and null.
    """

    plugin: str
    # "running", "starting" or "unavailable"; anything but running is not running.
    status: str
    choices: MappingProxyType[str, tuple[PluginOption, ...] | None]
    controls: MappingProxyType[str, ControlValue | None]

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> PluginEntry:
        """Build from a plugin's entry of a ``PluginValues`` message."""
        choices: dict[str, tuple[PluginOption, ...] | None] = {}
        for name, listed in (raw.get("choices") or {}).items():
            choices[name] = (
                None
                if listed is None
                else tuple(PluginOption.from_wire(o) for o in listed["options"])
            )
        controls: dict[str, ControlValue | None] = {}
        for name, held in (raw.get("controls") or {}).items():
            if held is None:
                controls[name] = None
                continue
            value = held["value"]
            controls[name] = ControlValue(
                None if value is None else _scalar(value, "value")
            )
        return cls(
            plugin=_text(raw["plugin"], "plugin"),
            status=_text(raw["status"], "status"),
            choices=MappingProxyType(choices),
            controls=MappingProxyType(controls),
        )


def parse_plugin_controls(listed: Any) -> tuple[PluginControl, ...]:
    """The select controls of a ``PluginControls`` message, in its order.

    A malformed entry is left out with a warning, so one bad control doesn't cost the rest.
    """
    if not isinstance(listed, list):
        raise TypeError("controls is not a list")
    controls: dict[str, PluginControl] = {}
    for raw in listed:
        try:
            control = PluginControl.from_wire(raw)
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning("Ignoring a malformed plugin control: %r", err)
            continue
        if control is not None:
            controls.setdefault(control.key, control)
    return tuple(controls.values())


def parse_plugin_values(listed: Any) -> MappingProxyType[str, PluginEntry]:
    """Every plugin's entry of a ``PluginValues`` message, by plugin id.

    A malformed entry is left out with a warning: its plugin is then unknown, like one the
    message doesn't hold.
    """
    if not isinstance(listed, list):
        raise TypeError("plugins is not a list")
    entries: dict[str, PluginEntry] = {}
    for raw in listed:
        try:
            entry = PluginEntry.from_wire(raw)
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning("Ignoring a plugin's malformed values: %r", err)
            continue
        entries[entry.plugin] = entry
    return MappingProxyType(entries)


def shown_labels(
    options: tuple[PluginOption, ...], reserved: str | None = None
) -> tuple[str | None, ...]:
    """What each option is called in a select, in order: its label, made unique.

    Home Assistant's select holds strings and stores the one chosen, so no two options, nor an
    option and ``reserved`` (the wording for null), may read the same. A label nothing else has
    is shown as it is. Otherwise the option's detail is added when that tells it apart, and its
    value when it doesn't: values are unique within a list. An option without a label is shown
    as its value.

    None is longer than an entity's state may be: a label is cut, a detail is cut to the room
    its label leaves, and a value is never cut, since it is what tells two options apart, so
    the label gives way to it. An option whose value leaves no room is None: not offered.
    """
    labels = [_cut(option.label, _LABEL_LIMIT) for option in options]
    counts = Counter(labels)

    def plain(label: str) -> bool:
        return bool(label) and counts[label] == 1 and label != reserved

    def detailed(label: str, option: PluginOption) -> str | None:
        if not label or plain(label) or not option.detail:
            return None
        return f"{label} ({_cut(option.detail, STATE_LIMIT - len(label) - 3)})"

    taken = {label for label in labels if plain(label)}
    if reserved is not None:
        taken.add(reserved)
    details = Counter(
        text
        for label, option in zip(labels, options, strict=True)
        if (text := detailed(label, option)) is not None
    )
    shown: list[str | None] = []
    for label, option in zip(labels, options, strict=True):
        if plain(label):
            shown.append(label)
            continue
        text = detailed(label, option)
        if text is None or details[text] > 1 or text in taken:
            text = _valued(label, option.value, taken)
        if text is not None:
            taken.add(text)
        shown.append(text)
    return tuple(shown)


# Home Assistant shows a state longer than this as unknown, and logs an error at every write.
STATE_LIMIT = 255
# A label alone, so that a detail or a value in brackets still has room beside it.
_LABEL_LIMIT = 160


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _valued(label: str, value: ControlScalar, taken: set[str]) -> str | None:
    """``label`` with ``value`` in brackets, or the value alone: unique, or None without room.

    Another option's own label can read like this one's label and value, so the value is
    added again until nothing else reads the same.
    """
    tail = f" ({value})"
    if not label:
        text = str(value)
        while len(text) <= STATE_LIMIT:
            if text not in taken:
                return text
            text += tail
        return None
    added = tail
    while len(added) < STATE_LIMIT:
        text = f"{_cut(label, STATE_LIMIT - len(added))}{added}"
        if text not in taken:
            return text
        added += tail
    return None


@dataclass(frozen=True, slots=True)
class ControlView:
    """A select control as Home Assistant shows it: everything its entity's state is made of."""

    # False only when the plugin is known not to be running.
    available: bool
    # What each option is called, with the value it stands for; the null wording is last.
    options: tuple[tuple[str, ControlScalar | None], ...]
    # The name of the option that is current, or None while that is unknown.
    current: str | None
    # The control's value as the server sent it; None while unknown.
    value: ControlValue | None

    @property
    def names(self) -> list[str]:
        """The options' names, in order."""
        return [name for name, _ in self.options]


def control_view(
    control: PluginControl, entry: PluginEntry | None, default_none: str
) -> ControlView:
    """Resolve a control against its plugin's entry of the values (None: unknown).

    A plugin without an entry is neither running nor not running, so its control stays
    available with nothing known. With the list unknown the only option is the wording for
    null, on a control that offers null: clearing it needs no list. The current option is
    unknown when the value is, when the list doesn't hold it or holds it as an option too long
    to offer, and when it is null on a control that doesn't offer null.
    """
    none_name = (
        _cut(control.none or default_none, STATE_LIMIT) if control.nullable else None
    )
    listed = entry.choices.get(control.choices) if entry else None
    value = entry.controls.get(control.control) if entry else None
    names = shown_labels(listed or (), none_name)
    options: tuple[tuple[str, ControlScalar | None], ...] = tuple(
        (name, option.value)
        for name, option in zip(names, listed or (), strict=True)
        if name is not None
    )
    if none_name is not None:
        options = (*options, (none_name, None))
    current = None
    if value is not None:
        current = next(
            (name for name, held in options if same_value(held, value.value)), None
        )
    return ControlView(
        available=entry is None or entry.status == "running",
        options=options,
        current=current,
        value=value,
    )


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
    # The select controls the user's plugins declare (the last PluginControls), or None while
    # the server has sent none. A Snapshot holds neither this nor the values: the coordinator
    # carries both over, since only the server's next message of each kind replaces them.
    plugin_controls: tuple[PluginControl, ...] | None = None
    # Each plugin's lists and control values, by plugin id (the last PluginValues), or None
    # while the server has sent none. A plugin that isn't in it is unknown.
    plugin_values: MappingProxyType[str, PluginEntry] | None = None

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
        case "PluginControls":
            # Each one replaces the catalogue. The same one again changes nothing.
            controls = parse_plugin_controls(msg["controls"])
            if controls == state.plugin_controls:
                return state
            return replace(state, plugin_controls=controls)
        case "PluginValues":
            # Each one replaces everything held from the last.
            values = parse_plugin_values(msg["plugins"])
            if values == state.plugin_values:
                return state
            return replace(state, plugin_values=values)
        case _:
            return state
