"""Configuration for cortex-slack-bridge."""

import json
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BRIDGE_DIR = Path.home() / ".cortex-slack-bridge"
INBOX_FILE = BRIDGE_DIR / "inbox.json"  # legacy single-session fallback
PID_FILE = BRIDGE_DIR / "bridge.pid"
LOG_FILE = BRIDGE_DIR / "bridge.log"
ACTIVE_SESSION_FILE = BRIDGE_DIR / "active_session"
HISTORY_FILE = BRIDGE_DIR / "history.jsonl"

# ---------------------------------------------------------------------------
# Slack tokens
#
# Preferred: set via environment variables (Cortex secret injection).
#   SLACK_BRIDGE_APP_TOKEN  — xapp-... (Socket Mode)
#   SLACK_BRIDGE_BOT_TOKEN  — xoxb-... (Bot API calls)
#
# Fallback: a JSON config file at ~/.cortex-slack-bridge/config.json
#   { "app_token": "xapp-...", "bot_token": "xoxb-...", "user_id": "U..." }
# ---------------------------------------------------------------------------
CONFIG_FILE = BRIDGE_DIR / "config.json"


def _load_file_config() -> dict:
    """Load optional JSON config file."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# macOS Keychain helpers (zero external dependencies)
# ---------------------------------------------------------------------------
KEYCHAIN_SERVICE = "coco-slack-bridge"


def keychain_get(key: str) -> str | None:
    """Read a value from macOS Keychain. Returns None if not found."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # not on macOS or keychain unavailable
    return None


def keychain_set(key: str, value: str) -> bool:
    """Store a value in macOS Keychain. Returns True on success."""
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key, "-w", value, "-U"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def keychain_delete(key: str) -> bool:
    """Remove a value from macOS Keychain. Returns True on success."""
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_app_token() -> str:
    """Return the Slack App-Level token (xapp-...) for Socket Mode."""
    token = os.environ.get("SLACK_BRIDGE_APP_TOKEN")
    if token:
        return token
    token = keychain_get("app_token")
    if token:
        return token
    token = _load_file_config().get("app_token")
    if token:
        return token
    raise RuntimeError(
        "Missing SLACK_BRIDGE_APP_TOKEN. Set the env var, run "
        "'coco-bridge setup-keychain', or add 'app_token' to "
        "~/.cortex-slack-bridge/config.json"
    )


def get_bot_token() -> str:
    """Return the Slack Bot token (xoxb-...) for API calls."""
    token = os.environ.get("SLACK_BRIDGE_BOT_TOKEN")
    if token:
        return token
    token = keychain_get("bot_token")
    if token:
        return token
    token = _load_file_config().get("bot_token")
    if token:
        return token
    raise RuntimeError(
        "Missing SLACK_BRIDGE_BOT_TOKEN. Set the env var, run "
        "'coco-bridge setup-keychain', or add 'bot_token' to "
        "~/.cortex-slack-bridge/config.json"
    )


def get_user_id() -> str:
    """Return your Slack user ID (U...) for DM targeting."""
    uid = os.environ.get("SLACK_BRIDGE_USER_ID")
    if uid:
        return uid
    uid = keychain_get("user_id")
    if uid:
        return uid
    uid = _load_file_config().get("user_id")
    if uid:
        return uid
    raise RuntimeError(
        "Missing SLACK_BRIDGE_USER_ID. Set the env var, run "
        "'coco-bridge setup-keychain', or add 'user_id' to "
        "~/.cortex-slack-bridge/config.json"
    )


def ensure_dirs():
    """Create the bridge directory if it doesn't exist."""
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Session management — multi-session inbox routing
# ---------------------------------------------------------------------------

def get_session_id() -> str:
    """Return the current Cortex Code session ID, or 'default'."""
    return os.environ.get("CORTEX_SESSION_ID", "default")


def get_session_inbox(session_id: str | None = None) -> Path:
    """Return the inbox path for a specific session.

    Falls back to INBOX_FILE for session_id='default' (backward compat).
    """
    sid = session_id or get_session_id()
    if sid == "default":
        return INBOX_FILE
    return BRIDGE_DIR / f"inbox_{sid}.json"


def get_active_session() -> str:
    """Return the most recently active session ID.

    Prefers the active project's most-recently-used session (from the
    project registry). Falls back to the legacy ACTIVE_SESSION_FILE,
    then to 'default'.
    """
    # Lazy import to avoid circular dependency (projects imports from config)
    try:
        from cortex_slack_bridge import projects as _projects

        sid = _projects.active_session_for_project()
        if sid:
            return sid
    except Exception:
        pass

    if ACTIVE_SESSION_FILE.exists():
        try:
            return ACTIVE_SESSION_FILE.read_text().strip()
        except OSError:
            pass
    return "default"


def set_active_session(session_id: str):
    """Mark a session as the most recently active."""
    ensure_dirs()
    ACTIVE_SESSION_FILE.write_text(session_id)


# ---------------------------------------------------------------------------
# Bridge configuration (poll interval, wake signal)
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL = 5  # seconds between in-prompt inbox reads
MIN_POLL_INTERVAL = 2
MAX_POLL_INTERVAL = 30


def get_poll_interval() -> int:
    """Return the configured inbox-poll interval in seconds.

    Priority: env var > config.json > default. Clamped to [MIN, MAX].
    """
    raw = os.environ.get("COCO_BRIDGE_POLL_INTERVAL")
    if raw is None:
        raw = _load_file_config().get("poll_interval_seconds")
    try:
        val = int(raw) if raw is not None else DEFAULT_POLL_INTERVAL
    except (TypeError, ValueError):
        val = DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, val))


def set_poll_interval(seconds: int) -> int:
    """Persist the poll interval in ~/.cortex-slack-bridge/config.json."""
    seconds = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, int(seconds)))
    ensure_dirs()
    cfg = _load_file_config()
    cfg["poll_interval_seconds"] = seconds
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_FILE)
    return seconds


def get_wake_enabled() -> bool:
    raw = os.environ.get("COCO_BRIDGE_WAKE_ENABLED")
    if raw is None:
        raw = _load_file_config().get("poll_wake_enabled")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return False


def wake_file_for(session_id: str) -> Path:
    return BRIDGE_DIR / f"wake_{session_id or 'default'}"
