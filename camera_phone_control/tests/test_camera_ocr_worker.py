"""Focused process-boundary and unchanged-screen cache tests."""

import multiprocessing
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from automation.screen.camera_ocr import (
    CameraOCRScreenStateProvider,
    _PaddleWorkerClient,
    _build_paddle_engine,
    _ocr_language_for_text,
    _ocr_language_profile,
)
from automation.screen.state import ScreenStateError


def protocol_worker(connection, config):
    """Small spawned worker used to test the production parent protocol."""
    try:
        with config["initializations"].get_lock():
            config["initializations"].value += 1
        try:
            connection.send(("ready", os.getpid(), 0.001))
        except (BrokenPipeError, EOFError, OSError):
            return
        while True:
            try:
                message = connection.recv()
            except (BrokenPipeError, EOFError, OSError):
                return
            if message[0] == "shutdown":
                return
            _, request_id, image = message
            command = int(image.flat[0])
            try:
                if command == 250:
                    connection.send(
                        (
                            "error",
                            request_id,
                            "RuntimeError",
                            "test failure",
                            os.getpid(),
                        )
                    )
                elif command == 251:
                    os._exit(7)
                elif command == 252:
                    time.sleep(5)
                else:
                    if command >= 200:
                        time.sleep(0.2)
                    text = "Log In" if command in {0, 1} else f"Value {command}"
                    connection.send(
                        (
                            "result",
                            request_id,
                            [
                                {
                                    "res": {
                                        "rec_texts": [text],
                                        "rec_scores": [0.9],
                                        "rec_boxes": [[0, 0, 10, 10]],
                                    }
                                }
                            ],
                            0.002,
                            os.getpid(),
                        )
                    )
            except (BrokenPipeError, EOFError, OSError):
                return
    finally:
        stopped = config.get("stopped")
        if stopped is not None:
            stopped.set()
        connection.close()


def startup_error_worker(connection, config):
    connection.send(
        (
            "startup_error",
            os.getpid(),
            "OSError",
            "model unavailable",
            {
                "exception_detail": "native DLL unavailable",
                "exception_chain": "TextDetectionUnavailable <- OSError",
                "traceback_summary": "camera_ocr.py:123:_build_paddle_engine",
            },
        )
    )
    connection.close()


