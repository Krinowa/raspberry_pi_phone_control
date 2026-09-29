"""Compare one and two Paddle workers against the same saved real screen frame."""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation.screen.camera_ocr import _PaddleWorkerPool  # noqa: E402


def run(image, pool_size):
    pool = _PaddleWorkerPool(
        {
            "language": "en",
            "textline_orientation": False,
            "cpu_threads": int(os.environ.get("CAMERA_OCR_CPU_THREADS", "1")),
            "recognition_batch_size": int(
                os.environ.get("CAMERA_OCR_RECOGNITION_BATCH_SIZE", "1")
            ),
        },
        timeout=60.0,
        pool_size=pool_size,
    )
    try:
        pool.start()
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as executor:
            timings = [
                future.result()[1]
                for future in (
                    executor.submit(pool.request, image.copy()),
                    executor.submit(pool.request, image.copy()),
                )
            ]
        status = pool.status()
        try:
            import psutil
        except ImportError:
            status["worker_rss_mb"] = None
        else:
            try:
                status["worker_rss_mb"] = {
                    worker["worker_id"]: round(
                        psutil.Process(worker["pid"]).memory_info().rss / (1024 * 1024),
                        1,
                    )
                    for worker in status["workers"]
                    if worker["pid"] is not None
                }
            except (OSError, psutil.Error):
                status["worker_rss_mb"] = None
        return time.monotonic() - started, timings, status
    finally:
        pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Saved calibrated OCR input image")
    args = parser.parse_args()
    image = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if image is None:
        parser.error("Image could not be decoded.")
    for size in (1, 2):
        wall, requests, status = run(image, size)
        print(f"pool_size={size} wall={wall:.3f}s")
        for index, timing in enumerate(requests, 1):
            print(
                f"  request={index} worker={timing['worker_id']} "
                f"queue={timing['queue_wait_ms']:.1f}ms "
                f"inference={timing['inference_ms']:.1f}ms "
                f"total={timing['total_ocr_ms']:.1f}ms"
            )
        pids = [worker["pid"] for worker in status["workers"]]
        print(f"  worker_pids={pids}")
        if status["worker_rss_mb"] is not None:
            print(f"  observed_worker_rss_mb={status['worker_rss_mb']}")


if __name__ == "__main__":
    main()
