"""Mark buttons on point activity devices, and Stop session on the workspace device."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DriftBeaconConfigEntry
from .entity import ActivityEntity, WorkspaceEntity, async_setup_activity_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities([StopSessionButton(entry.runtime_data, "stop_session")])
    async_setup_activity_entities(
        entry,
        async_add_entities,
        {MarkButton.role: (lambda a: a.tracking_type == "point", MarkButton)},
    )


class MarkButton(ActivityEntity, ButtonEntity):
    """Records one occurrence of a point activity."""

    role = "mark"

    async def async_press(self) -> None:
        """Mark the activity."""
        await self.coordinator.async_mark(self.activity_id)


class StopSessionButton(WorkspaceEntity, ButtonEntity):
    """Stops the connection user's live session, whichever activity it belongs to."""

    async def async_press(self) -> None:
        """Stop the live session; nothing live is a no-op."""
        await self.coordinator.async_stop_current_session()
