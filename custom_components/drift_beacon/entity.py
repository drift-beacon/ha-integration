"""Base entities and the per-activity entity reconciler shared by all platforms."""

from __future__ import annotations

import logging
from collections.abc import Callable, Hashable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DriftBeaconConfigEntry, DriftBeaconCoordinator
from .models import Activity

_LOGGER = logging.getLogger(__name__)


class DriftBeaconEntity(CoordinatorEntity[DriftBeaconCoordinator]):
    """Base for every Drift Beacon entity.

    Every entity is hidden by default: a workspace can have hundreds of activities, and their
    controls belong in automations, not on auto-generated dashboards.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_visible_default = False


class WorkspaceEntity(DriftBeaconEntity):
    """An entity on the workspace device."""

    def __init__(self, coordinator: DriftBeaconCoordinator, key: str) -> None:
        """Name and identify the entity by ``key``."""
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.workspace_id}:{key}"
        self._attr_device_info = coordinator.devices.workspace_device_info


class ActivityEntity(DriftBeaconEntity):
    """An entity on an activity's device.

    State is only written when this entity's own inputs change, so an update to one activity
    does not rewrite the state of every entity in the workspace.
    """

    # Part of the unique id; an activity has at most one entity per role.
    role: str
    # Defaults to the role; set when two roles share a name.
    entity_translation_key: str | None = None

    def __init__(self, coordinator: DriftBeaconCoordinator, activity: Activity) -> None:
        """Bind to one activity."""
        super().__init__(coordinator)
        self.activity_id = activity.id
        self._attr_translation_key = self.entity_translation_key or self.role
        self._attr_unique_id = f"{coordinator.workspace_id}:{activity.id}:{self.role}"
        self._attr_device_info = coordinator.devices.activity_device_info(activity)
        self._written: Hashable = None

    @property
    def activity(self) -> Activity | None:
        """The activity, or None once it has been deleted or archived."""
        return self.coordinator.data.activity(self.activity_id)

    @property
    def available(self) -> bool:
        """Available while connected and the activity still exists."""
        return super().available and self.activity is not None

    def state_key(self) -> Hashable:
        """Everything this entity's state and attributes depend on."""
        return self.activity

    @callback
    def _handle_coordinator_update(self) -> None:
        key = (self.available, self.state_key())
        if key != self._written:
            self._written = key
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Record the initial state so the first update is compared against it."""
        await super().async_added_to_hass()
        self._written = (self.available, self.state_key())


type ActivityEntityFactory = Callable[
    [DriftBeaconCoordinator, Activity], ActivityEntity
]


@callback
def async_setup_activity_entities(
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factories: dict[str, tuple[Callable[[Activity], bool], ActivityEntityFactory]],
) -> None:
    """Keep one entity per (activity, role) for which the role applies.

    ``factories`` maps a role to ``(applies_to, factory)``. New activities get entities, and an
    activity whose tracking type changes swaps roles (Session ↔ Mark). Entities of deleted or
    archived activities disappear with their device (see ``DeviceManager.async_sync``).
    """
    coordinator = entry.runtime_data
    entities: dict[tuple[str, str], ActivityEntity] = {}

    @callback
    def reconcile() -> None:
        state = coordinator.data
        wanted = {
            (activity.id, role): (activity, factory)
            for activity in state.activities.values()
            for role, (applies_to, factory) in factories.items()
            if applies_to(activity)
        }
        registry = er.async_get(coordinator.hass)
        for key in entities.keys() - wanted.keys():
            entity = entities.pop(key)
            if entity.entity_id and registry.async_get(entity.entity_id):
                # Removing the registry entry also removes the entity from Home Assistant.
                registry.async_remove(entity.entity_id)
        new = []
        for key, (activity, factory) in wanted.items():
            if key not in entities:
                entities[key] = factory(coordinator, activity)
                new.append(entities[key])
        if new:
            async_add_entities(new)

    reconcile()
    entry.async_on_unload(coordinator.async_add_listener(reconcile))


def compact(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop None values, so absent data does not produce churn in the recorder."""
    return {k: v for k, v in attributes.items() if v is not None}
