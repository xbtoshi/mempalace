"""
sync_state.py — Tracks which drawers have been synced to Cloudflare.

State file: ~/.mempalace/sync_state.json
  {
    "<drawer_id>": {
      "hash": "<sha256 of content>",
      "synced_at": 1234567890.0,   # unix timestamp of last successful sync
      "direction": "push" | "pull"
    },
    ...
  }

Conflict log: ~/.mempalace/sync_conflicts.jsonl
  One JSON object per line, each a conflict event.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, Optional


def _state_path() -> Path:
    return Path(os.environ.get("MEMPALACE_SYNC_STATE", str(Path.home() / ".mempalace" / "sync_state.json")))


def _conflict_log_path() -> Path:
    return Path.home() / ".mempalace" / "sync_conflicts.jsonl"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def load_state() -> Dict[str, dict]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: Dict[str, dict]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)


def mark_synced(state: Dict[str, dict], drawer_id: str, content: str, direction: str) -> None:
    state[drawer_id] = {
        "hash": content_hash(content),
        "synced_at": time.time(),
        "direction": direction,
    }


def mark_deleted(state: Dict[str, dict], drawer_id: str) -> None:
    state.pop(drawer_id, None)


def log_conflict(drawer_id: str, reason: str, local: Optional[dict], remote: Optional[dict]) -> None:
    path = _conflict_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "id": drawer_id,
        "reason": reason,
        "local": local,
        "remote": remote,
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
