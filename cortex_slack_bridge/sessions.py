"""Headless cortex session manager for the Slack bridge.

Runs `cortex -p` subprocesses to handle Slack messages without polling.
Supports multi-turn conversations via `--resume <session_id>`.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from cortex_slack_bridge.config import (
    BRIDGE_DIR,
    ensure_dirs,
    get_headless_max_turns,
    get_headless_timeout,
)

log = logging.getLogger("cortex-slack-bridge")

# ---------------------------------------------------------------------------
# Thread-session registry (Slack thread_ts → cortex session_id + project)
# ---------------------------------------------------------------------------

HEADLESS_SESSIONS_FILE = BRIDGE_DIR / "headless_sessions.json"


def _read_sessions() -> dict:
    if not HEADLESS_SESSIONS_FILE.exists():
        return {}
    try:
        with open(HEADLESS_SESSIONS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_sessions(data: dict):
    ensure_dirs()
    tmp = HEADLESS_SESSIONS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(HEADLESS_SESSIONS_FILE)


def get_session_for_thread(thread_ts: str) -> dict | None:
    """Look up the headless session for a Slack thread.

    Returns {"session_id": ..., "project_path": ..., "created_at": ..., "last_active": ...}
    or None.
    """
    data = _read_sessions()
    entry = data.get(thread_ts)
    if entry:
        entry["last_active"] = time.time()
        data[thread_ts] = entry
        _write_sessions(data)
    return entry


def register_session(
    thread_ts: str,
    session_id: str,
    project_path: str,
    project_name: str | None = None,
) -> None:
    """Map a Slack thread to a headless cortex session."""
    data = _read_sessions()
    data[thread_ts] = {
        "session_id": session_id,
        "project_path": project_path,
        "project_name": project_name,
        "created_at": time.time(),
        "last_active": time.time(),
    }
    _write_sessions(data)


def list_sessions() -> list[dict]:
    """Return all headless session mappings."""
    data = _read_sessions()
    out = []
    for ts, entry in sorted(
        data.items(), key=lambda kv: kv[1].get("last_active", 0), reverse=True
    ):
        out.append({"thread_ts": ts, **entry})
    return out


def delete_session(thread_ts: str) -> bool:
    """Remove a thread-session mapping. Returns True if it existed."""
    data = _read_sessions()
    if thread_ts in data:
        del data[thread_ts]
        _write_sessions(data)
        return True
    return False


# ---------------------------------------------------------------------------
# Cortex process runner
# ---------------------------------------------------------------------------

def _find_cortex() -> str:
    """Locate the cortex binary."""
    # Check common locations
    for candidate in [
        Path.home() / ".local" / "bin" / "cortex",
        Path("/usr/local/bin/cortex"),
    ]:
        if candidate.exists():
            return str(candidate)
    # Fall back to PATH
    found = shutil.which("cortex")
    if found:
        return found
    raise FileNotFoundError("cortex binary not found")


def run_cortex(
    message: str,
    project_path: str,
    session_id: str | None = None,
    max_turns: int | None = None,
    timeout: int | None = None,
) -> dict:
    """Run a headless cortex invocation and return the response.

    Args:
        message: The user's message to send to cortex.
        project_path: Working directory for the cortex session.
        session_id: If provided, resumes this session (multi-turn).
        max_turns: Max agentic turns (default from config).
        timeout: Subprocess timeout in seconds (default from config).

    Returns:
        {
            "session_id": str,       # cortex session ID (for future --resume)
            "response": str,         # assistant's text response
            "error": str | None,     # error message if something went wrong
            "duration_ms": int,      # how long the invocation took
        }
    """
    max_turns = max_turns or get_headless_max_turns()
    timeout = timeout or get_headless_timeout()
    cortex_bin = _find_cortex()

    cmd = [
        cortex_bin,
        "-p", message,
        "-w", project_path,
        "--max-turns", str(max_turns),
        "--output-format", "stream-json",
        "--bypass",
    ]

    if session_id:
        cmd.extend(["--resume", session_id])

    log.info(
        "Running cortex: session=%s project=%s message=%s",
        session_id or "new", project_path, message[:80],
    )

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_path,
        )
    except subprocess.TimeoutExpired:
        duration = int((time.time() - start) * 1000)
        log.error("Cortex timed out after %ds", timeout)
        return {
            "session_id": session_id,
            "response": None,
            "error": f"Cortex timed out after {timeout}s",
            "duration_ms": duration,
        }
    except Exception as exc:
        duration = int((time.time() - start) * 1000)
        log.error("Cortex subprocess error: %s", exc)
        return {
            "session_id": session_id,
            "response": None,
            "error": str(exc),
            "duration_ms": duration,
        }

    duration = int((time.time() - start) * 1000)

    # Parse stream-json output (one JSON object per line)
    resolved_session_id = session_id
    response_text = None
    errors = []

    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type")

        if msg_type == "system" and obj.get("subtype") == "init":
            resolved_session_id = obj.get("session_id", resolved_session_id)

        elif msg_type == "assistant":
            # Extract text content from the assistant message
            content_blocks = (
                obj.get("message", {}).get("content", [])
            )
            texts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    texts.append(block["text"])
            if texts:
                response_text = "\n".join(texts)

        elif msg_type == "result":
            result_errors = obj.get("errors", [])
            # Filter out the "Max tool call iterations reached" non-error
            real_errors = [
                e for e in result_errors
                if e != "Max tool call iterations reached"
            ]
            errors.extend(real_errors)

    # If stderr has useful info and we got no response
    if not response_text and result.stderr:
        log.warning("Cortex stderr: %s", result.stderr[:500])

    error_msg = "; ".join(errors) if errors else None

    log.info(
        "Cortex done: session=%s duration=%dms response_len=%d error=%s",
        resolved_session_id,
        duration,
        len(response_text or ""),
        error_msg,
    )

    return {
        "session_id": resolved_session_id,
        "response": response_text,
        "error": error_msg,
        "duration_ms": duration,
    }
