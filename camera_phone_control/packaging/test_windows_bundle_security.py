"""Fail-closed checks for sensitive files in the Windows distribution."""

from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGING))

from validate_windows_bundle import (  # noqa: E402
    CHARDET_MYPYC_MODULES,
    PADDLEX_OCR_CORE_METADATA,
    _forbidden_bundle_inputs,
    _validate_chardet_runtime_extensions,
    _validate_cython_runtime_data,
    _validate_paddlex_ocr_core_metadata,
    _validate_smoke_report,
)


class WindowsBundleSecurityTests(unittest.TestCase):
    @staticmethod
    def valid_smoke_report():
        routes = {
            ("/agent/v1/devices/<device_id>/frames", "GET"),
            ("/agent/v1/devices/<device_id>/takeover", "POST"),
            ("/agent/v1/devices/<device_id>/takeover/renew", "POST"),
            ("/agent/v1/devices/<device_id>/takeover", "DELETE"),
            ("/agent/v1/devices/<device_id>/input", "POST"),
            ("/agent/v1/devices/<device_id>/screen", "GET"),
            ("/agent/v1/devices/<device_id>/screen", "PUT"),
            ("/api/remote-control", "GET"),
            ("/api/devices/<device_id>/remote-control/end", "POST"),
        }
        endpoints = (
            "/",
            "/api/status",
            "/static/app.js",
            "/static/i18n.js",
            "/static/workflows.js",
            "/static/style.css",
            "/api/workflow-options/transfer-methods",
        )
        models = {
            "en": "en_PP-OCRv5_mobile_rec",
            "zh-HK": "PP-OCRv5_mobile_rec",
            "ms": "latin_PP-OCRv5_mobile_rec",
        }
        shutdown = {
            "pool_size": 1,
            "busy_workers": 0,
            "queued_requests": 0,
            "accepting": False,
            "workers": [{"worker_id": 0, "state": "IDLE", "pid": None}],
        }
        return {
            "exit_code": 0,
            "phase": "complete",
            "console_streams_absent": True,
            "ocr_recovery": {
                language: {
                    "controlled_error": {"wrapper_type": "ScreenStateError"},
                    "subsequent_results": [
                        {"fixture_matched": True, "console_streams_absent": True}
                        for _ in range(3)
                    ],
                    "status": {"busy_workers": 0, "queued_requests": 0},
                } for language in models
            },
            "roi_api": {
                language: [{"status_code": 200, "found": found} for found in (True, False, True)]
                for language in models
            },
            "compiled_imports": [{"module": "protected.test"}],
            "multiprocessing_exit_code": 0,
            "ocr_worker_status": {"workers": [{"state": "IDLE"}]},
            "ocr_language_statuses": {
                language: {"workers": [{"state": "IDLE"}]}
                for language in ("en", "zh-HK", "zh-TW")
            },
            "ocr_inference_results": [
                {
                    "language": language,
                    "recognition_model": model,
                    "result_batch_count": 1,
                    "region_count": 1,
                    "inference_ms": 10.0,
                    "worker_id": index + 100,
                    "fixture_matched": True,
                    "fixture_recognized": True,
                    "console_streams_absent": True,
                }
                for index, (language, model) in enumerate(models.items())
            ],
            "runtime_model_download_count": 0,
            "http_health": {
                "status_code": 200,
                "content_type": "application/json",
                "thread_stopped": True,
            },
            "ocr_shutdown_statuses": {
                language: dict(shutdown) for language in models
            },
            "endpoints": [
                {"endpoint": endpoint, "status_code": 200} for endpoint in endpoints
            ],
            "transfer_method_values": [
                "bank_account",
                "email",
                "fps_id",
                "phone_no",
            ],
            "remoteviewer_routes": [
                {"route": route, "method": method} for route, method in routes
            ],
        }

    def test_frozen_smoke_report_requires_real_ocr_inference(self):
        report = {
            "exit_code": 0,
            "phase": "complete",
            "compiled_imports": [{"module": "protected.test"}],
            "multiprocessing_exit_code": 0,
            "ocr_worker_status": {"workers": [{"state": "IDLE"}]},
            "ocr_language_statuses": {
                language: {"workers": [{"state": "IDLE"}]}
                for language in ("en", "zh-HK", "zh-TW")
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "inference smoke checks"):
                _validate_smoke_report(path, ["protected.test"])

    def test_frozen_smoke_report_requires_nonempty_offline_ocr_and_cleanup(self):
        cases = (
            ("region_count", 0, "invalid diagnostics"),
            ("recognition_model", "wrong", "unexpected recognition model"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                report = self.valid_smoke_report()
                report["ocr_inference_results"][0][field] = value
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "report.json"
                    path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, message):
                        _validate_smoke_report(path, ["protected.test"])

        report = self.valid_smoke_report()
        report["runtime_model_download_count"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "runtime model downloads"):
                _validate_smoke_report(path, ["protected.test"])

        report = self.valid_smoke_report()
        report["ocr_shutdown_statuses"]["zh-HK"]["workers"][0]["pid"] = 4321
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "shutdown check"):
                _validate_smoke_report(path, ["protected.test"])

        report = self.valid_smoke_report()
        report["http_health"]["thread_stopped"] = False
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "HTTP listener"):
                _validate_smoke_report(path, ["protected.test"])

    def test_complete_frozen_smoke_report_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(
                json.dumps(self.valid_smoke_report()), encoding="utf-8"
            )
            report = _validate_smoke_report(path, ["protected.test"])
            self.assertEqual(report["runtime_model_download_count"], 0)

    def test_windowed_fixture_recovery_and_roi_checks_cannot_be_omitted(self):
        import copy

        mutations = (
            lambda report: report.update(console_streams_absent=False),
            lambda report: report["ocr_inference_results"][0].update(fixture_recognized=False),
            lambda report: report.update(ocr_recovery={}),
            lambda report: report["ocr_recovery"]["en"].update(subsequent_results=[]),
            lambda report: report["roi_api"]["zh-HK"][1].update(found=True),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                report = copy.deepcopy(self.valid_smoke_report())
                mutate(report)
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "report.json"
                    path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaises(RuntimeError):
                        _validate_smoke_report(path, ["protected.test"])

    def test_all_paddlex_ocr_core_distribution_metadata_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary)

            with self.assertRaisesRegex(RuntimeError, "python-bidi"):
                _validate_paddlex_ocr_core_metadata(resources)

            prefixes = {
                "opencv-contrib-python": "opencv_contrib_python",
                "python-bidi": "python_bidi",
            }
            for distribution in PADDLEX_OCR_CORE_METADATA:
                prefix = prefixes.get(distribution, distribution)
                metadata = resources / f"{prefix}-1.0.dist-info" / "METADATA"
                metadata.parent.mkdir()
                metadata.write_text(
                    f"Name: {distribution}\nVersion: 1.0\n", encoding="utf-8"
                )
            self.assertEqual(_validate_paddlex_ocr_core_metadata(resources), resources)

            metadata = resources / "python_bidi-1.0.dist-info" / "METADATA"
            metadata.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "python-bidi"):
                _validate_paddlex_ocr_core_metadata(resources)

    def test_all_chardet_mypyc_runtime_extensions_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary)
            chardet_root = resources / "chardet"

            with self.assertRaisesRegex(RuntimeError, "pipeline.orchestrator"):
                _validate_chardet_runtime_extensions(resources)

            for module in CHARDET_MYPYC_MODULES:
                relative = Path(*module.split("."))
                extension = (
                    chardet_root
                    / relative.parent
                    / (f"{relative.name}__mypyc.cp313-win_amd64.pyd")
                )
                extension.parent.mkdir(parents=True, exist_ok=True)
                extension.write_bytes(b"test extension")
            self.assertEqual(
                _validate_chardet_runtime_extensions(resources), chardet_root
            )

            (
                chardet_root / "pipeline" / "orchestrator__mypyc.cp313-win_amd64.pyd"
            ).write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "pipeline.orchestrator"):
                _validate_chardet_runtime_extensions(resources)

    def test_cython_runtime_utility_data_is_required_and_nonempty(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary)
            utility = resources / "Cython" / "Utility" / "CppSupport.cpp"

            with self.assertRaisesRegex(RuntimeError, "runtime utility data"):
                _validate_cython_runtime_data(resources)

            utility.parent.mkdir(parents=True)
            utility.touch()
            with self.assertRaisesRegex(RuntimeError, "runtime utility data"):
                _validate_cython_runtime_data(resources)

            utility.write_text("// Cython runtime template", encoding="utf-8")
            self.assertEqual(_validate_cython_runtime_data(resources), utility)

    def test_remoteviewer_secret_file_is_rejected_from_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            distribution = Path(temporary)
            secret = distribution / "static" / "remoteviewer.secret"
            secret.parent.mkdir()
            secret.write_text("test-only-secret", encoding="utf-8")
            (distribution / "static" / "app.js").write_text(
                "// safe asset", encoding="utf-8"
            )

            self.assertEqual(_forbidden_bundle_inputs(distribution), (secret,))


if __name__ == "__main__":
    unittest.main()
