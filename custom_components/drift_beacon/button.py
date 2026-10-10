"""Mark buttons on point activity devices, Stop session on the workspace device, and a button per trigger a plugin declares."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DriftBeaconConfigEntry
from .entity import ActivityEntity, WorkspaceEntity, async_setup_activity_entities
from .plugin_control import PluginControlEntity, async_setup_plugin_controls

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
    async_setup_plugin_controls(
        hass, entry, async_add_entities, "trigger", PluginControlButton
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


class PluginControlButton(PluginControlEntity, ButtonEntity):
    """One trigger control of a plugin: its command, run with no input.

    It has no value. It is unavailable while its plugin is known not to be running.
    """

    async def async_press(self) -> None:
        """Run the control's command."""
        control = self._control
        await self.coordinator.async_run_plugin_command(
            control.plugin, control.command, {}
        )
