"""Progress sensors on activity devices; session, pin and user sensors on the workspace device."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_ACTIVITY_DEVICE_ID,
    ATTR_ACTIVITY_ID,
    ATTR_CATEGORY_ID,
    ATTR_CATEGORY_NAME,
    ATTR_COLOR,
    ATTR_DESCRIPTION,
    ATTR_MEMBER_IDS,
    ATTR_PINNED_AT,
    ATTR_SESSION_ID,
    ATTR_STARTED_AT,
    ATTR_TARGET,
    ATTR_TRACKING_TYPE,
    ATTR_USER_ID,
)
from .coordinator import DriftBeaconConfigEntry
from .entity import (
    ActivityEntity,
    WorkspaceEntity,
    async_setup_activity_entities,
    compact,
)

PARALLEL_UPDATES = 0


def _mdi(icon: str | None) -> str | None:
    """Activity icons are only usable in Home Assistant when they are MDI icons."""
    return icon if icon and icon.startswith("mdi:") else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            CurrentSessionSensor(coordinator, "current_session"),
            PinnedActivitySensor(coordinator, "pinned_activity"),
            ConnectedUserSensor(coordinator, "connected_user"),
        ]
    )
    async_setup_activity_entities(
        entry,
        async_add_entities,
        {
            TimeProgressSensor.role: (
                lambda a: a.tracking_type == "span",
                TimeProgressSensor,
            ),
            CountProgressSensor.role: (
                lambda a: a.tracking_type == "point",
                CountProgressSensor,
            ),
        },
    )


class ProgressSensor(ActivityEntity, SensorEntity):
    """Progress for the activity's progress period, across all workspace members.

    Timed activities report completed-session time (the running session is not included);
    point activities report their mark count. No ``state_class``: long-term statistics for
    every activity are not worth their cost.
    """

    entity_translation_key = "progress"
    # Static or rarely changing metadata stays out of the recorder.
    _unrecorded_attributes = frozenset(
        {
            ATTR_ACTIVITY_ID,
            ATTR_TRACKING_TYPE,
            ATTR_COLOR,
            ATTR_DESCRIPTION,
            ATTR_CATEGORY_ID,
            ATTR_CATEGORY_NAME,
        }
    )

    def state_key(self) -> Hashable:
        """Depends on the activity and the name of its category."""
        activity = self.activity
        category = (
            self.coordinator.data.categories.get(activity.category_id)
            if activity and activity.category_id
            else None
        )
        return (activity, category)

    @property
    def native_value(self) -> float | None:
        """Progress in the current progress period."""
        activity = self.activity
        return activity.progress if activity else None

    @property
    def icon(self) -> str | None:
        """The activity's own icon, when it is an MDI icon."""
        activity = self.activity
        return _mdi(activity.icon) if activity else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Target plus activity metadata for templates and dashboards."""
        activity = self.activity
        if activity is None:
            return {}
        category = self.coordinator.data.categories.get(activity.category_id or "")
        return compact(
            {
                ATTR_TARGET: activity.target,
                ATTR_ACTIVITY_ID: activity.id,
                ATTR_TRACKING_TYPE: activity.tracking_type,
                ATTR_COLOR: activity.color,
                ATTR_DESCRIPTION: activity.description,
                ATTR_CATEGORY_ID: activity.category_id,
                ATTR_CATEGORY_NAME: category.name if category else None,
            }
        )


class TimeProgressSensor(ProgressSensor):
    """Completed-session time for a timed activity."""

    role = "progress_time"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 1


class CountProgressSensor(ProgressSensor):
    """Mark count for a point activity."""

    role = "progress_count"

    @property
    def native_unit_of_measurement(self) -> str | None:
        """The activity's own unit, if it has one."""
        activity = self.activity
        return activity.unit if activity else None


class CurrentSessionSensor(WorkspaceEntity, SensorEntity):
    """Name of the activity the connection user is tracking, or unknown when idle."""

    _unrecorded_attributes = frozenset({ATTR_ACTIVITY_DEVICE_ID, ATTR_MEMBER_IDS})

    @property
    def native_value(self) -> str | None:
        """The live activity's name."""
        state = self.coordinator.data
        session = state.live_session
        if session is None:
            return None
        activity = state.activity(session.activity_id)
        # A session can outlive its activity being archived; still report that something is live.
        return activity.name if activity else session.activity_id

    @property
    def icon(self) -> str | None:
        """The live activity's icon."""
        state = self.coordinator.data
        session = state.live_session
        activity = state.activity(session.activity_id) if session else None
        return _mdi(activity.icon) if activity else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Identify the session and its activity device."""
        session = self.coordinator.data.live_session
        if session is None:
            return {}
        return compact(
            {
                ATTR_ACTIVITY_ID: session.activity_id,
                ATTR_ACTIVITY_DEVICE_ID: self.coordinator.devices.activity_device_id(
                    session.activity_id
                ),
                ATTR_SESSION_ID: session.id,
                ATTR_STARTED_AT: session.started_at,
                ATTR_MEMBER_IDS: list(session.member_ids),
            }
        )


class PinnedActivitySensor(WorkspaceEntity, SensorEntity):
    """Name of the connection user's pinned activity, or unknown when nothing is pinned."""

    _unrecorded_attributes = frozenset({ATTR_ACTIVITY_DEVICE_ID})

    @property
    def native_value(self) -> str | None:
        """The pinned activity's name."""
        state = self.coordinator.data
        pinned = state.pinned
        if pinned is None:
            return None
        activity = state.activity(pinned.activity_id)
        return activity.name if activity else pinned.activity_id

    @property
    def icon(self) -> str | None:
        """The pinned activity's icon."""
        state = self.coordinator.data
        pinned = state.pinned
        activity = state.activity(pinned.activity_id) if pinned else None
        return _mdi(activity.icon) if activity else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Identify the pinned activity and its device."""
        pinned = self.coordinator.data.pinned
        if pinned is None:
            return {}
        return compact(
            {
                ATTR_ACTIVITY_ID: pinned.activity_id,
                ATTR_ACTIVITY_DEVICE_ID: self.coordinator.devices.activity_device_id(
                    pinned.activity_id
                ),
                ATTR_PINNED_AT: pinned.pinned_at,
            }
        )


class ConnectedUserSensor(WorkspaceEntity, SensorEntity):
    """The Drift Beacon user this connection acts for."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str:
        """The user's display name."""
        return self.coordinator.data.user_name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The stable user id."""
        return {ATTR_USER_ID: self.coordinator.data.user_id}
