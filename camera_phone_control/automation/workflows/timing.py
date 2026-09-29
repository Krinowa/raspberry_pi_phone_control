"""Workflow-local performance measurements with no payload/value logging."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import threading
import time

_active_timing = ContextVar("workflow_timing", default=None)


def active_timing():
    return _active_timing.get()


def record_timing(category, seconds, *, count=0, event=None):
    timing = active_timing()
    if timing is not None:
        timing.record(category, seconds, count=count, event=event)


@contextmanager
def measure_timing(category, *, count=0, event=None):
    started = time.monotonic()
    try:
        yield
    finally:
        record_timing(
            category,
            time.monotonic() - started,
            count=count,
            event=event,
        )


class WorkflowTiming:
    """Collect step and aggregate wall-clock timing for one workflow run."""

    _ROI_COUNTS = (
        "roi_attempt_count",
        "roi_hit_count",
        "roi_miss_count",
        "full_fallback_count",
    )

    _DURATION_FIELDS = (
        "roi_ocr_seconds",
        "full_ocr_seconds",
        "hid_api_seconds",
        "fixed_sleep_seconds",
        "polling_wait_seconds",
        "wait_for_condition_seconds",
        "manual_wait_seconds",
        "camera_frame_seconds",
        "ocr_preprocessing_seconds",
        "ocr_queue_seconds",
        "ocr_ipc_seconds",
        "ocr_inference_seconds",
        "ocr_postprocessing_seconds",
        "ocr_matching_seconds",
    )

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.started_at = None
        self.finished_at = None
        self.steps = []
        self._current = None
        self._token = None
        self._seen_ocr_frames = set()

    def start(self):
        with self.lock:
            self.started_at = self.clock()
            self._token = _active_timing.set(self)

    def finish(self):
        with self.lock:
            if self._current is not None:
                self._finish_step_locked("interrupted")
            self.finished_at = self.clock()
            if self._token is not None:
                _active_timing.reset(self._token)
                self._token = None

    def begin_step(self, index, name):
        with self.lock:
            if self._current is not None:
                self._finish_step_locked("interrupted")
            step = {
                "index": index,
                "name": name,
                "status": "running",
                "total_seconds": 0.0,
                **{field: 0.0 for field in self._DURATION_FIELDS},
                "poll_count": 0,
                "retry_count": 0,
                "camera_frame_count": 0,
                "ocr_call_count": 0,
                "ocr_inference_count": 0,
                "ocr_cache_hit_count": 0,
                "ocr_skipped_count": 0,
                "ocr_forced_count": 0,
                "ocr_change_count": 0,
                "ocr_initial_count": 0,
                "repeated_ocr_frame_count": 0,
                "ocr_calls": [],
                **{field: 0 for field in self._ROI_COUNTS},
                "_started": self.clock(),
            }
            self.steps.append(step)
            self._current = step

    def end_step(self, status="completed"):
        with self.lock:
            if self._current is not None:
                self._finish_step_locked(status)

    def _finish_step_locked(self, status):
        self._current["total_seconds"] = self.clock() - self._current.pop("_started")
        self._current["status"] = status
        self._current = None

    def record(self, category, seconds, *, count=0, event=None):
        if seconds is None:
            return
        with self.lock:
            step = self._current
            if step is None:
                return
            field = category if category.endswith("_seconds") else category + "_seconds"
            if field in self._DURATION_FIELDS:
                step[field] += max(0.0, float(seconds))
            if category == "polling_wait":
                step["poll_count"] += count
            elif category == "camera_frame":
                step["camera_frame_count"] += count
            elif category == "retry":
                step["retry_count"] += count
            elif category == "ocr_call":
                metadata = dict(event or {})
                for field in self._ROI_COUNTS:
                    step[field] += metadata.get(field, 0)
                scope = "roi" if metadata.get("scope") == "roi" else "full"
                step[scope + "_ocr_seconds"] += sum(
                    metadata.get(key, 0.0)
                    for key in (
                        "preprocessing_seconds",
                        "queue_wait_seconds",
                        "ipc_send_seconds",
                        "ipc_wait_seconds",
                        "postprocessing_seconds",
                        "worker_start_seconds",
                    )
                )
                frame_id = metadata.get("frame_id")
                repeated = frame_id is not None and frame_id in self._seen_ocr_frames
                if frame_id is not None:
                    self._seen_ocr_frames.add(frame_id)
                metadata["repeated_frame"] = repeated
                step["ocr_call_count"] += 1
                step["ocr_inference_count"] += int(not bool(metadata.get("cache_hit")))
                step["ocr_cache_hit_count"] += int(bool(metadata.get("cache_hit")))
                step["repeated_ocr_frame_count"] += int(repeated)
                step["ocr_calls"].append(metadata)
            elif category == "ocr_skip":
                step["ocr_skipped_count"] += count
            elif category == "ocr_trigger":
                reason = (event or {}).get("reason")
                if reason == "forced":
                    step["ocr_forced_count"] += count
                elif reason == "changed_and_settled":
                    step["ocr_change_count"] += count
                elif reason == "no_baseline":
                    step["ocr_initial_count"] += count

    @staticmethod
    def _public_step(step):
        result = {key: value for key, value in step.items() if not key.startswith("_")}
        result["ocr_wall_seconds"] = (
            result["ocr_preprocessing_seconds"]
            + result["ocr_queue_seconds"]
            + result["ocr_ipc_seconds"]
            + result["ocr_postprocessing_seconds"]
        )
        calls = result["ocr_calls"]
        result["roi"] = next(
            (call["roi"] for call in calls if call.get("roi") is not None), None
        )
        result["scopes"] = sorted({call.get("scope", "full") for call in calls})
        for field in ("inference_ms", "queue_wait_ms", "region_count"):
            result[field] = sum(call.get(field) or 0 for call in calls)
        result["visual_app_wait_seconds"] = result["polling_wait_seconds"]
        accounted = (
            result["hid_api_seconds"]
            + result["fixed_sleep_seconds"]
            + result["polling_wait_seconds"]
            + result["manual_wait_seconds"]
            + result["camera_frame_seconds"]
            + result["ocr_wall_seconds"]
            + result["ocr_matching_seconds"]
        )
        result["other_seconds"] = max(0.0, result["total_seconds"] - accounted)
        return result

    def snapshot(self):
        with self.lock:
            now = self.finished_at or self.clock()
            started = self.started_at
            steps = [self._public_step(deepcopy(step)) for step in self.steps]
            if self._current is not None and steps:
                steps[-1]["total_seconds"] = now - self._current["_started"]
                steps[-1] = self._public_step(
                    {**self._current, "total_seconds": steps[-1]["total_seconds"]}
                )
            totals = {
                field: sum(step[field] for step in steps)
                for field in self._DURATION_FIELDS
            }
            total = 0.0 if started is None else max(0.0, now - started)
            # OCR IPC wait includes child inference, so ocr_wall uses IPC rather
            # than adding inference a second time. Condition waits are outer
            # wall-clock spans and likewise remain diagnostic, not additive.
            ocr_wall = (
                totals["ocr_preprocessing_seconds"]
                + totals["ocr_queue_seconds"]
                + totals["ocr_ipc_seconds"]
                + totals["ocr_postprocessing_seconds"]
            )
            accounted = (
                totals["hid_api_seconds"]
                + totals["fixed_sleep_seconds"]
                + totals["polling_wait_seconds"]
                + totals["manual_wait_seconds"]
                + totals["camera_frame_seconds"]
                + ocr_wall
                + totals["ocr_matching_seconds"]
            )
            summary = {
                **totals,
                **{
                    field: sum(step[field] for step in steps)
                    for field in self._ROI_COUNTS
                },
                "ocr_wall_seconds": ocr_wall,
                "visual_app_wait_seconds": totals["polling_wait_seconds"],
                "other_seconds": max(0.0, total - accounted),
                "total_seconds": total,
                "poll_count": sum(step["poll_count"] for step in steps),
                "retry_count": sum(step["retry_count"] for step in steps),
                "camera_frame_count": sum(step["camera_frame_count"] for step in steps),
                "ocr_call_count": sum(step["ocr_call_count"] for step in steps),
                "ocr_inference_count": sum(
                    step["ocr_inference_count"] for step in steps
                ),
                "ocr_cache_hit_count": sum(
                    step["ocr_cache_hit_count"] for step in steps
                ),
                "ocr_skipped_count": sum(step["ocr_skipped_count"] for step in steps),
                "ocr_forced_count": sum(step["ocr_forced_count"] for step in steps),
                "ocr_change_count": sum(step["ocr_change_count"] for step in steps),
                "ocr_initial_count": sum(step["ocr_initial_count"] for step in steps),
                "repeated_ocr_frame_count": sum(
                    step["repeated_ocr_frame_count"] for step in steps
                ),
            }
            return {"steps": steps, "summary": summary}
