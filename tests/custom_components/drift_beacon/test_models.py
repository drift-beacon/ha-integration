"""The pure state reducer and colour parsing."""

from __future__ import annotations

import pytest

from custom_components.drift_beacon.models import (
    WorkspaceState,
    apply_message,
    parse_color,
)

from .conftest import activity, live_session, make_snapshot, schedule_trigger


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#336699", [51, 102, 153]),
        ("336699", [51, 102, 153]),
        ("#369", [51, 102, 153]),
        ("#336699cc", [51, 102, 153]),
        ("oklch(0.7 0.1 200)", None),
        ("#zzzzzz", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_color_never_raises(value: object, expected: list[int] | None) -> None:
    """Colours the server does not normalise degrade to None instead of raising."""
    assert parse_color(value) == expected


def _state(**overrides: object) -> WorkspaceState:
    return WorkspaceState.from_snapshot(make_snapshot(**overrides))


def test_snapshot_keeps_one_live_session_and_the_pin() -> None:
    """The member has at most one live session; the pin is per user."""
    state = _state(
        liveSessions=[live_session("s1", "reading")],
        pinnedActivity={"activityId": "water", "pinnedAt": "2026-09-24T09:00:00Z"},
    )
    assert state.live_session.id == "s1"
    assert state.pinned.activity_id == "water"
    assert state.activity("water").unit == "glasses"
    assert state.activity("reading").color == [51, 102, 153]


def test_session_ended_only_clears_the_session_it_names() -> None:
    """A stale SessionEnded must not clear a newer session."""
    state = _state(liveSessions=[live_session("s2", "reading")])
    assert (
        apply_message(
            state, {"_tag": "SessionEnded", "sessionId": "s1", "activityId": "reading"}
        )
        is state
    )
    cleared = apply_message(
        state, {"_tag": "SessionEnded", "sessionId": "s2", "activityId": "reading"}
    )
    assert cleared.live_session is None


def test_activity_lifecycle_and_unknown_tags() -> None:
    """Create, update and delete activities; ignore tags this client does not know."""
    state = _state()
    state = apply_message(
        state, {"_tag": "ActivityCreated", "activity": activity("run", "Run")}
    )
    state = apply_message(
        state,
        {"_tag": "ActivityUpdated", "activity": activity("run", "Running", "point")},
    )
    assert state.activity("run").name == "Running"
    assert state.activity("run").tracking_type == "point"
    state = apply_message(state, {"_tag": "ActivityDeleted", "activityId": "run"})
    assert state.activity("run") is None
    assert apply_message(state, {"_tag": "LabelCreated", "label": {}}) is state


def test_point_marks_accumulate_until_consumed() -> None:
    """Marks are carried on the state for the coordinator to turn into events."""
    state = apply_message(
        _state(),
        {
            "_tag": "PointMarked",
            "session": {
                "id": "m1",
                "activityId": "water",
                "memberIds": ["user-1"],
                "markedAt": "2026-09-24T10:00:00Z",
            },
        },
    )
    assert [mark.id for mark in state.marks] == ["m1"]


def test_schedule_triggers_accumulate_until_consumed() -> None:
    """Triggers are carried like marks, whatever their kind and behavior; a Snapshot has none."""
    state = _state()
    assert state.schedule_triggers == ()
    for trigger in (
        schedule_trigger("morning", "reading"),
        schedule_trigger("lunar", "water", kind="everyFullMoon", behavior="later"),
    ):
        state = apply_message(state, {"_tag": "ScheduleTriggered", "trigger": trigger})
    morning, lunar = state.schedule_triggers
    assert (morning.id, morning.schedule_id, morning.activity_id) == (
        "morning:2026-09-24",
        "morning",
        "reading",
    )
    assert (morning.kind, morning.behavior, morning.triggered_at) == (
        "recurring",
        "pin",
        "2026-09-24T09:00:00.000Z",
    )
    assert (lunar.kind, lunar.behavior) == ("everyFullMoon", "later")
