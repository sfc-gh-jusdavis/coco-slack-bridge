"""Slack Socket Mode bridge — sidecar bot for Cortex Code.

Listens for DMs and interactive button clicks. In headless mode, dispatches
messages directly to `cortex -p` subprocesses for immediate responses.
In terminal mode, writes to inbox JSON files for cron-based polling.

Usage:
    coco-bridge start          # via shell wrapper
    python -m cortex_slack_bridge.bridge   # direct
"""

import json
import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
    get_bridge_mode,
    get_session_for_thread,
    get_session_inbox,
    get_user_id,
    get_wake_enabled,
    list_thread_sessions,
    register_thread_session,
    set_thread_ts,
    wake_file_for,
)
from cortex_slack_bridge import commands, projects, sessions

# Thread pool for running cortex subprocesses without blocking the event loop
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cortex")

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


def _inbox_age_str(inbox_path: Path) -> str:
    """Return a human-readable age string for an inbox file, with activity indicator."""
    if not inbox_path.exists():
        return ":black_circle: never written"
    age_secs = time.time() - inbox_path.stat().st_mtime
    if age_secs < 120:
        return f":large_green_circle: active ({int(age_secs)}s ago)"
    if age_secs < 600:
        return f":large_yellow_circle: {int(age_secs // 60)}m ago"
    if age_secs < 3600:
        return f":white_circle: {int(age_secs // 60)}m ago"
    return f":black_circle: {int(age_secs // 3600)}h ago"


# ---------------------------------------------------------------------------
# Slack App setup
# ---------------------------------------------------------------------------

def create_app() -> App:
    """Create and configure the Slack Bolt app."""
    app = App(token=get_bot_token())
    target_user = get_user_id()

    # --- DM listener -----------------------------------------------------------
    @app.event("message")
    def handle_dm(event, say, client):
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
            # Free text -> dispatch based on bridge mode
            user_ts = event.get("ts", "")
            thread_ts = event.get("thread_ts")  # present only for threaded replies
            channel_id = event.get("channel", "")

            # Add eyes reaction as acknowledgement
            if user_ts and channel_id:
                try:
                    client.reactions_add(
                        channel=channel_id,
                        timestamp=user_ts,
                        name="eyes",
                    )
                except Exception as exc:
                    log.warning("Failed to add reaction: %s", exc)

            mode = get_bridge_mode()
            if mode == "headless":
                _dispatch_headless(text, user_ts, thread_ts, channel_id, client)
            else:
                _dispatch_terminal(text, user, user_ts, thread_ts)
            return

        kind = parsed.get("kind")
        if kind == "error":
            say(f":warning: {parsed.get('message', 'Invalid command')}")
            return
        if kind == "inline":
            _handle_inline(parsed, say, event, client)
            return
        if kind == "command":
            _append_inbox({
                "type": "command",
                "command": parsed["command"],
                "args": parsed.get("args", {}),
                "user": user,
                "ts": event.get("ts", ""),
                "thread_ts": event.get("thread_ts"),
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


def _dispatch_headless(text: str, user_ts: str, thread_ts: str | None, channel_id: str, client):
    """Dispatch a message to a headless cortex process and reply in Slack."""

    def _run():
        try:
            # Determine project path from active project
            active_proj = projects.get_active_project() or {}
            project_path = active_proj.get("path", str(Path.home()))
            project_name = active_proj.get("name")

            # Look up existing session for this thread
            reply_thread = thread_ts or user_ts
            existing = sessions.get_session_for_thread(reply_thread) if reply_thread else None
            session_id = existing["session_id"] if existing else None

            if existing:
                project_path = existing.get("project_path", project_path)
                log.info("Headless: resuming session %s for thread %s", session_id, reply_thread)
            else:
                log.info("Headless: new session for thread %s in project %s", reply_thread, project_path)

            # Run cortex
            result = sessions.run_cortex(
                message=text,
                project_path=project_path,
                session_id=session_id,
            )

            # Register the thread-session mapping
            if result.get("session_id") and reply_thread:
                sessions.register_session(
                    thread_ts=reply_thread,
                    session_id=result["session_id"],
                    project_path=project_path,
                    project_name=project_name,
                )

            # Send response to Slack
            response_text = result.get("response")
            error = result.get("error")

            if response_text:
                # Truncate long responses for Slack (max ~4000 chars)
                if len(response_text) > 3900:
                    response_text = response_text[:3900] + "\n\n_(truncated — full response in CLI)_"
                client.chat_postMessage(
                    channel=channel_id,
                    text=response_text,
                    thread_ts=reply_thread,
                )
            elif error:
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: Cortex error: {error}",
                    thread_ts=reply_thread,
                )
            else:
                client.chat_postMessage(
                    channel=channel_id,
                    text=":warning: Cortex returned no response.",
                    thread_ts=reply_thread,
                )

            _log_history({
                "type": "headless_dispatch",
                "text": text[:200],
                "session_id": result.get("session_id"),
                "duration_ms": result.get("duration_ms"),
                "has_response": bool(response_text),
            }, "inbound")

        except Exception as exc:
            log.error("Headless dispatch error: %s", exc, exc_info=True)
            try:
                reply_thread = thread_ts or user_ts
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: Bridge error: {exc}",
                    thread_ts=reply_thread,
                )
            except Exception:
                pass

    # Run in thread pool so bridge stays responsive
    _executor.submit(_run)


