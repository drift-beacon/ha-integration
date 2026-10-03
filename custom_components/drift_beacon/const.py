"""Constants for the Drift Beacon integration."""

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "drift_beacon"
MANUFACTURER: Final = "Drift Beacon"

PLATFORMS: Final = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]

# Config entry data
CONF_API_TOKEN: Final = "api_token"
CONF_PROTOCOL: Final = "protocol"
CONF_WORKSPACE_ID: Final = "workspace_id"
CONF_USER_ID: Final = "user_id"
CONF_USER_NAME: Final = "user_name"

DEFAULT_HOST: Final = "local-drift-beacon"
DEFAULT_PORT: Final = 9000

# Transport
STATUS_PATH: Final = "/api/device/status"
WS_PATH: Final = "/api/ws"
REQUEST_TIMEOUT: Final = 10  # seconds, per RPC and per handshake
# A plugin command's budget on the server (10 s) plus a margin for its `timeout` answer.
PLUGIN_COMMAND_TIMEOUT: Final = 15  # seconds
HEARTBEAT_INTERVAL: Final = 30  # seconds
RECONNECT_MIN_DELAY: Final = 1  # seconds
RECONNECT_MAX_DELAY: Final = 60  # seconds
WORKSPACE_MISSING_RETRY_DELAY: Final = 300  # seconds
# How long a connection must stay up before the reconnect backoff starts over.
STABLE_CONNECTION_TIME: Final = 60  # seconds
# Keep entities available across short reconnects instead of flapping every blip.
UNAVAILABLE_GRACE_PERIOD: Final = 30  # seconds

# Local add-on discovery
DETECTION_CANDIDATES: Final = [
    ("local-drift-beacon", DEFAULT_PORT),
    ("homeassistant.local", DEFAULT_PORT),
    ("localhost", DEFAULT_PORT),
]
DETECTION_TIMEOUT: Final = 2  # seconds

# Device models (used by selectors in services.yaml and the blueprints)
MODEL_WORKSPACE: Final = "Workspace"
MODEL_ACTIVITY: Final = "Activity"

# Services
SERVICE_TRACK_ACTIVITY: Final = "track_activity"
SERVICE_PAUSE_ACTIVITY: Final = "pause_activity"
SERVICE_PIN_ACTIVITY: Final = "pin_activity"
SERVICE_UNPIN_ACTIVITY: Final = "unpin_activity"
SERVICE_QUEUE_ACTIVITY: Final = "queue_activity"
SERVICE_STOP_SESSION: Final = "stop_session"
SERVICE_PAUSE_SESSION: Final = "pause_session"

# Bus events
EVENT_SESSION_STARTED: Final = "drift_beacon_session_started"
EVENT_SESSION_STOPPED: Final = "drift_beacon_session_stopped"
EVENT_ACTIVITY_PINNED: Final = "drift_beacon_activity_pinned"
EVENT_ACTIVITY_UNPINNED: Final = "drift_beacon_activity_unpinned"
EVENT_ACTIVITY_MARKED: Final = "drift_beacon_activity_marked"
EVENT_SCHEDULE_TRIGGERED: Final = "drift_beacon_schedule_triggered"
EVENT_FOCUS_CHANGED: Final = "drift_beacon_focus_changed"

# Event and attribute keys
ATTR_WORKSPACE_ID: Final = "workspace_id"
ATTR_WORKSPACE_DEVICE_ID: Final = "workspace_device_id"
ATTR_ACTIVITY_ID: Final = "activity_id"
ATTR_ACTIVITY_DEVICE_ID: Final = "activity_device_id"
ATTR_ACTIVITY_NAME: Final = "activity_name"
ATTR_COLOR: Final = "color"
ATTR_SESSION_ID: Final = "session_id"
ATTR_STARTED_AT: Final = "started_at"
ATTR_MARKED_AT: Final = "marked_at"
ATTR_PINNED_AT: Final = "pinned_at"
ATTR_MEMBER_IDS: Final = "member_ids"
ATTR_SCHEDULE_ID: Final = "schedule_id"
ATTR_OCCURRENCE_ID: Final = "occurrence_id"
ATTR_KIND: Final = "kind"
ATTR_BEHAVIOR: Final = "behavior"
ATTR_TRIGGERED_AT: Final = "triggered_at"
ATTR_STATE: Final = "state"
ATTR_PREVIOUS_STATE: Final = "previous_state"
ATTR_PREVIOUS_ACTIVITY_ID: Final = "previous_activity_id"
ATTR_INITIAL: Final = "initial"
ATTR_TARGET: Final = "target"
ATTR_UNIT: Final = "unit"
ATTR_DESCRIPTION: Final = "description"
ATTR_CATEGORY_ID: Final = "category_id"
ATTR_CATEGORY_NAME: Final = "category_name"
ATTR_TRACKING_TYPE: Final = "tracking_type"
ATTR_USER_ID: Final = "user_id"

# Focus states carried by EVENT_FOCUS_CHANGED
FOCUS_LIVE: Final = "live"
FOCUS_PINNED: Final = "pinned"
FOCUS_IDLE: Final = "idle"

# Server error reasons (IntegrationWsError.reason) handled specially
REASON_NO_LIVE_SESSION: Final = "NoLiveSession"
REASON_NOT_PINNED: Final = "NotPinned"
