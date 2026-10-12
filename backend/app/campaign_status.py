from datetime import datetime, timezone
import threading


_lock = threading.Lock()


def _default_state():
    return {
        "status": "idle",
        "message": "No campaign has been started yet.",
        "total": 0,
        "processed": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "remaining": 0,
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": None,
    }


_state = _default_state()


def _now():
    return datetime.now(timezone.utc)


def _serialize_datetime(value):
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _snapshot():
    return {
        **_state,
        "started_at": _serialize_datetime(_state["started_at"]),
        "finished_at": _serialize_datetime(_state["finished_at"]),
    }


def campaign_is_running():
    with _lock:
        return _state["status"] == "running"


def get_campaign_status_snapshot():
    with _lock:
        return _snapshot()


def start_campaign_tracking(total):
    global _state

    with _lock:
        if _state["status"] == "running":
            return False, _snapshot()

        _state = _default_state()
        _state.update(
            {
                "status": "running",
                "message": f"Campaign running: 0/{total} processed.",
                "total": total,
                "remaining": total,
                "started_at": _now(),
            }
        )
        return True, _snapshot()


def record_campaign_progress(sent=0, skipped=0, failed=0):
    with _lock:
        if _state["status"] != "running":
            return _snapshot()

        sent = max(0, int(sent))
        skipped = max(0, int(skipped))
        failed = max(0, int(failed))
        processed_delta = sent + skipped + failed

        _state["sent"] += sent
        _state["skipped"] += skipped
        _state["failed"] += failed
        _state["processed"] += processed_delta
        _state["remaining"] = max(0, _state["total"] - _state["processed"])
        _state["message"] = (
            f"Campaign running: {_state['processed']}/{_state['total']} processed."
        )
        return _snapshot()


def finish_campaign(elapsed_seconds=None, message=None):
    with _lock:
        if _state["status"] != "running":
            return _snapshot()

        _state["status"] = "finished"
        _state["finished_at"] = _now()
        _state["remaining"] = max(0, _state["total"] - _state["processed"])
        if elapsed_seconds is not None:
            _state["elapsed_seconds"] = round(float(elapsed_seconds), 2)
        _state["message"] = message or (
            f"Campaign finished: sent={_state['sent']}, skipped={_state['skipped']}, "
            f"failed={_state['failed']}."
        )
        return _snapshot()


def fail_campaign(error_message):
    with _lock:
        _state["status"] = "failed"
        _state["finished_at"] = _now()
        _state["message"] = f"Campaign failed: {error_message}"
        return _snapshot()
