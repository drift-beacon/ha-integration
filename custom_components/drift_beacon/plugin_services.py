"""Plugin commands as service actions.

Every command a Drift Beacon plugin provides in a workspace is a service action,
``drift_beacon.<manifest id>_<command in snake case>``, targeting that workspace's device.
Each config entry's coordinator pushes its workspace's list (``PluginCommands`` on the stream);
the services are the union across entries. The lists are kept in storage, so services exist
while a server is down or an entry reloads (calls then fail as not loaded, which an automation
can continue past, instead of the action being unknown), and go only when no entry offers them.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryChange
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, MODEL_WORKSPACE
from .coordinator import DriftBeaconCoordinator
from .services import (
    ACTIVITY_ACTIONS,
    WORKSPACE_ACTIONS,
    async_run_all,
    resolve_devices,
)

_LOGGER = logging.getLogger(__name__)

PLUGIN_SERVICES: HassKey[PluginServices] = HassKey(f"{DOMAIN}_plugin_services")
STORAGE_KEY = f"{DOMAIN}.plugin_commands"
STORAGE_VERSION = 1
SAVE_DELAY = 1  # seconds

# Keys Home Assistant takes as targets, or drops, before a handler sees the call.
RESERVED_FIELDS = frozenset(
    {"entity_id", "device_id", "area_id", "floor_id", "label_id", "metadata"}
)
STATIC_SERVICES = frozenset(ACTIVITY_ACTIONS) | frozenset(WORKSPACE_ACTIONS)
# Only device targets, as for the other services; the fields are the command's input.
SERVICE_SCHEMA = cv.make_entity_service_schema({}, extra=vol.ALLOW_EXTRA)
TARGET = {"device": [{"integration": DOMAIN, "model": MODEL_WORKSPACE}]}


def service_name(plugin: str, command: str) -> str | None:
    """Name a command's service, or None when scripts couldn't call that name."""
    slug = re.sub(r"[^a-z0-9]+", "_", plugin.lower()).strip("_")
    snake = re.sub(r"(?<=.)([A-Z])", r"_\1", command).lower()
    name = f"{slug}_{snake}"
    try:
        cv.service(f"{DOMAIN}.{name}")
    except vol.Invalid:
        return None
    return name


def _types(schema: Mapping[str, Any]) -> list[str]:
    """A schema's types, without ``null`` (a field left out is how to send nothing)."""
    kind = schema.get("type")
    kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
    return [k for k in kinds if k != "null"]


def _option_text(value: Any) -> str | None:
    """An option's value as a select holds it: text as it is, a number as its digits.

    None for null, which a select can't hold: YAML can still send it.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return json.dumps(value)
    return None


def _labelled(schema: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    """A field's declared ``options`` as (text, label, value), without null."""
    options = schema.get("options")
    if not isinstance(options, list):
        return []
    found = []
    for option in options:
        if not isinstance(option, Mapping):
            continue
        text = _option_text(option.get("value"))
        if text is not None:
            found.append((text, str(option.get("label") or text), option["value"]))
    return found


