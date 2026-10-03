# Testing the Drift Beacon Home Assistant integration

The suite runs the integration inside a real Home Assistant instance using
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component),
against a fake Drift Beacon server (`conftest.py`) that speaks the real `/api/ws` JSON-RPC
protocol over a local WebSocket. Nothing talks to a real server.

## Running

From the repository root (the plugin version pins Home Assistant 2026.9.3 and needs Python 3.14):

```sh
uv run --no-project --python 3.14 \
  --with pytest-homeassistant-custom-component==0.13.366 \
  env PYTHONPATH=. pytest -q tests
```

`pyproject.toml` holds the pytest settings (`asyncio_mode = "auto"` is required by the
plugin). Add `-k <name>` to select tests, or a file path to run one module.

## Layout

| File | Covers |
| --- | --- |
| `conftest.py` | `FakeDriftBeacon` server (plugin commands, schedule events only for subscribers that ask, answers that come late or never), config entry and setup fixtures (`make_entry` for more workspaces), `wait_for` |
| `test_models.py` | State reducer, marks and schedule triggers carried for the coordinator, colour parsing |
| `test_init.py` | Setup errors, device model, hidden entities, unload, legacy entries, device deletion |
| `test_coordinator.py` | Events, focus, schedule events (once per occurrence, none from a reconnect), reconnect diffs, grace period, malformed input, device rename/type change/removal |
| `test_entities.py` | Switch, button and sensor state and actions, error propagation, write-on-change |
| `test_services.py` | Device-target resolution and rejection, per-service RPCs, multi-target failures |
| `test_plugin_services.py` | Plugin commands as actions: names, fields and selectors, registration from the first chunk, list changes, reconnects, calls, errors, several workspaces, stored lists |
| `test_config_flow.py` | User, reauth and reconfigure flows |
| `test_device_trigger.py` | Device triggers, diagnostics redaction |
| `test_blueprints.py` | Blueprint schemas; each blueprint run as a real automation |

The WebSocket reader runs outside Home Assistant's task tracking, so tests that wait on
pushed server messages use `wait_for(predicate)` instead of `hass.async_block_till_done()`.

## Style

```sh
uvx ruff check custom_components tests
uvx ruff format --check custom_components tests
```
