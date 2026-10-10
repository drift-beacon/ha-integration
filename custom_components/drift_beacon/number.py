"""A number per range control a plugin declares, on the workspace device."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DriftBeaconConfigEntry
from .plugin_control import PluginControlEntity, async_setup_plugin_controls

PARALLEL_UPDATES = 0

# Steps across a range whose plugin names none and takes fractions.
_DEFAULT_STEPS = 100


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Keep one number per range control in the workspace's catalogue."""
    async_setup_plugin_controls(
        hass, entry, async_add_entities, "number", "range", PluginControlNumber
    )


class PluginControlNumber(PluginControlEntity, NumberEntity):
    """One range control of a plugin: a number between the two ends its command takes."""

    _attr_mode = NumberMode.SLIDER

    @property
    def native_min_value(self) -> float:
        """The lowest value the control's command takes."""
        return self._control.min or 0

    @property
    def native_max_value(self) -> float:
        """The highest value the control's command takes."""
        low, high = self._control.min, self._control.max
        return high if high is not None else (low or 0) + 1

    @property
    def native_step(self) -> float:
        """The plugin's step; without one, 1 for whole numbers and a hundredth of the range."""
        control = self._control
        if control.step is not None:
            return control.step
        if control.integer:
            return 1
        return (self.native_max_value - self.native_min_value) / _DEFAULT_STEPS

    @property
    def native_unit_of_measurement(self) -> str | None:
        """What the number counts, when the plugin says."""
        return self._control.unit

    @property
    def native_value(self) -> float | None:
        """The control's value as published; None while it is unknown or isn't a number."""
        held = self._value
        if held is None or isinstance(held.value, (bool, str)):
            return None
        return held.value

    async def async_set_native_value(self, value: float) -> None:
        """Run the control's command with the number, whole when the control takes whole ones."""
        await self._async_set(round(value) if self._control.integer else value)
