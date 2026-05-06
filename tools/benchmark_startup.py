from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gui.memory_reader import MemoryReader, is_process_running


def benchmark_startup(*, min_rank: float = 1.5) -> dict[str, Any]:
    """Measure read-only live hook and first entity scan time."""
    started = time.perf_counter()
    result: dict[str, Any] = {
        "process": "t-engine.exe",
        "process_running": is_process_running("t-engine.exe"),
        "attached": False,
        "level_id": None,
        "entity_count": 0,
        "attach_ms": None,
        "first_entity_read_ms": None,
        "total_ms": None,
    }
    if not result["process_running"]:
        result["total_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        return result

    reader = MemoryReader()
    try:
        attach_started = time.perf_counter()
        attached = reader.attach(verbose=False)
        result["attach_ms"] = round((time.perf_counter() - attach_started) * 1000.0, 3)
        result["attached"] = attached
        if not attached:
            result["total_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            return result

        result["level_id"] = reader.read_level_id()
        read_started = time.perf_counter()
        entities = reader.read_entities(min_rank=min_rank)
        result["first_entity_read_ms"] = round((time.perf_counter() - read_started) * 1000.0, 3)
        result["entity_count"] = len(entities)
        result["total_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        return result
    finally:
        reader.detach()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only ToME live startup benchmark.")
    parser.add_argument("--min-rank", type=float, default=1.5)
    parser.add_argument("--warn-over-ms", type=float, default=1000.0)
    parser.add_argument("--fail-over-ms", type=float, default=0.0)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    result = benchmark_startup(min_rank=args.min_rank)
    total_ms = result.get("total_ms")
    if isinstance(total_ms, (int, float)) and args.warn_over_ms > 0 and total_ms > args.warn_over_ms:
        result["warning"] = f"startup benchmark exceeded {args.warn_over_ms:.0f} ms"

    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.json_out is not None:
        args.json_out.write_text(text + "\n", encoding="utf-8")

    if (
        result.get("attached")
        and isinstance(total_ms, (int, float))
        and args.fail_over_ms > 0
        and total_ms > args.fail_over_ms
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
