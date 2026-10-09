"""Measure Paddle inference while production camera/preview loops are active."""

import argparse
import json
from pathlib import Path
import statistics
import threading
import time

import cv2

from camera import Camera
from .camera_ocr import _PaddleWorkerClient
from .benchmark_paddle import percentile


def poll_frames(cameras, stop, counts):
    while not stop.is_set():
        for camera in cameras:
            if camera.frame() is not None:
                counts[id(camera)] = counts.get(id(camera), 0) + 1
        stop.wait(0.05)


def benchmark(worker, image, iterations):
    wall, cpu, native_threads = [], [], []
    for _ in range(iterations):
        _, timing = worker.request(image)
        wall.append(timing["inference"])
        cpu.append(timing.get("cpu_user", 0) + timing.get("cpu_system", 0))
        native_threads.append(timing.get("native_threads"))

    return {
        "median_inference_seconds": statistics.median(wall),
        "p95_inference_seconds": percentile(wall, 0.95),
        "median_child_cpu_seconds": statistics.median(cpu),
        "native_thread_counts": sorted(
            {value for value in native_threads if value is not None}
        ),
    }


def run_scenario(worker, image, sources, preview_polling, iterations):
    cameras = [Camera(source, 1280, 720, 15, 90) for source in sources]
    stop = threading.Event()
    counts = {}
    poller = None

    try:
        for camera in cameras:
            camera.start()
            if camera.wait_until_opened(4) is not True:
                raise RuntimeError(camera.status()["error"])

        time.sleep(1)
        sequences_before = [camera.frame()[1] for camera in cameras]

        if preview_polling:
            poller = threading.Thread(
                target=poll_frames, args=(cameras, stop, counts), daemon=True
            )
            poller.start()

        measurement_started = time.monotonic()
        result = benchmark(worker, image, iterations)
        elapsed = time.monotonic() - measurement_started
        sequences_after = [camera.frame()[1] for camera in cameras]
        result.update(
            camera_count=len(cameras),
            preview_polling=preview_polling,
            camera_capture_fps=[
                (after - before) / elapsed
                for before, after in zip(sequences_before, sequences_after)
            ],
            preview_poll_count=sum(counts.values()),
        )

        return result
    finally:
        stop.set()
        if poller is not None:
            poller.join(1)
        for camera in cameras:
            camera.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--sources", default="1,0")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    image = cv2.imread(str(args.image), cv2.IMREAD_COLOR)

    if image is None:
        parser.error("image could not be read")

    sources = [int(value) for value in args.sources.split(",") if value.strip()]

    if len(sources) < 2:
        parser.error("--sources requires at least two indexes")

    worker = _PaddleWorkerClient(
        {
            "language": "en",
            "textline_orientation": False,
            "cpu_threads": args.cpu_threads,
            "recognition_batch_size": None,
        },
        30,
    )

    try:
        # Initialize both models and native pools before comparing scenarios.
        worker.request(image)
        report = {
            "capture_disabled": benchmark(worker, image, args.iterations),
            "one_camera_preview_disabled": run_scenario(
                worker, image, sources[:1], False, args.iterations
            ),
            "one_camera_preview_active": run_scenario(
                worker, image, sources[:1], True, args.iterations
            ),
            "two_camera_previews_active": run_scenario(
                worker, image, sources[:2], True, args.iterations
            ),
        }

    finally:
        worker.close()
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
