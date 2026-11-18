"""Regression coverage for hardware-free frozen application validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from windows_launcher import (
    _PackageSmokeCamera,
    _package_smoke_ocr_inference,
    _package_smoke_fixtures,
    _package_smoke_recovery,
    _package_smoke_roi,
    _close_application,
    _create_application,
)
from windows_runtime import RuntimePaths


class FrozenLauncherSmokeTests(unittest.TestCase):
    def test_language_fixtures_decode_and_expected_text_is_required(self):
        fixtures = _package_smoke_fixtures(Path(__file__).parent)
        self.assertEqual(set(fixtures), {"en", "zh-HK", "ms"})
        self.assertEqual(fixtures["zh-HK"][1], "確認取消")
        for language, (image, expected) in fixtures.items():
            self.assertEqual(image.shape, (160, 900, 3))
            self.assertLess(image.min(), 10)
            pool = Mock()
            provider = Mock()
            provider._worker_for_language.return_value = pool
            pool.request.return_value = (
                [{"rec_texts": [expected]}],
                {"inference": .01, "worker_pid": 123, "console_streams_absent": True},
            )
            self.assertTrue(_package_smoke_ocr_inference(provider, language, image, expected)["fixture_matched"])
            pool.request.return_value[1]["console_streams_absent"] = False
            with self.assertRaisesRegex(RuntimeError, "absent console"):
                _package_smoke_ocr_inference(provider, language, image, expected)
            pool.request.return_value = (
                [{"rec_texts": ["HON"]}],
                {"inference": .01, "worker_pid": 123, "console_streams_absent": True},
            )
            with self.assertRaisesRegex(RuntimeError, "did not recognize"):
                _package_smoke_ocr_inference(provider, language, image, expected)

    def test_controlled_error_preserves_details_and_requires_three_fresh_predictions(self):
        from automation.screen.state import ScreenStateError

        provider = Mock()
        pool = provider._worker_for_language.return_value
        error = ScreenStateError("controlled predictor failure")
        error.ocr_diagnostics = {"exception_type": "ValueError", "traceback_summary": "predict.py:1:predict"}
        pool.request.side_effect = [error] + [
            ([{"rec_texts": ["Ready"]}], {"inference": .01, "worker_pid": 123, "console_streams_absent": True})
            for _ in range(3)
        ]
        pool.status.return_value = {"busy_workers": 0, "queued_requests": 0}
        report = _package_smoke_recovery(provider, "en", np.zeros((20, 40, 3), np.uint8), "Ready")
        self.assertEqual(pool.request.call_count, 4)
        self.assertEqual(pool.request.call_args_list[0].args[0].shape, (0, 0, 3))
        self.assertEqual(report["controlled_error"]["exception_type"], "ValueError")
        self.assertEqual(len(report["subsequent_results"]), 3)

    def test_roi_smoke_uses_real_handlers_and_rejects_missing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.create(Path(temporary) / "data")
            app, manager = _create_application(paths, package_smoke=True)
            try:
                provider = app.extensions["workflow_screen_state_provider"]
                fixtures = _package_smoke_fixtures(Path(__file__).parent)
                pool = Mock()
                pool.request.return_value = (
                    [{"res": {"rec_texts": [text for _, text in fixtures.values()],
                               "rec_scores": [.99] * 3, "rec_boxes": [[0, 0, 100, 100]] * 3}}],
                    {"inference": .01, "worker_pid": 123},
                )
                for language in ("en", "chinese_cht", "zh-HK", "ms"):
                    provider._language_workers[language] = pool
                original_language = provider.language
                report = _package_smoke_roi(app, provider, fixtures)
                self.assertEqual(provider.language, original_language)
                for results in report.values():
                    self.assertEqual([item["found"] for item in results], [True, False, True])
                self.assertEqual(pool.request.call_count, 9)
            finally:
                _close_application(app, manager)

    def test_ocr_smoke_runs_real_request_without_returning_recognized_text(self):
        pool = Mock()
        pool.request.return_value = (
            [{"res": {"rec_texts": ["Sensitive screen text"]}}],
            {"inference": 0.125, "worker_pid": 4321},
        )
        provider = Mock()
        provider._worker_for_language.return_value = pool

        result = _package_smoke_ocr_inference(
            provider, "zh-HK", np.zeros((20, 40, 3), np.uint8)
        )

        provider._worker_for_language.assert_called_once_with("zh-HK")
        pool.request.assert_called_once()
        self.assertEqual(
            result,
            {
                "language": "zh-HK",
                "recognition_model": "PP-OCRv5_mobile_rec",
                "result_batch_count": 1,
                "region_count": 1,
                "inference_ms": 125.0,
                "worker_id": 4321,
            },
        )
        self.assertNotIn("Sensitive screen text", repr(result))

    def test_smoke_application_uses_no_capture_thread_and_closes_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.create(Path(temporary) / "data")
            app, manager = _create_application(paths, package_smoke=True)
            camera = manager.cameras["phone_A"]
            remote = app.extensions["remote_control"]
            try:
                self.assertIsInstance(camera, _PackageSmokeCamera)
                self.assertFalse(hasattr(camera, "thread"))
                self.assertIsNone(app.extensions["job_executor"].thread)
                self.assertEqual(
                    remote.history.path,
                    paths.jobs / "remote_control_history.jsonl",
                )

                registered = {
                    (rule.rule, method)
                    for rule in app.url_map.iter_rules()
                    for method in rule.methods - {"HEAD", "OPTIONS"}
                }
                self.assertIn(
                    ("/agent/v1/devices/<device_id>/frames", "GET"), registered
                )
                self.assertIn(
                    ("/agent/v1/devices/<device_id>/takeover", "POST"), registered
                )
                self.assertIn(
                    ("/agent/v1/devices/<device_id>/screen", "PUT"), registered
                )

                client = app.test_client()
                responses = (
                    client.get("/", follow_redirects=True),
                    client.get(
                        "/api/status",
                        headers={"X-Control-Client": "package-smoke"},
                    ),
                    client.get("/static/app.js"),
                    client.get("/static/i18n.js"),
                    client.get("/static/workflows.js"),
                    client.get("/static/style.css"),
                    client.get("/api/workflow-options/transfer-methods"),
                )
                try:
                    self.assertEqual(
                        [response.status_code for response in responses],
                        [200, 200, 200, 200, 200, 200, 200],
                    )
                    self.assertEqual(
                        {
                            method["value"]
                            for method in responses[-1].get_json()[
                                "transfer_methods"
                            ]
                        },
                        {"bank_account", "phone_no", "email", "fps_id"},
                    )
                finally:
                    for response in responses:
                        response.close()
            finally:
                with patch.object(remote, "close", wraps=remote.close) as close_remote:
                    _close_application(app, manager)
                    close_remote.assert_called_once_with()

            self.assertTrue(camera.closed)
            self.assertEqual(manager.cameras, {})


if __name__ == "__main__":
    unittest.main()
