"""Select entities for the controls plugins declare, on the workspace device.

A plugin's select control (magic-cube's preset, say) is a current value with the command that
sets it. The server sends the catalogue of controls (``PluginControls``) and, live, each
plugin's options and current values (``PluginValues``). Each control is one select here: its
options are the list's labels, and choosing one runs the control's command with that option's
value.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_VALUE, DEFAULT_NONE_OPTION, DOMAIN
from .coordinator import DriftBeaconConfigEntry
from .models import ControlView, control_view
from .plugin_control import PluginControlEntity, async_setup_plugin_controls

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Keep one select per select control in the workspace's catalogue."""
    async_setup_plugin_controls(
        hass, entry, async_add_entities, "select", PluginControlSelect
    )


class PluginControlSelect(PluginControlEntity, SelectEntity):
    """One select control of a plugin, for this workspace.

    Options are labels: Home Assistant's select holds strings, and an automation stores the
    one it chose, so a renamed option breaks it. The value behind the current option is the
    ``value`` attribute.
    """

    _view: ControlView

    def _read(self) -> None:
        super()._read()
        self._view = control_view(self._control, self._entry, DEFAULT_NONE_OPTION)

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
        attributes = super().extra_state_attributes
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

    def _shown(self) -> Hashable:
        view = self._view
        return (view.options, view.current, view.value)
