"""Slack Socket Mode bridge — sidecar bot for Cortex Code.

Listens for DMs and interactive button clicks, writes responses to inbox.json
so Cortex Code can pick them up via cron polling.

Usage:
    coco-bridge start          # via shell wrapper
    python -m cortex_slack_bridge.bridge   # direct
"""

import json
import logging
import sys
import time
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from cortex_slack_bridge.config import (
    HISTORY_FILE,
    LOG_FILE,
    PID_FILE,
    ensure_dirs,
    get_active_session,
    get_app_token,
    get_bot_token,
    get_session_inbox,
    get_user_id,
    get_wake_enabled,
    wake_file_for,
)
from cortex_slack_bridge import commands, projects

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("cortex-slack-bridge")

# ---------------------------------------------------------------------------
# Inbox helpers — simple JSON append with no external deps
# ---------------------------------------------------------------------------

def _read_inbox(session_id: str | None = None) -> list[dict]:
    """Read the current inbox entries for a session."""
    inbox = get_session_inbox(session_id or get_active_session())
    if not inbox.exists():
        return []
    try:
        with open(inbox) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _log_history(entry: dict, direction: str):
    """Append a JSONL line to the audit history. Never raises."""
    try:
        record = {**entry, "direction": direction, "logged_at": time.time()}
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # history logging must never break core functionality


def _append_inbox(entry: dict, session_id: str | None = None):
    """Append a message to the session's inbox (atomic-ish via temp file)."""
    ensure_dirs()
    sid = session_id or get_active_session()
    inbox = get_session_inbox(sid)
    entries = _read_inbox(sid)
    entries.append(entry)
    tmp = inbox.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    tmp.replace(inbox)
    log.info("Wrote inbox entry: %s -> session %s", entry.get("type", "unknown"), sid)
    _log_history(entry, "inbound")
    # Optional wake-signal — lets the skill short-circuit its sleep loop.
    if get_wake_enabled():
        try:
            wake_file_for(sid).touch()
        except OSError as exc:
            log.warning("Failed to touch wake file for %s: %s", sid, exc)


def _project_context() -> dict:
    """Return {'name':..., 'path':...} for the active project (or empty)."""
    active = projects.get_active_project()
    if not active:
        return {}
    return {"name": active.get("name"), "path": active.get("path")}


# ---------------------------------------------------------------------------
# Slack App setup
# ---------------------------------------------------------------------------

def create_app() -> App:
    """Create and configure the Slack Bolt app."""
    app = App(token=get_bot_token())
    target_user = get_user_id()

    # --- DM listener -----------------------------------------------------------
    @app.event("message")
    def handle_dm(event, say):
        """Capture DMs from the target user and dispatch commands or inbox-forward."""
        # Only process messages from our user (ignore bot's own messages)
        user = event.get("user")
        subtype = event.get("subtype")
        if subtype or user != target_user:
            return

        text = event.get("text", "")
        log.info("DM received from %s: %s", user, text[:80])

        parsed = commands.parse(text)
        if parsed is None:
            # Free text -> forward to active session inbox as a reply
            _append_inbox({
                "type": "reply",
                "text": text,
                "user": user,
                "ts": event.get("ts", ""),
                "received_at": time.time(),
                "project": _project_context(),
            })
            say("Message sent to CoCo CLI. Awaiting response... :hourglass_flowing_sand:")
            return

        kind = parsed.get("kind")
        if kind == "error":
            say(f":warning: {parsed.get('message', 'Invalid command')}")
            return
        if kind == "inline":
            _handle_inline(parsed, say)
            return
        if kind == "command":
            _append_inbox({
                "type": "command",
                "command": parsed["command"],
                "args": parsed.get("args", {}),
                "user": user,
                "ts": event.get("ts", ""),
                "received_at": time.time(),
                "project": _project_context(),
            })
            say(
                f":inbox_tray: Queued `{parsed['command']}` for project "
                f"*{_project_context().get('name') or 'default'}*. Agent will respond when it picks it up."
            )
            return

    # --- Button action handlers ------------------------------------------------
    @app.action("confirm_approve")
    def handle_approve(ack, body, client):
        """Handle Approve button click."""
        ack()
        action_id = _extract_confirmation_id(body)
        session_id = _extract_session_id(body, client)
        user = body.get("user", {}).get("id", "")
        log.info("Approve clicked by %s for confirmation %s (session %s)", user, action_id, session_id)

        _append_inbox({
            "type": "confirmation",
            "confirmation_id": action_id,
            "response": "approved",
            "user": user,
            "received_at": time.time(),
            "project": _project_context(),
        }, session_id=session_id)

        # Update the original message to show the result
        _update_confirmation_message(client, body, "Approved ✓")

    @app.action("confirm_deny")
    def handle_deny(ack, body, client):
        """Handle Deny button click."""
        ack()
        action_id = _extract_confirmation_id(body)
        session_id = _extract_session_id(body, client)
        user = body.get("user", {}).get("id", "")
        log.info("Deny clicked by %s for confirmation %s (session %s)", user, action_id, session_id)

        _append_inbox({
            "type": "confirmation",
            "confirmation_id": action_id,
            "response": "denied",
            "user": user,
            "received_at": time.time(),
            "project": _project_context(),
        }, session_id=session_id)

        _update_confirmation_message(client, body, "Denied ✗")

    return app


