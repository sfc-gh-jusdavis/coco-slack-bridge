"""Slack DM command parser for the Cortex Code Slack Bridge.

Commands are prefixed with `!` (e.g. `!task add high Title :: Desc`) to avoid
conflicts with Slack's global slash command interception.

All parsing is string-only and zero-dep to keep the sidecar lean.
"""

from __future__ import annotations

from typing import Any

VALID_PRIORITIES = {"urgent", "high", "medium", "low", "none"}
VALID_STATUSES = {"backlog", "in_progress", "need_approval", "review", "done"}


COMMAND_PREFIX = "!"


def parse(text: str) -> dict[str, Any] | None:
    """Parse a command string. Returns None if `text` is not a command.

    Commands start with `!` to avoid Slack's slash command interception.

    Returns a dict like:
        {"kind": "inline", "op": "projects"}                        # bot answers directly
        {"kind": "inline", "op": "use", "args": {"name": "foo"}}
        {"kind": "inline", "op": "help"}
        {"kind": "command", "command": "snowboard.create_task", "args": {...}}
        {"kind": "error", "message": "..."}
    """
    if not text or not text.strip().startswith(COMMAND_PREFIX):
        return None
    raw = text.strip()
    # Normalize smart quotes so phones don't break us
    raw = raw.replace("\u2018", "'").replace("\u2019", "'")
    raw = raw.replace("\u201c", '"').replace("\u201d", '"')
    head, _, rest = raw.partition(" ")
    head = head.lower().lstrip(COMMAND_PREFIX)
    rest = rest.strip()

    if head in {"help", "?"}:
        return {"kind": "inline", "op": "help"}

    if head == "projects":
        return {"kind": "inline", "op": "projects"}

    if head in {"status", "session"}:
        return {"kind": "inline", "op": "status"}

    if head == "use":
        if not rest:
            return {"kind": "error", "message": "Usage: /use <project-name>"}
        return {"kind": "inline", "op": "use", "args": {"name": rest.split()[0]}}

    if head == "new":
        return _parse_new(rest)

    if head == "task":
        return _parse_task(rest)

    return {"kind": "error", "message": f"Unknown command `!{head}`. Try `!help`."}


def _parse_new(rest: str) -> dict[str, Any]:
    """`/new <project-name> [initial prompt]` — request a new CoCo CLI session."""
    if not rest:
        return {"kind": "error", "message": "Usage: /new <project-name> [initial prompt]"}
    parts = rest.split(None, 1)
    name = parts[0]
    prompt = parts[1] if len(parts) > 1 else ""
    return {
        "kind": "command",
        "command": "coco.new_session",
        "args": {"project": name, "prompt": prompt},
    }


def _parse_task(rest: str) -> dict[str, Any]:
    if not rest:
        return {"kind": "error", "message": "Usage: /task add|list|status ..."}
    op, _, tail = rest.partition(" ")
    op = op.lower()
    tail = tail.strip()

    if op == "add":
        return _parse_task_add(tail)
    if op == "list":
        return _parse_task_list(tail)
    if op == "status":
        return _parse_task_status(tail)

    return {"kind": "error", "message": f"Unknown task op `{op}`. Use add|list|status."}


def _parse_task_add(tail: str) -> dict[str, Any]:
    """`/task add [priority] <title> :: <description>`."""
    if not tail:
        return {
            "kind": "error",
            "message": "Usage: /task add [priority] <title> :: <description>",
        }

    priority = "medium"
    # Optional leading priority
    first, _, after = tail.partition(" ")
    if first.lower() in VALID_PRIORITIES:
        priority = first.lower()
        tail = after.strip()

    if "::" in tail:
        title, _, desc = tail.partition("::")
        title = title.strip()
        desc = desc.strip()
    else:
        title = tail.strip()
        desc = title

    if not title:
        return {"kind": "error", "message": "Task title cannot be empty."}

    return {
        "kind": "command",
        "command": "snowboard.create_task",
        "args": {
            "title": title,
            "description": desc or title,
            "priority": priority,
        },
    }


def _parse_task_list(tail: str) -> dict[str, Any]:
    status = None
    if tail:
        candidate = tail.split()[0].lower()
        if candidate in VALID_STATUSES:
            status = candidate
        else:
            return {
                "kind": "error",
                "message": f"Unknown status `{candidate}`. Valid: {', '.join(sorted(VALID_STATUSES))}",
            }
    return {
        "kind": "command",
        "command": "snowboard.list_tasks",
        "args": {"status": status},
    }


def _parse_task_status(tail: str) -> dict[str, Any]:
    parts = tail.split()
    if len(parts) < 2:
        return {
            "kind": "error",
            "message": "Usage: /task status <id> <backlog|in_progress|need_approval|review|done>",
        }
    task_id_raw, new_status = parts[0], parts[1].lower()
    try:
        task_id = int(task_id_raw)
    except ValueError:
        return {"kind": "error", "message": f"Task id must be numeric, got `{task_id_raw}`"}
    if new_status not in VALID_STATUSES:
        return {
            "kind": "error",
            "message": f"Unknown status `{new_status}`. Valid: {', '.join(sorted(VALID_STATUSES))}",
        }
    return {
        "kind": "command",
        "command": "snowboard.update_task",
        "args": {"task_id": task_id, "status": new_status},
    }


HELP_TEXT = (
    "*CoCo Bridge commands* (prefix: `!`)\n"
    "`!help` — this help\n"
    "`!status` — active project, session, inbox age, poll interval\n"
    "`!projects` — list all registered projects and session activity\n"
    "`!use <name>` — switch the active project (routes free-text DMs)\n"
    "`!new <name> [prompt]` — ask the agent to start a new CoCo CLI session\n"
    "`!task add [priority] <title> :: <description>` — create SnowBoard task "
    "(priority: urgent|high|medium|low|none, default medium)\n"
    "`!task list [status]` — list tasks (status: backlog|in_progress|need_approval|review|done)\n"
    "`!task status <id> <status>` — update a task status\n"
    "Free text (no leading `!`) is forwarded to the active CoCo session as a reply."
)