def _dispatch_terminal(text: str, user: str, user_ts: str, thread_ts: str | None):
    """Legacy terminal mode: write to inbox for cron-based polling."""
    # Determine target session: thread registry > active session
    if thread_ts:
        mapping = get_session_for_thread(thread_ts)
        if mapping:
            sid = mapping["session_id"]
            log.info("Thread-routed: ts=%s -> session %s (project %s)",
                     thread_ts, sid, mapping.get("project"))
        else:
            sid = get_active_session()
            log.info("Threaded reply with no mapping (thread_ts=%s), falling back to active session %s",
                     thread_ts, sid)
    else:
        sid = get_active_session()

    _append_inbox({
        "type": "reply",
        "text": text,
        "user": user,
        "ts": user_ts,
        "thread_ts": thread_ts,
        "received_at": time.time(),
        "project": _project_context(),
    }, session_id=sid)

    reply_thread = thread_ts or user_ts
    if reply_thread:
        set_thread_ts(reply_thread, session_id=sid)
        log.info("Thread context saved: ts=%s session=%s", reply_thread, sid)


def _handle_inline(parsed: dict, say, event: dict = None, client=None):
    """Answer simple commands directly from the bridge, no agent roundtrip."""
    op = parsed.get("op")
    if op == "help":
        say(commands.HELP_TEXT)
        return
    if op == "status":
        active_proj = projects.get_active_project() or {}
        proj_name = active_proj.get("name") or "none"
        proj_path = active_proj.get("path") or "—"
        from cortex_slack_bridge.config import get_active_session, get_poll_interval
        mode = get_bridge_mode()
        lines = [
            "*Bridge status*",
            f":gear: Mode: *{mode}*",
            f":file_folder: Active project: `{proj_name}`  `{proj_path}`",
        ]
        if mode == "headless":
            headless = sessions.list_sessions()
            lines.append(f":zap: Headless sessions: {len(headless)}")
            for h in headless[:5]:
                ts_short = h["thread_ts"][:12] + "…"
                age = int(time.time() - h.get("last_active", 0))
                age_str = f"{age}s" if age < 60 else f"{age // 60}m"
                lines.append(
                    f"  • `{ts_short}` → `{h['session_id'][:12]}…` "
                    f"project=`{h.get('project_name') or '?'}` active {age_str} ago"
                )
        else:
            sid = get_active_session()
            inbox = get_session_inbox(sid)
            age = _inbox_age_str(inbox)
            poll = get_poll_interval()
            lines.append(f":id: Active session: `{sid}`")
            lines.append(f":inbox_tray: Inbox: {age}")
            lines.append(f":timer_clock: Poll interval: {poll}s")
        say("\n".join(lines))
        return
    if op == "projects":
        entries = projects.list_projects()
        if not entries:
            say(":open_file_folder: No projects registered yet. Start a CoCo session and it will auto-register.")
            return
        lines = ["*Projects & sessions*"]
        for p in entries:
            marker = " *(routed here)*" if p["active"] else ""
            lines.append(f"\n:file_folder: *{p['name']}*{marker}  `{p['path']}`")
            if p["sessions"]:
                for sid in p["sessions"]:
                    inbox = get_session_inbox(sid)
                    age = _inbox_age_str(inbox)
                    lines.append(f"  • `{sid[:12]}…`  {age}")
            else:
                lines.append("  _no sessions registered_")
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
    if op == "threads":
        entries = list_thread_sessions()
        if not entries:
            say(":thread: No active thread-session mappings.")
            return
        lines = ["*Thread-session mappings*"]
        for e in entries[:10]:
            ts_short = e["thread_ts"][:12] + "…"
            age = int(time.time() - e.get("last_active", 0))
            age_str = f"{age}s" if age < 60 else f"{age // 60}m"
            lines.append(
                f"  • `{ts_short}` → session `{e['session_id'][:12]}…` "
                f"project=`{e.get('project') or '?'}` active {age_str} ago"
            )
        say("\n".join(lines))
        return
    if op == "new":
        _handle_new_session(parsed.get("args", {}), say, event, client)
        return
    if op == "mode":
        _handle_mode(parsed.get("args", {}), say)
        return
    if op == "open":
        _handle_open(parsed.get("args", {}), say, event)
        return
    if op == "sessions":
        _handle_sessions(say)
        return
    if op == "kill":
        _handle_kill(parsed.get("args", {}), say, event)
        return
    say(f":warning: Unhandled inline op `{op}`")


