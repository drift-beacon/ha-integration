"""Device registry model: one workspace device, and one service device per activity under it."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, MANUFACTURER, MODEL_ACTIVITY, MODEL_WORKSPACE
from .models import Activity, WorkspaceState

_LOGGER = logging.getLogger(__name__)

_ACTIVITY_MARKER = ":activity:"


class DeviceManager:
    """Keeps the device registry in step with the workspace's activities.

    Activity devices are created by their entities' ``device_info``. This class renames them,
    updates their tracking type, and removes the devices of activities that were deleted or
    archived. A removed device is restored with the same id if its activity comes back, so
    automations that reference it keep working.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, workspace_id: str
    ) -> None:
        """Bind to a config entry's workspace."""
        self._hass = hass
        self._entry = entry
        self.workspace_id = workspace_id
        self.workspace_device_id: str | None = None

    @callback
    def async_register_workspace(self, name: str, configuration_url: str) -> None:
        """Create or refresh the workspace device; activity devices link to its id."""
        device = dr.async_get(self._hass).async_get_or_create(
            config_entry_id=self._entry.entry_id,
            identifiers={self.workspace_identifier},
            name=name,
            manufacturer=MANUFACTURER,
            model=MODEL_WORKSPACE,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=configuration_url,
        )
        self.workspace_device_id = device.id

    @property
    def workspace_identifier(self) -> tuple[str, str]:
        """The workspace device's registry identifier."""
        return (DOMAIN, self.workspace_id)

    @property
    def workspace_device_info(self) -> DeviceInfo:
        """Device info for entities on the workspace device."""
        return DeviceInfo(identifiers={self.workspace_identifier})

    def activity_identifier(self, activity_id: str) -> tuple[str, str]:
        """An activity device's registry identifier."""
        return (DOMAIN, f"{self.workspace_id}{_ACTIVITY_MARKER}{activity_id}")

    def activity_device_info(self, activity: Activity) -> DeviceInfo:
        """Device info for entities on an activity's device."""
        return DeviceInfo(
            identifiers={self.activity_identifier(activity.id)},
            name=activity.name,
            manufacturer=MANUFACTURER,
            model=MODEL_ACTIVITY,
            model_id=activity.tracking_type,
            entry_type=DeviceEntryType.SERVICE,
            via_device_id=self.workspace_device_id,
        )

    def _device(self, identifier: tuple[str, str]) -> dr.DeviceEntry | None:
        return dr.async_get(self._hass).async_get_device_by_identifier(
            identifier, self._entry.entry_id
        )

    def activity_device_id(self, activity_id: str) -> str | None:
        """Return the registry id of an activity's device, if it exists."""
        device = self._device(self.activity_identifier(activity_id))
        return device.id if device else None

    def activity_id_for_device(self, device: dr.DeviceEntry) -> str | None:
        """Return the activity id a device represents, or None for any other device."""
        prefix = f"{self.workspace_id}{_ACTIVITY_MARKER}"
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith(prefix):
                return identifier.removeprefix(prefix)
        return None

    @callback
    def async_sync(
        self, prev: WorkspaceState | None, new: WorkspaceState, *, full: bool
    ) -> None:
        """Apply renames, type changes and removals after a chunk.

        ``full`` marks a Snapshot, which is reconciled against the registry so activities that
        disappeared while disconnected (or while Home Assistant was stopped) lose their devices.
        """
        registry = dr.async_get(self._hass)

        workspace = self._device(self.workspace_identifier)
        if workspace and workspace.name != new.workspace_name:
            registry.async_update_device(workspace.id, name=new.workspace_name)

        for activity in new.activities.values():
            before = prev.activity(activity.id) if prev and not full else None
            if before == activity:
                continue
            device = self._device(self.activity_identifier(activity.id))
            if device is None:
                continue
            # `name_by_user` is left alone, so a name the user chose keeps winning.
            if (
                device.name != activity.name
                or device.model_id != activity.tracking_type
            ):
                registry.async_update_device(
                    device.id, name=activity.name, model_id=activity.tracking_type
                )

        if full:
            stale = [
                device.id
                for device in dr.async_entries_for_config_entry(
                    registry, self._entry.entry_id
                )
                if (activity_id := self.activity_id_for_device(device)) is not None
                and activity_id not in new.activities
            ]
        elif prev is not None:
            stale = [
                device_id
                for activity_id in prev.activities.keys() - new.activities.keys()
                if (device_id := self.activity_device_id(activity_id)) is not None
            ]
        else:
            stale = []
        for device_id in stale:
            _LOGGER.debug(
                "Removing device %s for a deleted or archived activity", device_id
            )
            registry.async_remove_device(device_id)
