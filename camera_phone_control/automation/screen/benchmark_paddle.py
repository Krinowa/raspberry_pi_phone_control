"""Benchmark the production Paddle worker with a saved real camera crop."""

import argparse
import json
from pathlib import Path
import statistics

import cv2

from .camera_ocr import _PaddleWorkerClient, _plain_result

EXPECTED_DEFAULTS = ("Amount", "RM", "Touch the fingerprint sensor", "Done")


def resized(image, scale=None, longest_side=None):
    height, width = image.shape[:2]

    if longest_side is not None:
        scale = min(1.0, longest_side / max(width, height))
    if scale is None or scale == 1:
        return image.copy()

    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def percentile(values, percentile):
    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower

    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def result_text(results):
    values = []
    for result in results:
        values.extend(_plain_result(result)["res"]["rec_texts"])
    return " ".join(values).casefold()


def benchmark_variant(worker, name, image, expected, iterations):
    paddle_input = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    # Keep shape warmup separate so predictor/allocator reshaping is visible.
    warm_results, warm_timing = worker.request(paddle_input)
    samples = []
    cpu_samples = []
    matches = {text: True for text in expected}
    region_counts = []
    for _ in range(iterations):
        results, timing = worker.request(paddle_input)
        text = result_text(results)
        samples.append(timing["inference"])
        cpu_samples.append(timing.get("cpu_user", 0) + timing.get("cpu_system", 0))
        region_counts.append(
            sum(len(_plain_result(item)["res"]["rec_texts"]) for item in results)
        )

        for expected_text in expected:
            matches[expected_text] &= expected_text.casefold() in text

    return {
        "name": name,
        "width": image.shape[1],
        "height": image.shape[0],
        "shape_warmup_inference_seconds": warm_timing["inference"],
        "model_start_seconds": warm_timing["worker_start"],
        "median_inference_seconds": statistics.median(samples),
        "p95_inference_seconds": percentile(samples, 0.95),
        "median_child_cpu_seconds": statistics.median(cpu_samples),
        "native_thread_count": warm_timing.get("native_threads"),
        "region_count_range": [min(region_counts), max(region_counts)],
        "expected_text_present_every_run": matches,
        "warmup_region_count": sum(
            len(_plain_result(item)["res"]["rec_texts"]) for item in warm_results
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--recognition-batch-size", type=int)
    parser.add_argument("--expected", action="append", default=[])
    parser.add_argument("--current-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.iterations < 1:
        parser.error("--iterations must be positive")

    image = cv2.imread(str(args.image), cv2.IMREAD_GRAYSCALE)

    if image is None:
        parser.error("image could not be read")

    expected = args.expected or list(EXPECTED_DEFAULTS)
    variants = [
        ("current", resized(image)),
        ("75_percent", resized(image, scale=0.75)),
        ("50_percent", resized(image, scale=0.5)),
        ("longest_960", resized(image, longest_side=960)),
        ("longest_720", resized(image, longest_side=720)),
    ]

    if args.current_only:
        variants = variants[:1]

    worker = _PaddleWorkerClient(
        {
            "language": "en",
            "textline_orientation": False,
            "cpu_threads": args.cpu_threads,
            "recognition_batch_size": args.recognition_batch_size,
        },
        30,
    )

    try:
        report = {
            "source": str(args.image),
            "cpu_threads": args.cpu_threads,
            "recognition_batch_size": args.recognition_batch_size,
            "iterations": args.iterations,
            "variants": [
                benchmark_variant(worker, name, candidate, expected, args.iterations)
                for name, candidate in variants
            ],
        }

    finally:
        worker.close()
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
