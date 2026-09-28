"""Lightweight, cancellable stability checks over fresh external-camera samples."""

import time
import cv2
import numpy as np

from ..workflows.timing import record_timing


class ScreenStabilityError(RuntimeError):
    """Safe, user-facing observation failure (contains no device payloads)."""


def frames_are_stable(current, previous, anchor, threshold=3.0):
    """Tolerate sensor noise while rejecting adjacent or cumulative motion."""
    return (
        previous is not None
        and anchor is not None
        and np.mean(cv2.absdiff(current, previous)) <= threshold
        and np.mean(cv2.absdiff(current, anchor)) <= threshold
    )


def wait_until_stable(
    source,
    context,
    stable_ms,
    timeout_seconds,
    *,
    interval=0.15,
    threshold=3.0,
    stale_seconds=2.0,
    clock=time.monotonic,
):
    if source is None:
        raise ScreenStabilityError(
            "External camera is unavailable for screen stability."
        )

    wait_started = time.monotonic()
    deadline = clock() + timeout_seconds
    previous = anchor = None
    last_id = last_timestamp = stable_since = None
    session = source.session()
    try:
        while True:
            context.check_cancelled()
            now = clock()
            if now >= deadline:
                raise ScreenStabilityError(
                    f"Screen did not settle within {timeout_seconds:g} seconds."
                )
            record_timing("polling_wait", 0.0, count=1)
            frame, frame_id, captured_at = session.capture()
            now = clock()
            if now - captured_at >= stale_seconds:
                raise ScreenStabilityError(
                    "External camera feed is stale; no fresh frames arrived."
                )

            if last_id is None or frame_id != last_id:
                if last_timestamp is not None and captured_at <= last_timestamp:
                    raise ScreenStabilityError(
                        "External camera timestamps stopped advancing."
                    )

                # Ignore gaps in observation: they cannot prove continuous stability.
                gap = (
                    last_timestamp is not None
                    and captured_at - last_timestamp > interval * 3
                )

                # Mean absolute grayscale difference on a tiny, blurred display crop.
                # 3/255 tolerates camera noise. Compare both adjacent samples and the
                # window's anchor so slow cumulative transitions don't look stable.
                stable = not gap and frames_are_stable(
                    frame, previous, anchor, threshold
                )
                if not stable:
                    stable_since = max(now, captured_at)
                    anchor = frame
                previous, last_id, last_timestamp = frame, frame_id, captured_at
                if (
                    stable
                    and captured_at - stable_since >= stable_ms / 1000
                    and now < deadline
                ):
                    context.check_cancelled()
                    return

            # Event-based sleep preserves instant cancellation; also poll takeover.
            sleep_started = time.monotonic()
            context.cancel_event.wait(min(interval, max(0, deadline - clock())))
            record_timing("polling_wait", time.monotonic() - sleep_started)
    finally:
        record_timing("wait_for_condition", time.monotonic() - wait_started)
