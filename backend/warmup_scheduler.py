"""Background warm-up scheduler.

Runs warm-up sends in a daemon thread once per day. It also snaps the warm-up
status so the sender profile reflects the current ramping phase immediately on
startup when warm-up is enabled.
"""

import threading
import time
from datetime import datetime

from backend.config import load_project_env  # noqa: F401  (ensures .env loaded)
from backend.warmup_engine import get_warmup_state, run_warmup_once, update_status_snapshot

_CHECK_INTERVAL_SECONDS = 3600  # 1 hour
_LAST_RUN_MARKER_KEY = "warmup_last_run_date"
_MARKERS = {}


def _should_run_today() -> bool:
    today = datetime.now().date().isoformat()
    return _MARKERS.get(_LAST_RUN_MARKER_KEY) != today


def _run_daily():
    if not _should_run_today():
        return
    try:
        state = get_warmup_state()
        if not state.get("enabled"):
            return
        result = run_warmup_once()
        print(f"[warmup] Daily warm-up run: {result}")
    except Exception as exc:
        print(f"[warmup] Daily warm-up run failed: {exc}")
    finally:
        _MARKERS[_LAST_RUN_MARKER_KEY] = datetime.now().date().isoformat()


def _worker_loop(interval_seconds: int):
    while True:
        _run_daily()
        time.sleep(interval_seconds)


def start_warmup_scheduler(interval_seconds: int = _CHECK_INTERVAL_SECONDS) -> threading.Thread:
    try:
        update_status_snapshot()
    except Exception:
        pass
    thread = threading.Thread(target=_worker_loop, args=(interval_seconds,), daemon=True, name="warmup-scheduler")
    thread.start()
    return thread
