"""CLI tool for sending Slack notifications from Cortex Code.

Usage:
    # Plain notification
    coco-notify "Task completed successfully"

    # Notification with confirmation buttons (prints approved/denied to stdout)
    coco-notify --confirm "Run DROP TABLE staging.events?" --id drop-events-123

    # Programmatic usage
    from cortex_slack_bridge.notify import send_message, send_confirmation
    send_message("Build finished.")
    result = send_confirmation("Deploy to prod?", confirmation_id="deploy-42")
"""

import argparse
import json
import sys
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from cortex_slack_bridge.config import (
    HISTORY_FILE,
    ensure_dirs,
    get_bot_token,
    get_session_id,
    get_session_inbox,
    get_user_id,
    set_active_session,
)
from cortex_slack_bridge import projects as _projects

# ---------------------------------------------------------------------------
# Inbox reader (for polling confirmation responses)
# ---------------------------------------------------------------------------

def _read_inbox(session_id: str | None = None) -> list[dict]:
    inbox = get_session_inbox(session_id)
    if not inbox.exists():
        return []
    try:
        with open(inbox) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_inbox(entries: list[dict], session_id: str | None = None):
    ensure_dirs()
    inbox = get_session_inbox(session_id)
    tmp = inbox.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    tmp.replace(inbox)


def _log_history(entry: dict, direction: str):
    """Append a JSONL line to the audit history. Never raises."""
    try:
        record = {**entry, "direction": direction, "logged_at": time.time()}
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _pop_confirmation(confirmation_id: str, session_id: str | None = None) -> dict | None:
    """Check inbox for a confirmation response and remove it if found."""
    entries = _read_inbox(session_id)
    for i, entry in enumerate(entries):
        if (
            entry.get("type") == "confirmation"
            and entry.get("confirmation_id") == confirmation_id
        ):
            entries.pop(i)
            _write_inbox(entries, session_id)
            _log_history(entry, "consumed")
            return entry
    return None


# ---------------------------------------------------------------------------
# DM channel helper
# ---------------------------------------------------------------------------

def _open_dm(client: WebClient, user_id: str) -> str:
    """Open (or retrieve) a DM channel with the target user."""
    resp = client.conversations_open(users=[user_id])
    return resp["channel"]["id"]


# ---------------------------------------------------------------------------
# Message type → color mapping (Format 3: color-coded attachments)
# ---------------------------------------------------------------------------

MSG_TYPE_COLORS = {
    "status":  "#2196F3",   # blue — progress/status updates
    "success": "#4CAF50",   # green — successful results
    "warning": "#FF9800",   # yellow/orange — warnings
    "error":   "#F44336",   # red — errors
}

