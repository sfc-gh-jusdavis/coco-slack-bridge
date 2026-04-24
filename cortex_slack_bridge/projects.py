"""Project registry for the Cortex Code Slack Bridge.

A "project" maps a short friendly name to an absolute working directory
(where CoCo sessions are launched). The registry is persisted to
~/.cortex-slack-bridge/projects.json and used to:

- Route free-text Slack DMs to the currently active project's session
- Let the user switch the active project from Slack (`/use <name>`)
- Tag SnowBoard task commands with project context

Schema:
    {
      "active_project": "slack_v1",
      "projects": {
        "slack_v1": {
          "path": "/Users/jusdavis/projects/small_projects/slack_v1",
          "sessions": {"abc123": {"updated_at": 1234567890.0}},
          "updated_at": 1234567890.0
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cortex_slack_bridge.config import BRIDGE_DIR, ensure_dirs

PROJECTS_FILE = BRIDGE_DIR / "projects.json"


def _read() -> dict:
    if not PROJECTS_FILE.exists():
        return {"active_project": None, "projects": {}}
    try:
        with open(PROJECTS_FILE) as f:
            data = json.load(f)
        data.setdefault("active_project", None)
        data.setdefault("projects", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"active_project": None, "projects": {}}


def _write(data: dict) -> None:
    ensure_dirs()
    tmp = PROJECTS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(PROJECTS_FILE)


def _default_name_for_path(path: str) -> str:
    return Path(path).resolve().name or "root"


def register_project(
    path: str,
    name: str | None = None,
    session_id: str | None = None,
    make_active: bool = True,
) -> str:
    """Register or refresh a project entry. Returns the project name."""
    path = str(Path(path).expanduser().resolve())
    data = _read()
    # Reuse name if path already registered
    existing_name = None
    for pname, pinfo in data["projects"].items():
        if pinfo.get("path") == path:
            existing_name = pname
            break
    name = name or existing_name or _default_name_for_path(path)
    entry = data["projects"].get(name, {"path": path, "sessions": {}})
    entry["path"] = path
    entry["updated_at"] = time.time()
    entry.setdefault("sessions", {})
    if session_id:
        entry["sessions"][session_id] = {"updated_at": time.time()}
    data["projects"][name] = entry
    if make_active or data.get("active_project") is None:
        data["active_project"] = name
    _write(data)
    return name


def list_projects() -> list[dict]:
    data = _read()
    active = data.get("active_project")
    out = []
    for name, info in sorted(data["projects"].items()):
        out.append({
            "name": name,
            "path": info.get("path", ""),
            "sessions": list(info.get("sessions", {}).keys()),
            "active": name == active,
            "updated_at": info.get("updated_at", 0),
        })
    return out


def get_active_project() -> dict | None:
    data = _read()
    name = data.get("active_project")
    if not name:
        return None
    info = data["projects"].get(name)
    if not info:
        return None
    return {"name": name, **info}


def set_active_project(name: str) -> bool:
    data = _read()
    if name not in data["projects"]:
        return False
    data["active_project"] = name
    _write(data)
    return True


def project_for_session(session_id: str) -> dict | None:
    """Return the project (if any) that knows about this session_id."""
    data = _read()
    for name, info in data["projects"].items():
        if session_id in info.get("sessions", {}):
            return {"name": name, **info}
    return None


def active_session_for_project(name: str | None = None) -> str | None:
    """Return the most-recently-updated session_id for a project."""
    data = _read()
    pname = name or data.get("active_project")
    if not pname:
        return None
    info = data["projects"].get(pname, {})
    sessions = info.get("sessions", {})
    if not sessions:
        return None
    return max(sessions.items(), key=lambda kv: kv[1].get("updated_at", 0))[0]


def remove_project(name: str) -> bool:
    data = _read()
    if name not in data["projects"]:
        return False
    del data["projects"][name]
    if data.get("active_project") == name:
        data["active_project"] = next(iter(data["projects"].keys()), None)
    _write(data)
    return True


def auto_register_cwd(session_id: str | None = None) -> str:
    """Register the current working directory as a project and mark it active."""
    return register_project(os.getcwd(), session_id=session_id, make_active=True)
