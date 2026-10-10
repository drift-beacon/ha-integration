"""What the entities of plugin controls share: one entity per control, kept with the catalogue.

A plugin declares controls, and the server sends their catalogue (``PluginControls``) and,
live, each plugin's values (``PluginValues``). A control is one entity on the workspace
device, on the platform of its kind: a select, a switch for a toggle, a number for a range and
a button for a trigger. This module holds the entity they all extend and the reconciler each
platform runs for its own kind.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable
from functools import partial
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_CONTROL, ATTR_PLUGIN
from .coordinator import DriftBeaconConfigEntry, DriftBeaconCoordinator
from .entity import DriftBeaconEntity
from .models import ControlValue, PluginControl, PluginEntry


class PluginControlEntity(DriftBeaconEntity):
    """One control of a plugin, for this workspace.

    The state is not optimistic: it changes when the server says the plugin published the new
    value, wherever it was set from. It is written only when something it is made of changed.
    """

    # A workspace has a handful of controls, and they are what a user puts on a dashboard.
    _attr_entity_registry_visible_default = True

    def __init__(
        self, coordinator: DriftBeaconCoordinator, control: PluginControl
    ) -> None:
        """Bind to one control, by its plugin and name."""
        super().__init__(coordinator)
        self._key = control.key
        self._attr_unique_id = f"{coordinator.workspace_id}:control:{control.key}"
        self._attr_device_info = coordinator.devices.workspace_device_info
        self._control = control
        self._listed = True
        self._entry: PluginEntry | None = None
        self._read()
        self._written: Hashable = None

    def _read(self) -> None:
        """Read the control and its plugin's values once, for everything the state is made of."""
        state = self.coordinator.data
        if state.plugin_controls is not None:
            listed = next(
                (
                    c
                    for c in state.plugin_controls
                    if c.key == self._key and c.kind == self._control.kind
                ),
                None,
            )
            self._listed = listed is not None
            if listed is not None:
                self._control = listed
        values = state.plugin_values
        self._entry = values.get(self._control.plugin) if values is not None else None

    @property
    def _value(self) -> ControlValue | None:
        """The control's value as the server sent it; None while unknown."""
        return self._entry.controls.get(self._control.control) if self._entry else None

    @property
    def name(self) -> str:
        """The plugin's name, then the control's title, unless the title starts with it."""
        control = self._control
        if control.title.casefold().startswith(control.plugin_name.casefold()):
            return control.title
        return f"{control.plugin_name} {control.title}"

    @property
    def icon(self) -> str | None:
        """The plugin's icon for the control, when it gives one."""
        return self._control.icon

    @property
    def available(self) -> bool:
        """Available while connected, unless the plugin is known not to be running.

        A plugin without an entry in the values is neither running nor not running.
        """
        return super().available and (
            self._entry is None or self._entry.status == "running"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The control's identity."""
        return {
            ATTR_PLUGIN: self._control.plugin,
            ATTR_CONTROL: self._control.control,
        }

    async def _async_set(self, value: Any) -> None:
        """Run the control's command with its one field holding ``value``."""
        control = self._control
        await self.coordinator.async_run_plugin_command(
            control.plugin, control.command, {control.field: value}
        )

    def _shown(self) -> Hashable:
        """What the kind's own state is made of: the value with its type, since True equals 1."""
        held = self._value
        return held and (type(held.value), held.value)

    def _state_key(self) -> Hashable:
        return (self.available, self.name, self.icon, self._shown())

    @callback
    def _handle_coordinator_update(self) -> None:
        self._read()
        # A control that left the catalogue is about to be removed: it has nothing to say.
        if not self._listed:
            return
        key = self._state_key()
        if key != self._written:
            self._written = key
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Record the initial state so the first update is compared against it."""
        await super().async_added_to_hass()
        self._read()
        self._written = self._state_key()


def async_setup_plugin_controls[E: PluginControlEntity](
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    kind: str,
    make: Callable[[DriftBeaconCoordinator, PluginControl], E],
) -> None:
    """Keep one entity per control of ``kind`` in the workspace's catalogue.

    A control that leaves the catalogue (its plugin was disabled or uninstalled) takes its
    entity out of Home Assistant but keeps its registry entry, so its name, area and entity id
    are there when it returns. Nothing changes while the catalogue is unknown.
    """
    coordinator = entry.runtime_data
    entities: dict[str, E] = {}
    # Entities on their way out, by control: one that returns waits for its removal.
    removing: dict[str, asyncio.Task[None]] = {}
    unloaded = False

    @callback
    def removed(key: str, removal: asyncio.Task[None]) -> None:
        if removing.get(key) is removal:
            del removing[key]
        # The control may have returned meanwhile. Nothing is added to a platform that is gone.
        if not unloaded:
            reconcile()

    @callback
    def reconcile() -> None:
        controls = coordinator.data.plugin_controls
        if controls is None:
            return
        wanted = {control.key: control for control in controls if control.kind == kind}
        for key in entities.keys() - wanted.keys():
            entity = entities.pop(key)
            # One Home Assistant never added (the user disabled it) has nothing to remove.
            if entity.hass is None:
                continue
            removal = hass.async_create_task(entity.async_remove())
            if not removal.done():
                removing[key] = removal
                removal.add_done_callback(partial(removed, key))
        new = []
        for key, control in wanted.items():
            if key in entities or key in removing:
                continue
            entities[key] = make(coordinator, control)
            new.append(entities[key])
        if new:
            async_add_entities(new)

    @callback
    def unload() -> None:
        nonlocal unloaded
        unloaded = True

    reconcile()
    entry.async_on_unload(coordinator.async_add_listener(reconcile))
    entry.async_on_unload(unload)