def field_selector(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The selector for one input property; ``object`` (YAML) for anything else."""
    labelled = _labelled(schema)
    if labelled:
        return {
            "select": {
                "options": [
                    {"value": text, "label": label} for text, label, _ in labelled
                ]
            }
        }
    enum = schema.get("enum")
    options = [v for v in enum if v is not None] if isinstance(enum, list) else []
    if options and all(isinstance(v, str) for v in options):
        return {"select": {"options": options}}
    kinds = _types(schema)
    kind = kinds[0] if len(kinds) == 1 else None
    if kind == "string" and schema.get("format") == "color":
        return {"color_rgb": {}}
    if kind in ("integer", "number"):
        low, high = schema.get("minimum"), schema.get("maximum")
        number: dict[str, Any] = {}
        # A duration is a number of milliseconds here, not Home Assistant's duration
        # selector, which has no bounds: this keeps the field's own, and its slider.
        if schema.get("format") == "duration":
            number["unit_of_measurement"] = "ms"
        if low is not None:
            number["min"] = low
        if high is not None:
            number["max"] = high
        number["step"] = 1 if kind == "integer" else "any"
        bounded = kind == "integer" and low is not None and high is not None
        # A slider over milliseconds has a step nobody can land on: a box keeps the bounds.
        slider = bounded and "unit_of_measurement" not in number
        number["mode"] = "slider" if slider else "box"
        return {"number": number}
    if kind == "boolean":
        return {"boolean": {}}
    if kind == "string":
        return {"text": {}}
    return {"object": {}}


def _default_held(schema: Mapping[str, Any], held: Mapping[str, Any]) -> Any:
    """A field's default as its selector holds it, which ``command_input`` turns back."""
    default = schema["default"]
    if "select" in held and _labelled(schema):
        text = _option_text(default)
        return default if text is None else text
    is_hex = isinstance(default, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", default)
    if "color_rgb" in held and is_hex:
        return [int(default[i : i + 2], 16) for i in (1, 3, 5)]
    return default


def describe_fields(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """The service fields for a command's input schema: one per property."""
    if not schema:
        return {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, prop in (schema.get("properties") or {}).items():
        field: dict[str, Any] = {"name": prop.get("title") or key}
        if prop.get("description"):
            field["description"] = prop["description"]
        field["required"] = key in required
        field["selector"] = field_selector(prop)
        if "default" in prop:
            field["default"] = _default_held(prop, field["selector"])
        fields[key] = field
    return fields


@dataclass(frozen=True, slots=True)
class PluginCommand:
    """One command as the server lists it."""

    plugin: str
    plugin_name: str
    command: str
    title: str
    description: str
    input: Mapping[str, Any] | None
    output: Any

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PluginCommand:
        """Read a ``PluginCommands`` entry; raises on a malformed one."""
        return cls(
            plugin=str(raw["plugin"]),
            plugin_name=str(raw.get("pluginName") or raw["plugin"]),
            command=str(raw["command"]),
            title=str(raw.get("title") or raw["command"]),
            description=str(raw.get("description") or ""),
            input=raw.get("input") or None,
            output=raw.get("output"),
        )


def _fits(value: Any, kinds: list[str]) -> bool:
    if isinstance(value, bool):
        return "boolean" in kinds
    if isinstance(value, int):
        return "integer" in kinds or "number" in kinds
    return isinstance(value, float) and "number" in kinds


def _is_rgb(value: Any) -> bool:
    return (
        isinstance(value, list | tuple)
        and len(value) == 3
        and all(
            isinstance(part, int) and not isinstance(part, bool) and 0 <= part <= 255
            for part in value
        )
    )


def command_input(data: Mapping[str, Any], command: PluginCommand) -> dict[str, Any]:
    """The call's fields as the command's input; the server checks them.

    A number or boolean given to a text field (from a template or bare YAML) is sent as text.
    What a selector holds differently from the field is turned back: a number among a
    field's options, which a select holds as text, and a colour, which its selector gives
    as red, green and blue.
    """
    properties = (command.input or {}).get("properties") or {}
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key in RESERVED_FIELDS:
            continue
        prop = properties.get(key) or {}
        kinds = _types(prop)
        if isinstance(value, str):
            for text, _, option in _labelled(prop):
                if text == value and not isinstance(option, str):
                    value = option
                    break
        elif prop.get("format") == "color" and _is_rgb(value):
            value = "#{:02x}{:02x}{:02x}".format(*value)
        if (
            "string" in kinds
            and isinstance(value, int | float)
            and not _fits(value, kinds)
        ):
            value = json.dumps(value) if isinstance(value, bool) else str(value)
        values[key] = value
    return values


@dataclass(frozen=True, slots=True)
class _Service:
    """What a registered service runs, and how it was described."""

    plugin: str
    command: str
    supports_response: SupportsResponse
    description: dict[str, Any]


class PluginServices:
    """Every config entry's plugin commands, and the service actions made of them."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start empty; :meth:`async_load` reads the stored lists."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lists: dict[str, list[Any]] = {}
        self._services: dict[str, _Service] = {}
        self._warned: set[str] = set()

    async def async_load(self) -> None:
        """Register the services of each entry's last list."""
        data = await self._store.async_load()
        entries = data.get("entries") if isinstance(data, dict) else None
        known = {
            entry.entry_id for entry in self._hass.config_entries.async_entries(DOMAIN)
        }
        if isinstance(entries, dict):
            self._lists = {
                entry_id: commands
                for entry_id, commands in entries.items()
                if entry_id in known and isinstance(commands, list)
            }
        self.async_reconcile()

    @callback
    def async_set(self, entry_id: str, commands: list[Any]) -> None:
        """An entry's workspace offers these commands now."""
        if self._lists.get(entry_id) == commands:
            return
        self._lists[entry_id] = commands
        self._store.async_delay_save(self._data, SAVE_DELAY)
        self.async_reconcile()

    @callback
    def async_drop(self, entry_id: str) -> None:
        """Forget an entry's commands (it was removed or disabled)."""
        if self._lists.pop(entry_id, None) is None:
            return
        self._store.async_delay_save(self._data, SAVE_DELAY)
        self.async_reconcile()

    @callback
    def async_entry_changed(
        self, change: ConfigEntryChange, entry: ConfigEntry
    ) -> None:
        """Drop a disabled entry's commands, loaded or not (a retrying entry never unloads)."""
        if entry.domain == DOMAIN and entry.disabled_by:
            self.async_drop(entry.entry_id)

    def _data(self) -> dict[str, Any]:
        return {"entries": self._lists}

    def _warn_once(self, message: str, *args: Any) -> None:
        text = message % args
        if text not in self._warned:
            self._warned.add(text)
            _LOGGER.warning(text)

    def _wanted(self, command: PluginCommand) -> tuple[str, _Service] | None:
        name = service_name(command.plugin, command.command)
        if name is None:
            self._warn_once(
                "%s %s has no usable service name", command.plugin, command.command
            )
            return None
        clashing = RESERVED_FIELDS & set((command.input or {}).get("properties") or {})
        if clashing:
            self._warn_once(
                "%s %s isn't a service: Home Assistant reserves its fields %s",
                command.plugin,
                command.command,
                ", ".join(sorted(clashing)),
            )
            return None
        description = {
            "name": f"{command.plugin_name}: {command.title}",
            "description": command.description,
            "fields": describe_fields(command.input),
            "target": TARGET,
        }
        response = (
            SupportsResponse.OPTIONAL
            if command.output is not None
            else SupportsResponse.NONE
        )
        return name, _Service(command.plugin, command.command, response, description)

    @callback
    def async_reconcile(self) -> None:
        """Register, update and remove services to match the entries' lists."""
        # By name, then by (plugin, command): the first entry in config-entry order describes it.
        claims: dict[str, dict[tuple[str, str], _Service]] = {}
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            for raw in self._lists.get(entry.entry_id, []):
                try:
                    wanted = self._wanted(PluginCommand.from_wire(raw))
                except Exception:  # noqa: BLE001 - one bad command must cost only itself
                    self._warn_once(
                        "Skipping a plugin command Drift Beacon sent: %r", raw
                    )
                    continue
                if wanted is not None:
                    name, service = wanted
                    claims.setdefault(name, {}).setdefault(
                        (service.plugin, service.command), service
                    )

        services: dict[str, _Service] = {}
        for name, by_command in claims.items():
            if name in STATIC_SERVICES or len(by_command) > 1:
                for plugin, command in by_command:
                    self._warn_once(
                        "%s %s isn't a service: another action is named %s.%s",
                        plugin,
                        command,
                        DOMAIN,
                        name,
                    )
                continue
            services[name] = next(iter(by_command.values()))

        for name in [name for name in self._services if name not in services]:
            self._hass.services.async_remove(DOMAIN, name)
            del self._services[name]
        for name, service in services.items():
            if self._services.get(name) == service:
                continue
            try:
                # Registering again (a changed description too) tells open frontends to reload it.
                self._hass.services.async_register(
                    DOMAIN,
                    name,
                    self._async_handle,
                    schema=SERVICE_SCHEMA,
                    supports_response=service.supports_response,
                )
                async_set_service_schema(self._hass, DOMAIN, name, service.description)
            except Exception:
                _LOGGER.exception("Cannot register %s.%s", DOMAIN, name)
                continue
            self._services[name] = service

    def _offered(self, entry_id: str, service: _Service) -> PluginCommand | None:
        """The entry's own declaration of the command a service runs, if it offers it."""
        for raw in self._lists.get(entry_id, []):
            if (
                isinstance(raw, Mapping)
                and raw.get("plugin") == service.plugin
                and raw.get("command") == service.command
            ):
                return PluginCommand.from_wire(raw)
        return None

    async def _async_handle(self, call: ServiceCall) -> ServiceResponse:
        full_name = f"{DOMAIN}.{call.service}"
        service = self._services.get(call.service)
        runs: list[tuple[str, DriftBeaconCoordinator, PluginCommand]] = []
        for target in resolve_devices(self._hass, call):
            if target.activity_id is not None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="not_workspace_device",
                    translation_placeholders={"device": target.name},
                )
            entry = target.coordinator.config_entry
            command = self._offered(entry.entry_id, service) if service else None
            if command is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="plugin_command_not_here",
                    translation_placeholders={
                        "workspace": entry.title,
                        "service": full_name,
                    },
                )
            runs.append((target.name, target.coordinator, command))

        if call.return_response:
            if len(runs) != 1:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="plugin_command_one_workspace",
                    translation_placeholders={"service": full_name},
                )
            _, coordinator, command = runs[0]
            output = await coordinator.async_run_plugin_command(
                command.plugin, command.command, command_input(call.data, command)
            )
            return {"output": output}

        await async_run_all(
            [
                (
                    name,
                    partial(
                        coordinator.async_run_plugin_command,
                        command.plugin,
                        command.command,
                        command_input(call.data, command),
                    ),
                )
                for name, coordinator, command in runs
            ]
        )
        return None