def _handle_inline(parsed: dict, say):
    """Answer simple commands directly from the bridge, no agent roundtrip."""
    op = parsed.get("op")
    if op == "help":
        say(commands.HELP_TEXT)
        return
    if op == "projects":
        entries = projects.list_projects()
        if not entries:
            say(":open_file_folder: No projects registered yet. Start a CoCo session and it will auto-register.")
            return
        lines = ["*Projects* (sessions in parens)"]
        for p in entries:
            marker = " *(active)*" if p["active"] else ""
            sess = f" [{len(p['sessions'])} sess]" if p["sessions"] else ""
            lines.append(f"- `{p['name']}`{marker}{sess} — `{p['path']}`")
        say("\n".join(lines))
        return
    if op == "use":
        name = parsed["args"]["name"]
        if projects.set_active_project(name):
            active = projects.get_active_project() or {}
            say(f":white_check_mark: Active project set to `{name}` (`{active.get('path', '?')}`)")
        else:
            say(f":warning: No project named `{name}`. Use /projects to list.")
        return
    say(f":warning: Unhandled inline op `{op}`")


def _extract_confirmation_id(body: dict) -> str:
    """Pull the confirmation_id from the button's block_id."""
    actions = body.get("actions", [])
    if actions:
        # block_id is set to "confirm_{id}" in notify.py
        block_id = actions[0].get("block_id", "")
        if block_id.startswith("confirm_"):
            return block_id[len("confirm_"):]
    return "unknown"


def _extract_session_id(body: dict, client) -> str | None:
    """Extract the session_id from the original message's metadata.

    Slack includes metadata on the message when sent via chat_postMessage
    with the metadata parameter. For button actions, the original message
    is in body["message"].
    """
    message = body.get("message", {})
    metadata = message.get("metadata", {})
    if metadata.get("event_type") == "cortex_bridge":
        payload = metadata.get("event_payload", {})
        sid = payload.get("session_id")
        if sid:
            return sid
    return None  # falls back to active_session in _append_inbox


def _update_confirmation_message(client, body: dict, result_text: str):
    """Replace the confirmation buttons with a result summary."""
    channel = body.get("channel", {}).get("id", "")
    ts = body.get("message", {}).get("ts", "")
    original_text = body.get("message", {}).get("text", "Confirmation")

    if channel and ts:
        try:
            client.chat_update(
                channel=channel,
                ts=ts,
                text=f"{original_text}\n\n*{result_text}*",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{original_text}\n\n*{result_text}*",
                        },
                    }
                ],
            )
        except Exception as e:
            log.warning("Failed to update confirmation message: %s", e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    """Start the Socket Mode bridge."""
    ensure_dirs()

    # Write PID for the shell wrapper's stop command
    PID_FILE.write_text(str(__import__("os").getpid()))

    # Add file handler now that dirs exist
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)

    log.info("Starting Cortex Code Slack Bridge (Socket Mode)...")
    log.info("Active session: %s", get_active_session())
    log.info("PID:   %s", PID_FILE.read_text().strip())

    app = create_app()
    handler = SocketModeHandler(app, get_app_token())

    try:
        handler.start()  # blocks until interrupted
    except KeyboardInterrupt:
        log.info("Shutting down bridge.")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


if __name__ == "__main__":
    main()
