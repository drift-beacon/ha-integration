"""Service actions targeting Drift Beacon activity and workspace devices.

Every entity is hidden, and Home Assistant leaves hidden entities out when it expands a device
target, so these services resolve devices themselves instead of going through entities.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.target import TargetSelection

from .const import (
    DOMAIN,
    SERVICE_PAUSE_ACTIVITY,
    SERVICE_PAUSE_SESSION,
    SERVICE_PIN_ACTIVITY,
    SERVICE_QUEUE_ACTIVITY,
    SERVICE_STOP_SESSION,
    SERVICE_TRACK_ACTIVITY,
    SERVICE_UNPIN_ACTIVITY,
)
from .coordinator import DriftBeaconConfigEntry, DriftBeaconCoordinator

type ActivityAction = Callable[[DriftBeaconCoordinator, str], Awaitable[None]]
type WorkspaceAction = Callable[[DriftBeaconCoordinator], Awaitable[None]]

ACTIVITY_ACTIONS: dict[str, ActivityAction] = {
    SERVICE_TRACK_ACTIVITY: DriftBeaconCoordinator.async_track,
    SERVICE_PAUSE_ACTIVITY: DriftBeaconCoordinator.async_pause_session,
    SERVICE_PIN_ACTIVITY: DriftBeaconCoordinator.async_pin,
    SERVICE_UNPIN_ACTIVITY: DriftBeaconCoordinator.async_unpin,
    SERVICE_QUEUE_ACTIVITY: DriftBeaconCoordinator.async_queue,
}

WORKSPACE_ACTIONS: dict[str, WorkspaceAction] = {
    SERVICE_STOP_SESSION: DriftBeaconCoordinator.async_stop_current_session,
    SERVICE_PAUSE_SESSION: DriftBeaconCoordinator.async_pause_current_session,
}

# Only device targets: an area or label must not be able to track every activity in it.
SERVICE_SCHEMA = cv.make_entity_service_schema({})


@dataclass(frozen=True, slots=True)
class _Target:
    coordinator: DriftBeaconCoordinator
    activity_id: str | None
    name: str


def resolve_devices(hass: HomeAssistant, call: ServiceCall) -> list[_Target]:
    """Map the call's device targets to loaded workspaces and their activities."""
    selection = TargetSelection(call.data)
    if (
        selection.entity_ids
        or selection.area_ids
        or selection.floor_ids
        or selection.label_ids
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="devices_only"
        )
    if not selection.device_ids:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_target"
        )

    registry = dr.async_get(hass)
    targets = []
    for device_id in sorted(selection.device_ids):
        device = registry.async_get(device_id)
        entry: DriftBeaconConfigEntry | None = (
            hass.config_entries.async_get_entry(device.config_entry_id)
            if device and device.config_entry_id
            else None
        )
        if device is None or entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_drift_beacon_device",
                translation_placeholders={"device_id": device_id},
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="workspace_not_loaded",
                translation_placeholders={"workspace": entry.title},
            )
        coordinator = entry.runtime_data
        name = device.name_by_user or device.name or device_id
        targets.append(
            _Target(
                coordinator, coordinator.devices.activity_id_for_device(device), name
            )
        )
    return targets


async def async_run_all(
    calls: list[tuple[str, Callable[[], Awaitable[None]]]],
) -> None:
    """Run every target, then report all failures together."""
    failures: list[tuple[str, HomeAssistantError]] = []
    for name, run in calls:
        try:
            await run()
        except HomeAssistantError as err:
            failures.append((name, err))
    if len(calls) == 1 and failures:
        raise failures[0][1]
    if failures:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="some_targets_failed",
            translation_placeholders={
                "failures": "; ".join(f"{name}: {err}" for name, err in failures)
            },
        )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Drift Beacon service actions."""

    async def handle_activity(call: ServiceCall) -> None:
        action = ACTIVITY_ACTIONS[call.service]
        targets = resolve_devices(hass, call)
        for target in targets:
            if target.activity_id is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="not_activity_device",
                    translation_placeholders={"device": target.name},
                )
            if target.coordinator.data.activity(target.activity_id) is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="activity_unavailable",
                    translation_placeholders={"device": target.name},
                )
        await async_run_all(
            [
                (t.name, partial(action, t.coordinator, t.activity_id))  # type: ignore[arg-type]
                for t in targets
            ]
        )

    async def handle_workspace(call: ServiceCall) -> None:
        action = WORKSPACE_ACTIONS[call.service]
        targets = resolve_devices(hass, call)
        for target in targets:
            if target.activity_id is not None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="not_workspace_device",
                    translation_placeholders={"device": target.name},
                )
        # Several devices of one workspace still mean one action for it.
        unique = {id(t.coordinator): t for t in targets}.values()
        await async_run_all([(t.name, partial(action, t.coordinator)) for t in unique])

    for service in ACTIVITY_ACTIONS:
        hass.services.async_register(
            DOMAIN, service, handle_activity, schema=SERVICE_SCHEMA
        )
    for service in WORKSPACE_ACTIONS:
        hass.services.async_register(
            DOMAIN, service, handle_workspace, schema=SERVICE_SCHEMA
        )
