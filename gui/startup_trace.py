from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_DISABLED_VALUES = {"0", "false", "False", "no", "No"}
_ENABLED = os.environ.get("TOME_STARTUP_TRACE", "1") not in _DISABLED_VALUES
_LOCK = threading.Lock()
_STARTED_AT: float | None = None
_TRACE_PATH: Path | None = None
_MARKS: list[dict[str, Any]] = []


def configure_startup_trace(started_at: float | None, trace_path: Path) -> None:
    if not _ENABLED:
        return
    global _STARTED_AT, _TRACE_PATH
    with _LOCK:
        _STARTED_AT = started_at if started_at is not None else time.perf_counter()
        _TRACE_PATH = trace_path
        _MARKS.clear()
    mark_startup_phase("trace_configured")


def mark_startup_phase(name: str, **data: object) -> None:
    if not _ENABLED:
        return
    now = time.perf_counter()
    with _LOCK:
        started_at = _STARTED_AT if _STARTED_AT is not None else now
        mark: dict[str, Any] = {
            "name": name,
            "elapsed_s": round(now - started_at, 6),
        }
        if data:
            mark["data"] = _json_safe(data)
        _MARKS.append(mark)


def write_startup_trace(final_name: str | None = None, **data: object) -> None:
    if not _ENABLED:
        return
    if final_name is not None:
        mark_startup_phase(final_name, **data)
    now = time.perf_counter()
    with _LOCK:
        if _TRACE_PATH is None:
            return
        started_at = _STARTED_AT if _STARTED_AT is not None else now
        payload = {
            "total_s": round(now - started_at, 6),
            "marks": list(_MARKS),
        }
        trace_path = _TRACE_PATH
    try:
        trace_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)