class PaddleWorkerClientTests(unittest.TestCase):
    def setUp(self):
        self.context = multiprocessing.get_context("spawn")
        self.initializations = self.context.Value("i", 0)
        self.stopped = self.context.Event()

    def client(self, timeout=2):
        return _PaddleWorkerClient(
            {
                "language": "en",
                "textline_orientation": False,
                "cpu_threads": 1,
                "initializations": self.initializations,
                "stopped": self.stopped,
            },
            timeout,
            context=self.context,
            target=protocol_worker,
        )

    def test_worker_starts_once_and_serves_successful_requests(self):
        client = self.client()
        self.addCleanup(client.close)
        first, first_timing = client.request(np.zeros((4, 4), np.uint8))
        worker_pid = client.pid
        second, second_timing = client.request(np.ones((4, 4), np.uint8))
        self.assertEqual(first[0]["res"]["rec_texts"], ["Log In"])
        self.assertEqual(second[0]["res"]["rec_texts"], ["Log In"])
        self.assertEqual(first_timing["worker_pid"], worker_pid)
        self.assertEqual(second_timing["worker_pid"], worker_pid)
        self.assertNotEqual(worker_pid, os.getpid())
        self.assertEqual(self.initializations.value, 1)

    def test_explicit_warm_start_initializes_once_before_first_request(self):
        client = self.client()
        self.addCleanup(client.close)
        client.start()
        worker_pid = client.pid
        _, timing = client.request(np.zeros((4, 4), np.uint8))
        self.assertEqual(client.pid, worker_pid)
        self.assertEqual(timing["worker_start"], 0)
        self.assertEqual(self.initializations.value, 1)
        self.assertIn("queue_wait", timing)

    def test_worker_startup_error_is_clear(self):
        client = _PaddleWorkerClient(
            {"language": "en", "textline_orientation": False, "cpu_threads": 1},
            1,
            context=self.context,
            target=startup_error_worker,
        )
        with patch("automation.screen.camera_ocr.log_event") as logged:
            with self.assertRaisesRegex(
                ScreenStateError, "could not initialize: OSError"
            ):
                client.request(np.zeros((2, 2), np.uint8))
        self.assertIsNone(client.pid)
        fields = logged.call_args.kwargs
        self.assertEqual(fields["exception_type"], "OSError")
        self.assertEqual(fields["exception_detail"], "native DLL unavailable")
        self.assertIn("TextDetectionUnavailable", fields["exception_chain"])
        self.assertIn("_build_paddle_engine", fields["traceback_summary"])

    def test_worker_exception_is_propagated_and_worker_remains_available(self):
        client = self.client()
        self.addCleanup(client.close)
        with self.assertRaisesRegex(ScreenStateError, "RuntimeError: test failure"):
            client.request(np.full((2, 2), 250, np.uint8))
        result, _ = client.request(np.zeros((2, 2), np.uint8))
        self.assertEqual(result[0]["res"]["rec_texts"], ["Log In"])
        self.assertEqual(self.initializations.value, 1)

    def test_request_timeout_stops_failed_worker(self):
        client = self.client(timeout=2)
        self.addCleanup(client.close)
        client.start()
        self.assertIsNotNone(client.pid)
        client.timeout = 0.15
        with self.assertRaisesRegex(ScreenStateError, "timed out during inference"):
            client.request(np.full((2, 2), 252, np.uint8))
        self.assertIsNone(client.pid)

    def test_worker_crash_returns_error_without_hanging(self):
        client = self.client(timeout=1)
        with self.assertRaisesRegex(ScreenStateError, "stopped unexpectedly"):
            client.request(np.full((2, 2), 251, np.uint8))
        self.assertIsNone(client.pid)

    def test_shutdown_joins_worker(self):
        client = self.client()
        client.request(np.zeros((2, 2), np.uint8))
        worker_pid = client.pid
        client.close()
        self.assertIsNone(client.pid)
        self.assertTrue(self.stopped.wait(1), f"worker {worker_pid} did not stop")

    def paddle_options(self, language, *, packaged_models=None):
        paddleocr = Mock()
        paddleocr.PaddleOCR.return_value = object()
        paddle = Mock()
        paddle.framework.core = Mock()
        with patch.dict(
            sys.modules, {"paddle": paddle, "paddleocr": paddleocr}
        ), patch.dict(
            os.environ,
            {"PHONE_CONTROLLER_OCR_MODELS_DIR": packaged_models or ""},
            clear=False,
        ):
            engine = _build_paddle_engine(language, False, 1, 1)

        self.assertIs(engine, paddleocr.PaddleOCR.return_value)
        return paddleocr.PaddleOCR.call_args.kwargs

    def test_default_engine_preserves_english_recognizer(self):
        options = self.paddle_options("en")
        self.assertEqual(options["text_detection_model_name"], "PP-OCRv5_mobile_det")
        self.assertEqual(
            options["text_recognition_model_name"], "en_PP-OCRv5_mobile_rec"
        )
        self.assertEqual(options["lang"], "en")
        self.assertEqual(options["text_recognition_batch_size"], 1)
        self.assertFalse(options["use_doc_orientation_classify"])
        self.assertFalse(options["use_doc_unwarping"])

    def test_chinese_uses_multilingual_recognizer(self):
        options = self.paddle_options("zh-HK")
        self.assertEqual(options["lang"], "chinese_cht")
        self.assertEqual(options["text_recognition_model_name"], "PP-OCRv5_mobile_rec")

    def test_zh_tw_uses_same_traditional_chinese_profile(self):
        self.assertEqual(_ocr_language_profile("zh-TW"), _ocr_language_profile("zh-HK"))

    def test_zh_cn_uses_chinese_profile_and_english_remains_separate(self):
        self.assertEqual(_ocr_language_profile("zh-CN")[0], "ch")
        self.assertEqual(_ocr_language_profile("zh-CN")[2], "PP-OCRv5_mobile_rec")
        self.assertEqual(_ocr_language_profile("en")[2], "en_PP-OCRv5_mobile_rec")

    def test_cjk_targets_route_to_chinese_and_latin_stays_configured(self):
        for target in ("取消", "確認", "轉賬成功", "取消", "确认", "转账成功"):
            self.assertEqual(_ocr_language_for_text(target, "en"), "chinese_cht")
        for target in ("Cancel", "Confirm", "123456", "RM 88.00"):
            self.assertEqual(_ocr_language_for_text(target, "en"), "en")

    def test_malay_uses_latin_recognizer(self):
        options = self.paddle_options("ms")
        self.assertEqual(options["lang"], "ms")
        self.assertEqual(
            options["text_recognition_model_name"], "latin_PP-OCRv5_mobile_rec"
        )

    def test_packaged_model_paths_are_explicit_and_language_specific(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for model in (
                "PP-OCRv5_mobile_det",
                "PP-OCRv5_mobile_rec",
                "PP-LCNet_x1_0_textline_ori",
            ):
                (root / model).mkdir()
                for filename in (
                    "config.json",
                    "inference.json",
                    "inference.pdiparams",
                    "inference.yml",
                ):
                    (root / model / filename).write_text("model", encoding="utf-8")
            options = self.paddle_options("zh-HK", packaged_models=temporary)
        self.assertEqual(
            options["text_recognition_model_dir"],
            str(root / "PP-OCRv5_mobile_rec"),
        )
        self.assertEqual(
            options["text_detection_model_dir"],
            str(root / "PP-OCRv5_mobile_det"),
        )

    def test_unsupported_language_is_rejected_before_model_loading(self):
        with self.assertRaisesRegex(ScreenStateError, "does not support language"):
            _ocr_language_profile("klingon")

    def test_provider_uses_configured_ocr_language(self):
        worker = Mock()
        with patch.dict(os.environ, {"CAMERA_OCR_LANGUAGE": "malay"}), patch(
            "automation.screen.camera_ocr._PaddleWorkerClient", return_value=worker
        ) as worker_type:
            provider = CameraOCRScreenStateProvider(Mock())
        self.assertEqual(provider.language, "ms")
        self.assertEqual(worker_type.call_args.args[0]["language"], "ms")


class CameraOCRCacheTests(unittest.TestCase):
    def provider(self, worker):
        with patch.dict(os.environ, {"CAMERA_OCR_CACHE_THRESHOLD": "0.001"}), patch(
            "automation.screen.camera_ocr._PaddleWorkerClient",
            return_value=worker,
        ):
            return CameraOCRScreenStateProvider(Mock())

    @staticmethod
    def worker():
        worker = Mock()
        worker.pid = 7654
        worker.request.return_value = (
            [
                {
                    "res": {
                        "rec_texts": ["Accounts"],
                        "rec_scores": [0.95],
                        "rec_boxes": [[0, 0, 20, 10]],
                    }
                }
            ],
            {
                "worker_start": 0,
                "ipc_send": 0,
                "ipc_wait": 0,
                "inference": 0,
                "worker_pid": 7654,
            },
        )
        return worker

    def test_unchanged_frame_uses_cached_compatible_output(self):
        worker = self.worker()
        provider = self.provider(worker)
        image = np.full((300, 180), 100, np.uint8)
        first = provider.visible_text_candidates_from_image(image)
        second = provider.visible_text_candidates_from_image(image.copy())
        self.assertEqual(first, second)
        self.assertEqual(first[0]["text"], "Accounts")
        worker.request.assert_called_once()

    def test_changed_frame_misses_cache(self):
        worker = self.worker()
        provider = self.provider(worker)
        provider.visible_texts_from_image(np.zeros((300, 180), np.uint8))
        provider.visible_texts_from_image(np.full((300, 180), 255, np.uint8))
        self.assertEqual(worker.request.call_count, 2)

    def test_chinese_and_english_recognizers_never_share_cache_entries(self):
        worker = self.worker()
        provider = self.provider(worker)
        provider._language_workers["chinese_cht"] = worker
        image = np.full((300, 180), 100, np.uint8)

        provider._recognize_candidates(image, expected="Cancel", scope="full")
        provider._recognize_candidates(image, expected="取消", scope="full")

        self.assertEqual(worker.request.call_count, 2)
        self.assertIn(("en", "full"), provider._cache_by_scope)
        self.assertIn(("chinese_cht", "full"), provider._cache_by_scope)

    def test_chinese_roi_and_full_frame_use_separate_cache_entries(self):
        worker = self.worker()
        provider = self.provider(worker)
        provider._language_workers["chinese_cht"] = worker
        image = np.full((300, 180), 100, np.uint8)

        provider._recognize_candidates(image, expected="確認", scope="roi:test")
        provider._recognize_candidates(image, expected="確認", scope="full")

        self.assertEqual(worker.request.call_count, 2)
        self.assertIn(("chinese_cht", "roi:test"), provider._cache_by_scope)
        self.assertIn(("chinese_cht", "full"), provider._cache_by_scope)

    def test_failed_ocr_is_not_cached(self):
        worker = self.worker()
        worker.request.side_effect = [
            ScreenStateError("failed"),
            worker.request.return_value,
        ]
        provider = self.provider(worker)
        image = np.zeros((300, 180), np.uint8)
        with self.assertRaisesRegex(ScreenStateError, "failed"):
            provider.visible_texts_from_image(image)
        self.assertEqual(provider.visible_texts_from_image(image), ["Accounts"])
        self.assertEqual(worker.request.call_count, 2)

    def test_failure_invalidates_an_older_successful_cache_entry(self):
        worker = self.worker()
        success = worker.request.return_value
        worker.request.side_effect = [success, ScreenStateError("failed"), success]
        provider = self.provider(worker)
        original = np.zeros((300, 180), np.uint8)
        changed = np.full((300, 180), 255, np.uint8)
        provider.visible_texts_from_image(original)
        with self.assertRaisesRegex(ScreenStateError, "failed"):
            provider.visible_texts_from_image(changed)
        self.assertEqual(provider.visible_texts_from_image(original), ["Accounts"])
        self.assertEqual(worker.request.call_count, 3)

    def test_recognition_batch_size_defaults_to_one_and_is_configurable(self):
        worker = self.worker()
        provider = self.provider(worker)
        self.assertEqual(provider.cpu_threads, 1)
        self.assertEqual(provider.recognition_batch_size, 1)
        with patch.dict(
            os.environ,
            {"CAMERA_OCR_RECOGNITION_BATCH_SIZE": "3"},
        ), patch(
            "automation.screen.camera_ocr._PaddleWorkerClient",
            return_value=worker,
        ):
            provider = CameraOCRScreenStateProvider(Mock())
        self.assertEqual(provider.recognition_batch_size, 3)


if __name__ == "__main__":
    unittest.main()