MSG_TYPE_ICONS = {
    "status":  ":hourglass_flowing_sand:",
    "success": ":white_check_mark:",
    "warning": ":warning:",
    "error":   ":x:",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_message(
    text: str,
    *,
    blocks: list | None = None,
    msg_type: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Send a DM notification to the configured Slack user.

    Args:
        text: Message text (also used as fallback for notifications).
        blocks: Optional custom Block Kit blocks (overrides msg_type).
        msg_type: One of "status", "success", "warning", "error".
            When set, the message is sent as a color-coded attachment.
            Ignored if blocks is provided.
        session_id: Cortex Code session ID for routing.

    Returns the Slack API response dict.
    """
    sid = session_id or get_session_id()
    client = WebClient(token=get_bot_token())
    user_id = get_user_id()
    channel = _open_dm(client, user_id)

    # Auto-register this session under the current project, so free-text DMs
    # from Slack route back to us.
    try:
        _projects.auto_register_cwd(session_id=sid)
    except Exception:
        pass  # registry is a convenience, never fail the notify

    project_info = _projects.get_active_project() or {}

    # Tag message with session ID + project so the bridge can route replies back
    metadata = {
        "event_type": "cortex_bridge",
        "event_payload": {
            "session_id": sid,
            "project": {
                "name": project_info.get("name"),
                "path": project_info.get("path"),
            },
        },
    }

    # Color-coded attachment mode
    if msg_type and msg_type in MSG_TYPE_COLORS and blocks is None:
        icon = MSG_TYPE_ICONS[msg_type]
        # Use blocks instead of attachments to avoid text+attachment duplication.
        # The `text` param is only shown in push notifications, not in the chat
        # UI, when blocks are present.
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{icon} {text}"},
            }
        ]
        try:
            resp = client.chat_postMessage(
                channel=channel,
                text=f"{icon} {text}",  # fallback for push notifications only
                blocks=blocks,
                metadata=metadata,
            )
            set_active_session(sid)
            _log_history({"type": "notification", "text": text, "msg_type": msg_type, "session_id": sid}, "outbound")
            return resp.data
        except SlackApiError as e:
            print(f"Slack API error: {e.response['error']}", file=sys.stderr)
            raise

    # Default block mode
    if blocks is None:
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ]

    try:
        resp = client.chat_postMessage(
            channel=channel, text=text, blocks=blocks, metadata=metadata
        )
        set_active_session(sid)
        _log_history({"type": "notification", "text": text, "session_id": sid}, "outbound")
        return resp.data
    except SlackApiError as e:
        print(f"Slack API error: {e.response['error']}", file=sys.stderr)
        raise


def send_confirmation(
    question: str,
    *,
    confirmation_id: str,
    session_id: str | None = None,
    timeout: float = 300,
    poll_interval: float = 2,
) -> str:
    """Send Approve/Deny buttons and wait for a response.

    Args:
        question: The question to display.
        confirmation_id: Unique ID to correlate the response.
        session_id: Cortex Code session ID for routing.
        timeout: Max seconds to wait (default 5 min).
        poll_interval: Seconds between inbox polls.

    Returns:
        "approved" or "denied".

    Raises:
        TimeoutError: If no response within timeout.
    """
    sid = session_id or get_session_id()
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":question: *{question}*"},
        },
        {
            "type": "actions",
            "block_id": f"confirm_{confirmation_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "confirm_approve",
                    "value": confirmation_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "confirm_deny",
                    "value": confirmation_id,
                },
            ],
        },
    ]

    send_message(question, blocks=blocks, session_id=sid)
    _log_history({"type": "confirmation_request", "text": question, "confirmation_id": confirmation_id, "session_id": sid}, "outbound")

    # Poll session-specific inbox for the button response
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _pop_confirmation(confirmation_id, session_id=sid)
        if result:
            return result["response"]
        time.sleep(poll_interval)

    raise TimeoutError(
        f"No confirmation response for '{confirmation_id}' within {timeout}s"
    )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Send Slack notifications from Cortex Code"
    )
    parser.add_argument("message", help="Message text to send")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Send as confirmation with Approve/Deny buttons",
    )
    parser.add_argument(
        "--id",
        dest="confirmation_id",
        default=None,
        help="Confirmation ID (required with --confirm)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Timeout in seconds for confirmation (default: 300)",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Cortex Code session ID (defaults to CORTEX_SESSION_ID env var)",
    )
    parser.add_argument(
        "--type",
        dest="msg_type",
        choices=["status", "success", "warning", "error"],
        default=None,
        help="Message type for color-coded attachments (blue/green/yellow/red)",
    )

    args = parser.parse_args()

    ensure_dirs()

    if args.confirm:
        cid = args.confirmation_id or f"confirm-{int(time.time())}"
        try:
            result = send_confirmation(
                args.message,
                confirmation_id=cid,
                session_id=args.session,
                timeout=args.timeout,
            )
            print(result)
        except TimeoutError as e:
            print(f"timeout: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        send_message(
            args.message, msg_type=args.msg_type, session_id=args.session
        )
        print("sent")


if __name__ == "__main__":
    main()
