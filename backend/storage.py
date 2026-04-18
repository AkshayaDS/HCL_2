"""
JSON-file storage helpers.

All persistence uses the files under /data with a simple file-lock so
concurrent Flask requests don't corrupt each other.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _path(name: str) -> Path:
    return DATA / name


def load(name: str) -> Any:
    with _lock:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)


def save(name: str, data: Any) -> None:
    """Atomic write: write to temp file then rename."""
    with _lock:
        target = _path(name)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


def append_record(name: str, record: dict[str, Any]) -> dict[str, Any]:
    data = load(name)
    if not isinstance(data, list):
        raise ValueError(f"{name} is not a list")
    data.append(record)
    save(name, data)
    return record


def update_record(name: str, key: str, value: Any, patch: dict[str, Any]) -> dict[str, Any] | None:
    data = load(name)
    if not isinstance(data, list):
        raise ValueError(f"{name} is not a list")
    for record in data:
        if record.get(key) == value:
            record.update(patch)
            save(name, data)
            return record
    return None


def find_record(name: str, key: str, value: Any) -> dict[str, Any] | None:
    data = load(name)
    if not isinstance(data, list):
        return data.get(value) if isinstance(data, dict) else None
    for record in data:
        if record.get(key) == value:
            return record
    return None
