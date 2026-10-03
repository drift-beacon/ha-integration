"""Session and Pin switches on each activity device."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_MEMBER_IDS, ATTR_PINNED_AT, ATTR_SESSION_ID, ATTR_STARTED_AT
from .coordinator import DriftBeaconConfigEntry
from .entity import ActivityEntity, async_setup_activity_entities, compact
from .models import LiveSession, PinnedActivity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the activity switches."""
    async_setup_activity_entities(
        entry,
        async_add_entities,
        {
            SessionSwitch.role: (lambda a: a.tracking_type == "span", SessionSwitch),
            PinSwitch.role: (lambda a: True, PinSwitch),
        },
    )


class SessionSwitch(ActivityEntity, SwitchEntity):
    """On while the connection user has a live session on this timed activity."""

    role = "session"

    @property
    def _session(self) -> LiveSession | None:
        session = self.coordinator.data.live_session
        return session if session and session.activity_id == self.activity_id else None

    def state_key(self) -> Hashable:
        """Depends only on whether this activity holds the live session."""
        return self._session

    @property
    def is_on(self) -> bool:
        """Return whether this activity is being tracked."""
        return self._session is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe the live session; static for its lifetime."""
        session = self._session
        if session is None:
            return {}
        return compact(
            {
                ATTR_SESSION_ID: session.id,
                ATTR_STARTED_AT: session.started_at,
                ATTR_MEMBER_IDS: list(session.member_ids),
            }
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start tracking; already tracking is a no-op (starting again would restart it)."""
        if not self.is_on:
            await self.coordinator.async_start_session(self.activity_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop tracking."""
        await self.coordinator.async_stop_session(self.activity_id)


class PinSwitch(ActivityEntity, SwitchEntity):
    """On while this activity holds the connection user's pinned slot."""

    role = "pin"

    @property
    def _pinned(self) -> PinnedActivity | None:
        pinned = self.coordinator.data.pinned
        return pinned if pinned and pinned.activity_id == self.activity_id else None

    def state_key(self) -> Hashable:
        """Depends only on whether this activity is pinned."""
        return self._pinned

    @property
    def is_on(self) -> bool:
        """Return whether this activity is pinned."""
        return self._pinned is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """When the activity was pinned."""
        pinned = self._pinned
        return {ATTR_PINNED_AT: pinned.pinned_at} if pinned else {}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Pin this activity; the previous pin moves to the front of the queue."""
        await self.coordinator.async_pin(self.activity_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unpin this activity; with auto-advance on, the queue head is pinned next."""
        await self.coordinator.async_unpin(self.activity_id)