def _handle_new_session(args: dict, say, event: dict = None, client=None):
    """Start a new CoCo session for a project.

    In headless mode: runs cortex -p in a thread pool and replies in-thread.
    In terminal mode: spawns a Terminal.app window via osascript.
    """
    project_name = args.get("project", "")
    prompt = args.get("prompt", "")

    # Look up project path from registry
    entries = projects.list_projects()
    match = None
    for p in entries:
        if p["name"].lower() == project_name.lower():
            match = p
            break

    if not match:
        names = ", ".join(f"`{p['name']}`" for p in entries) if entries else "_none_"
        say(f":warning: No project named `{project_name}`. Known projects: {names}")
        return

    project_path = match["path"]
    mode = get_bridge_mode()

    if mode == "headless":
        if not client or not event:
            say(":warning: Internal error — missing Slack context for headless dispatch.")
            return
        channel_id = event.get("channel", "")
        user_ts = event.get("ts", "")
        # Thread everything under the user's !new message
        reply_thread = user_ts
        initial_msg = prompt or f"New session started for project {project_name}. How can I help?"

        client.chat_postMessage(
            channel=channel_id,
            text=f":rocket: Starting new headless session in `{project_name}` (`{project_path}`)…",
            thread_ts=reply_thread,
        )

        def _run():
            try:
                result = sessions.run_cortex(
                    message=initial_msg,
                    project_path=project_path,
                )

                response_text = result.get("response")
                error = result.get("error")

                reply_body = ""
                if response_text:
                    if len(response_text) > 3900:
                        response_text = response_text[:3900] + "\n\n_(truncated)_"
                    reply_body = (
                        f":zap: *New session* in `{project_name}`\n\n{response_text}"
                    )
                elif error:
                    reply_body = f":x: Cortex error: {error}"
                else:
                    reply_body = f":zap: Session started in `{project_name}` (no initial response)"

                # Reply in the same thread as the !new command
                client.chat_postMessage(
                    channel=channel_id, text=reply_body, thread_ts=reply_thread,
                )

                # Register the thread → session mapping so follow-ups continue here
                if result.get("session_id") and reply_thread:
                    sessions.register_session(
                        thread_ts=reply_thread,
                        session_id=result["session_id"],
                        project_path=project_path,
                        project_name=project_name,
                    )
                    log.info(
                        "New headless session: project=%s session=%s thread=%s",
                        project_name, result["session_id"], reply_thread,
                    )
            except Exception as exc:
                log.error("New headless session error: %s", exc, exc_info=True)
                try:
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f":x: Failed to start session: {exc}",
                        thread_ts=reply_thread,
                    )
                except Exception:
                    pass

        _executor.submit(_run)
    else:
        # Terminal mode — spawn via osascript
        cortex_cmd = "cortex"
        if prompt:
            safe_prompt = prompt.replace("'", "'\\''")
            cortex_cmd = f"cortex -p '{safe_prompt}'"

        applescript = (
            f'tell application "Terminal" to do script '
            f'"cd \'{project_path}\' && {cortex_cmd}"'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                prompt_note = f" with prompt: _{prompt}_" if prompt else ""
                say(
                    f":rocket: Started new CoCo session in `{project_name}` "
                    f"(`{project_path}`){prompt_note}\n"
                    f"It will auto-register once running. Use `!projects` to check."
                )
                log.info("Spawned terminal session for project %s at %s", project_name, project_path)
            else:
                say(f":x: Failed to open Terminal: {result.stderr.strip()}")
                log.error("osascript failed: %s", result.stderr.strip())
        except subprocess.TimeoutExpired:
            say(":x: Timed out trying to open Terminal.")
            log.error("osascript timed out spawning session for %s", project_name)
        except Exception as exc:
            say(f":x: Error spawning session: {exc}")
            log.error("Failed to spawn session: %s", exc)


def _handle_mode(args: dict, say):
    """Switch bridge mode between headless and terminal."""
    from cortex_slack_bridge.config import set_bridge_mode
    new_mode = args.get("mode", "").strip().lower()
    if new_mode not in ("headless", "terminal"):
        current = get_bridge_mode()
        say(
            f":gear: Current mode: *{current}*\n"
            f"Usage: `!mode headless` or `!mode terminal`"
        )
        return
    result = set_bridge_mode(new_mode)
    say(f":gear: Bridge mode set to *{result}*. Takes effect on next message.")


def _handle_open(args: dict, say, event: dict = None):
    """Open an interactive Terminal.app session, resuming the headless session if in a thread."""
    thread_ts = event.get("thread_ts") if event else None
    project_name = args.get("project", "")

    session_id = None
    project_path = None

    # Check if the argument is a numbered session reference (#1, #2, etc.)
    if project_name.startswith("#"):
        resolved = _resolve_session_ref(project_name)
        if resolved:
            session_id = resolved["session_id"]
            project_path = resolved.get("project_path")
            project_name = resolved.get("project_name") or "unknown"
        else:
            say(f":warning: No session `{project_name}`. Use `!sessions` to list.")
            return

    # If used inside a thread, try to resume that thread's headless session
    elif thread_ts:
        existing = sessions.get_session_for_thread(thread_ts)
        if existing:
            session_id = existing["session_id"]
            project_path = existing.get("project_path")
            project_name = project_name or existing.get("project_name") or "unknown"

    # If no thread context or no session found, look up by project name
    if not project_path and project_name:
        entries = projects.list_projects()
        for p in entries:
            if p["name"].lower() == project_name.lower():
                project_path = p["path"]
                break

    if not project_path:
        if project_name:
            names = ", ".join(f"`{p['name']}`" for p in projects.list_projects())
            say(f":warning: No project named `{project_name}`. Known: {names}")
        else:
            say(
                ":warning: Usage: `!open <project>` or reply `!open` in a session thread.\n"
                "In a thread, it resumes the headless session in Terminal."
            )
        return

    # Build cortex command — resume if we have a session_id
    if session_id:
        cortex_cmd = f"cortex --resume {session_id}"
    else:
        cortex_cmd = "cortex"

    prompt = args.get("prompt", "")
    if prompt and not session_id:
        safe_prompt = prompt.replace("'", "'\\''")
        cortex_cmd = f"cortex -p '{safe_prompt}'"

    applescript = (
        f'tell application "Terminal" to do script '
        f'"cd \'{project_path}\' && {cortex_cmd}"'
    )

    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            resume_note = f" (resuming session `{session_id[:12]}…`)" if session_id else ""
            say(
                f":desktop_computer: Opened Terminal for `{project_name}`"
                f"{resume_note}\n"
                f"Path: `{project_path}`"
            )
            log.info("Opened terminal: project=%s session=%s", project_name, session_id)
        else:
            say(f":x: Failed to open Terminal: {result.stderr.strip()}")
            log.error("osascript failed for !open: %s", result.stderr.strip())
    except subprocess.TimeoutExpired:
        say(":x: Timed out trying to open Terminal.")
    except Exception as exc:
        say(f":x: Error opening Terminal: {exc}")
        log.error("!open error: %s", exc)


def _handle_sessions(say):
    """List all headless sessions with numbered shortcuts."""
    all_sessions = sessions.list_sessions()
    if not all_sessions:
        say(":zap: No active headless sessions.")
        return

    lines = [f"*Headless sessions* ({len(all_sessions)})"]
    lines.append("_Use `!open #N` or `!kill #N` with the number below._\n")
    for i, s in enumerate(all_sessions[:15], 1):
        proj = s.get("project_name") or "?"

        last = s.get("last_active", 0)
        age = int(time.time() - last)
        if age < 60:
            age_str = f"{age}s ago"
        elif age < 3600:
            age_str = f"{age // 60}m ago"
        else:
            age_str = f"{age // 3600}h ago"

        lines.append(
            f"*#{i}*  `{proj}` — active {age_str}"
        )

    if len(all_sessions) > 15:
        lines.append(f"\n_…and {len(all_sessions) - 15} more_")

    say("\n".join(lines))


def _resolve_session_ref(ref: str) -> dict | None:
    """Resolve a session reference to a session entry.

    Accepts:
      - '#N' — numbered shortcut from !sessions list
      - raw thread_ts string
    Returns the session dict with 'thread_ts' key, or None.
    """
    all_sessions = sessions.list_sessions()
    if not all_sessions:
        return None

    # Numbered shortcut: #1, #2, etc.
    if ref.startswith("#"):
        try:
            idx = int(ref[1:]) - 1
            if 0 <= idx < len(all_sessions):
                return all_sessions[idx]
        except ValueError:
            pass
        return None

    # Raw thread_ts lookup
    entry = sessions.get_session_for_thread(ref)
    if entry:
        return {"thread_ts": ref, **entry}
    return None


def _handle_kill(args: dict, say, event: dict = None):
    """Remove a thread-session mapping. Accepts #N, thread_ts, or in-thread context."""
    ref = args.get("thread_ts", "").strip()

    # If no arg and used in a thread, target that thread
    if not ref and event:
        ref = event.get("thread_ts", "")

    if not ref:
        say(
            ":warning: Usage: `!kill #N` or `!kill <thread_ts>`\n"
            "Use `!sessions` to find the session number. Or reply `!kill` in a session thread."
        )
        return

    resolved = _resolve_session_ref(ref)
    if not resolved:
        say(f":warning: No session found for `{ref}`. Use `!sessions` to list.")
        return

    thread_ts = resolved["thread_ts"]
    proj = resolved.get("project_name") or "?"
    sid = resolved["session_id"]
    sessions.delete_session(thread_ts)
    say(
        f":wastebasket: Removed session *#{ref[1:] if ref.startswith('#') else ''}* "
        f"(`{proj}`)\n"
        f"The cortex session still exists — you can `--resume {sid}` manually."
    )
    log.info("Killed session mapping: thread=%s session=%s project=%s", thread_ts, sid, proj)


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
