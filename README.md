# Drift Beacon integration

Connects Home Assistant to one Drift Beacon workspace, acting for the user who owns the
access token. Requires Home Assistant 2026.9 or later and a Drift Beacon server ([drift-beacon/ha-app](https://github.com/drift-beacon/ha-app))

## Install

- **HACS:** add `https://github.com/drift-beacon/ha-integration` as a custom repository (category _Integration_), download **Drift Beacon**, and restart Home Assistant.
- **By hand:** copy `custom_components/drift_beacon` into your Home Assistant `config/custom_components/` and restart.

Then add it under Settings → Devices & services, with the hub's address and an access token.

## Devices

- **Workspace device** (model `Workspace`): _Current session_ and _Pinned activity_ sensors,
  _Connected user_ (diagnostic), and a _Stop session_ button.
- **One device per activity** (model `Activity`, model id `span` or `point`), linked to the
  workspace device:
  - _Session_ switch (timed activities): on while you are tracking it.
  - _Mark_ button (point activities): records one occurrence.
  - _Pin_ switch: on while it is your pinned activity.
  - _Progress_ sensor: completed time or mark count for the activity's progress period,
    across all workspace members; the running session is not included.

Every entity is **hidden**, so hundreds of activities never appear on auto-generated
dashboards. Devices still show under Settings → Devices. Unhide an entity to use it on a
dashboard.

Archiving or deleting an activity removes its device. If the activity comes back, its device
returns with the same id, so action targets keep working; automations that use the device's
triggers need a reload (or a restart) before they attach again. Renaming an activity
renames its device unless you gave the device your own name.

## Actions

All actions take **devices** as targets. Entities are hidden, and Home Assistant skips hidden
entities when it expands a device target, so the integration resolves devices itself. Areas,
labels and entity targets are rejected.

| Action                        | Target         | Does                                                                                  |
| ----------------------------- | -------------- | ------------------------------------------------------------------------------------- |
| `drift_beacon.track_activity` | activity       | Timed: start, or stop if already tracking. Point: mark.                               |
| `drift_beacon.pause_activity` | timed activity | End its live session and pin it. Nothing live is a no-op.                             |
| `drift_beacon.pin_activity`   | activity       | Pin it; the previous pin moves to the front of the queue.                             |
| `drift_beacon.unpin_activity` | activity       | Unpin it if pinned; with auto-advance on, the queue head is pinned.                   |
| `drift_beacon.queue_activity` | activity       | Add it to the back of the queue (or pin it, with auto-advance on and nothing pinned). |
| `drift_beacon.stop_session`   | workspace      | Stop your live session, whichever activity it belongs to.                             |
| `drift_beacon.pause_session`  | workspace      | End your live session and pin its activity.                                           |

Ending a shared session ends it for everyone in it.

### Plugin commands

Every command a Drift Beacon plugin provides in a workspace is an action too, named
`drift_beacon.<plugin id>_<command in snake_case>`: magic-cube's `selectPreset` is
`drift_beacon.magic_cube_select_preset`. It targets a **workspace** device and runs as the
token's user there. Its fields are the command's input; a command that declares an output
answers with `{output}` when you ask for a response (one workspace at a time).

```yaml
action: drift_beacon.magic_cube_select_preset
target:
  device_id: <the workspace device>
data:
  preset: Focus
response_variable: result
```

- The actions are those of the plugins enabled in each workspace, including while a plugin
  starts, updates or waits for setup (calls then fail). With several workspaces, an action
  exists while any of them offers it; calling it on one that doesn't is an error.
- They stay while Drift Beacon is down or an entry reloads (calls fail until it's back), and
  go when no workspace offers them: the plugin was disabled or removed, or the entry was.
- Wrong input, or a plugin that isn't installed, enabled or compatible there, is a validation
  error. If the plugin doesn't confirm in 15 s, or stops, the error says the command may have
  run.
- A command gets no action when another action would have the same name, or when its input
  has a field Home Assistant keeps for targets (`entity_id`, `device_id`, `area_id`,
  `floor_id`, `label_id`, `metadata`); the log says which. Needs a Drift Beacon server with
  the `RunPluginCommand` RPC and `PluginCommands` messages; older servers offer none.

## Triggers and events

Activity devices offer device triggers: _Tracking started_, _Tracking stopped_ (timed),
_Marked_ (point), _Pinned_, _Unpinned_ and _Schedule triggered_.

The integration fires these bus events. All carry `workspace_id` and `workspace_device_id`;
activity events add `activity_id`, `activity_device_id`, `activity_name` and `color`
(`[r, g, b]` or `null`).

| Event                             | Extra data                                                                                                                              |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `drift_beacon_session_started`    | `session_id`, `started_at`, `member_ids`                                                                                                |
| `drift_beacon_session_stopped`    | `session_id`, `started_at`                                                                                                              |
| `drift_beacon_activity_pinned`    | `pinned_at`                                                                                                                             |
| `drift_beacon_activity_unpinned`  |                                                                                                                                         |
| `drift_beacon_activity_marked`    | `session_id`, `marked_at`, `member_ids`                                                                                                 |
| `drift_beacon_schedule_triggered` | `schedule_id`, `occurrence_id`, `kind` (`once`, `recurring` or `sinceLast`), `behavior` (`pin`, `queue` or `queueBack`), `triggered_at` |
| `drift_beacon_focus_changed`      | `state` (`live`, `pinned` or `idle`), `previous_state`, `previous_activity_id`, `initial`                                               |

Focus is what you are doing right now: a live session beats a pin, which beats nothing. It is
computed after each server update, so switching straight from one activity to another is a
single `live` → `live` change. It is also announced once when Home Assistant starts
(`initial: true`), and after a reconnect if it changed while disconnected.

`drift_beacon_schedule_triggered` reports one of your schedules pinning or queueing its
activity, as it happens:

- It follows the change the schedule made, so Home Assistant's state is at least as recent
  as the firing. It is the latest state, not the state at that moment: if two schedules pin
  different activities at the same instant, both events fire while the later one is pinned,
  and only that pin fires `drift_beacon_activity_pinned`. Use `behavior` for what the
  schedule did, and the pin switch for what is pinned now.
- It fires once per occurrence (`occurrence_id`), and only while connected: after a
  disconnect or a restart, Home Assistant shows the pin a schedule made in the meantime, but
  no event says that it fired.
- `behavior` is what the schedule does, not the outcome: `queue` adds to the front of the
  queue and `queueBack` to the back, and either pins instead with auto-advance on and nothing
  pinned. More values of `kind` and `behavior` can appear.
- A schedule that skips an occurrence (its target is met, its activity is archived) sends
  nothing, and neither do other members' schedules. Needs a Drift Beacon server with
  `ScheduleTriggered` messages; with an older one the event and its trigger never fire.

## Blueprints

Click a button to import the blueprint into your Home Assistant.

- **Drift Beacon Activity Controls**: pick an activity device, then map any triggers to Track,
  Pause, Pin, Queue and Unpin.

  [![Import Activity Controls blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fdrift-beacon%2Fha-integration%2Fblob%2Fmain%2Fcustom_components%2Fdrift_beacon%2Fblueprints%2Fdrift_beacon_activity_controls.yaml)

- **Drift Beacon Session Controls**: pick the workspace device, then map triggers to Stop or
  Pause your live session, whichever activity it belongs to.

  [![Import Session Controls blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fdrift-beacon%2Fha-integration%2Fblob%2Fmain%2Fcustom_components%2Fdrift_beacon%2Fblueprints%2Fdrift_beacon_session_controls.yaml)

- **Drift Beacon Activity Lighting**: colours lights from `drift_beacon_focus_changed` for the
  chosen workspace. It resyncs when Home Assistant starts and after reconnecting.

  [![Import Activity Lighting blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fdrift-beacon%2Fha-integration%2Fblob%2Fmain%2Fcustom_components%2Fdrift_beacon%2Fblueprints%2Fdrift_beacon_activity_lighting.yaml)

Both controls blueprints work out which mapping fired from the trigger's position, which
renders every mapped trigger. Triggers containing templates (template triggers, numeric
state value templates) fail there, so put those in their own automation that calls the
action directly.

If a device emits overlapping events, such as a press followed by a double press, both mapped
actions may run.

## Connection

The config flow detects `https` or `http`, and verifies TLS certificates unless you turn
**Verify SSL certificate** off (needed for the add-on's self-signed certificate). Change the
address later with **Reconfigure**. If the token stops working, Home Assistant asks for a new
one for the same workspace and user.
