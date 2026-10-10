"""Select entities for the controls plugins declare, on the workspace device.

A plugin's select control (magic-cube's preset, say) is a current value with the command that
sets it. The server sends the catalogue of controls (``PluginControls``) and, live, each
plugin's options and current values (``PluginValues``). Each control is one select here: its
options are the list's labels, and choosing one runs the control's command with that option's
value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Hashable
from functools import partial
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_CONTROL,
    ATTR_PLUGIN,
    ATTR_VALUE,
    DEFAULT_NONE_OPTION,
    DOMAIN,
)
from .coordinator import DriftBeaconConfigEntry, DriftBeaconCoordinator
from .entity import DriftBeaconEntity
from .models import ControlView, PluginControl, control_view

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Keep one select per control in the workspace's catalogue.

    A control that leaves the catalogue (its plugin was disabled or uninstalled) takes its
    entity out of Home Assistant but keeps its registry entry, so its name, area and entity id
    are there when it returns. Nothing changes while the catalogue is unknown.
    """
    coordinator = entry.runtime_data
    entities: dict[str, PluginControlSelect] = {}
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
        wanted = {control.key: control for control in controls}
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
            entities[key] = PluginControlSelect(coordinator, control)
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


class PluginControlSelect(DriftBeaconEntity, SelectEntity):
    """One select control of a plugin, for this workspace.

    Options are labels: Home Assistant's select holds strings, and an automation stores the
    one it chose, so a renamed option breaks it. The value behind the current option is the
    ``value`` attribute. The state is not optimistic: it changes when the server says the
    plugin published the new value, wherever it was set from.
    """

    # A workspace has a handful of controls, and a preset picker belongs on a dashboard.
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
        self._view = self._resolve()
        self._written: Hashable = None

    def _resolve(self) -> ControlView:
        """Read the control and its plugin's values once, for everything the state is made of."""
        state = self.coordinator.data
        if state.plugin_controls is not None:
            listed = next(
                (c for c in state.plugin_controls if c.key == self._key), None
            )
            self._listed = listed is not None
            if listed is not None:
                self._control = listed
        values = state.plugin_values
        entry = values.get(self._control.plugin) if values is not None else None
        return control_view(self._control, entry, DEFAULT_NONE_OPTION)

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
        """Available while connected, unless the plugin is known not to be running."""
        return super().available and self._view.available

    @property
    def options(self) -> list[str]:
        """The list's labels, then the wording for null on a control that offers it."""
        return self._view.names

    @property
    def current_option(self) -> str | None:
        """The option whose value is the control's; None while that is unknown."""
        return self._view.current

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The control's identity, and the value behind the current option while known."""
        attributes: dict[str, Any] = {
            ATTR_PLUGIN: self._control.plugin,
            ATTR_CONTROL: self._control.control,
        }
        if self._view.value is not None:
            attributes[ATTR_VALUE] = self._view.value.value
        return attributes

    async def async_select_option(self, option: str) -> None:
        """Run the control's command with the value of ``option``."""
        control = self._control
        for name, value in self._view.options:
            if name == option:
                await self.coordinator.async_run_plugin_command(
                    control.plugin, control.command, {control.field: value}
                )
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="plugin_control_no_option",
            translation_placeholders={"option": option, "control": self.name},
        )

    def _state_key(self) -> Hashable:
        view = self._view
        return (
            self.available,
            self.name,
            self.icon,
            view.options,
            view.current,
            view.value,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._view = self._resolve()
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
        self._view = self._resolve()
        self._written = self._state_key()
