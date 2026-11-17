"""Static packaging contracts that can be verified without a Windows build."""

import hashlib
import io
import json
import importlib.util
import tempfile
import unittest
import struct
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from hardware.camera_identity import resolve_ffmpeg_executable
from windows_runtime import RuntimePaths

APP_ROOT = Path(__file__).resolve().parents[1]


def _load_packaging_module(name):
    path = APP_ROOT / "packaging" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"phone_controller_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_models = _load_packaging_module("prepare_ocr_models")
MODEL_NAMES = _models.MODEL_NAMES
MODEL_PREPARATION_PROFILES = _models.MODEL_PREPARATION_PROFILES
REQUIRED_MODEL_FILES = _models.REQUIRED_MODEL_FILES
validate_models = _models._validate
_ffmpeg = _load_packaging_module("validate_ffmpeg")
FFmpegValidationError = _ffmpeg.FFmpegValidationError
validate_ffmpeg = _ffmpeg.validate_ffmpeg
_download = _load_packaging_module("download_ffmpeg")
FFmpegDownloadError = _download.FFmpegDownloadError
download_approved_ffmpeg = _download.download_approved_ffmpeg
resolve_ffmpeg_source = _download.resolve_source
_provision = _load_packaging_module("provision_ffmpeg")
FFmpegProvisioningError = _provision.FFmpegProvisioningError
provision_ffmpeg = _provision.provision_ffmpeg
_workflows = _load_packaging_module("validate_workflow_seeds")
WorkflowSeedError = _workflows.WorkflowSeedError
validate_seed_directory = _workflows.validate_seed_directory
_protected = _load_packaging_module("build_protected_modules")


class FakeDownloadResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self.stream = io.BytesIO(body)

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.stream.read(size)

    def close(self):
        self.stream.close()


class PackagingReadinessTests(unittest.TestCase):
    def test_windows_build_targets_only_cpython_31313_x64(self):
        packaging = APP_ROOT / "packaging"
        build = (packaging / "build_windows.bat").read_text(encoding="utf-8")
        readme = (packaging / "README_WINDOWS.md").read_text(encoding="utf-8")
        requirements = (packaging / "requirements-windows.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("py -3.13 -m venv", build)
        self.assertIn(".venv-packaging-3.13.13", build)
        self.assertIn("sys.version_info[:3] == (3, 13, 13)", build)
        self.assertIn("CPython 3.13.13 x64", build)
        self.assertIn("CPython 3.13.13 x64", readme)
        self.assertIn("CPython 3.13.13 x64", requirements)
        self.assertNotIn("Python 3.11", readme)
        self.assertNotIn("CPython 3.11", build + requirements)

    def test_windows_313_dependency_family_is_pinned(self):
        requirements = (APP_ROOT / "packaging" / "requirements-windows.txt").read_text(
            encoding="utf-8"
        )

        for requirement in (
            "numpy==2.1.3",
            "opencv-contrib-python==4.10.0.84",
            "paddleocr==3.5.0",
            "paddlepaddle==3.3.0",
            "paddlex[ocr-core]==3.5.2",
            "PyInstaller==6.16.0",
            "pyinstaller-hooks-contrib==2026.2",
            "Cython==3.1.6",
        ):
            self.assertIn(requirement, requirements)

        self.assertNotIn("opencv-contrib-python==4.11.0.86", requirements)
        self.assertEqual(requirements.count("paddlex[ocr-core]==3.5.2"), 1)
        self.assertNotIn("\npaddlex==3.5.2", requirements)

    def test_complete_offline_models_validate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in MODEL_NAMES:
                directory = root / name
                directory.mkdir()
                for filename in REQUIRED_MODEL_FILES:
                    (directory / filename).write_text("prepared", encoding="utf-8")
            self.assertEqual(len(validate_models(root)), len(MODEL_NAMES))

    def test_required_ocr_models_cover_english_chinese_and_malay(self):
        self.assertEqual(
            set(MODEL_NAMES),
            {
                "PP-OCRv5_mobile_det",
                "en_PP-OCRv5_mobile_rec",
                "PP-OCRv5_mobile_rec",
                "latin_PP-OCRv5_mobile_rec",
                "PP-LCNet_x1_0_textline_ori",
            },
        )
        self.assertEqual(
            MODEL_PREPARATION_PROFILES,
            (
                ("en", "en_PP-OCRv5_mobile_rec"),
                ("chinese_cht", "PP-OCRv5_mobile_rec"),
                ("ms", "latin_PP-OCRv5_mobile_rec"),
            ),
        )

    def test_incomplete_offline_model_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "missing or empty"):
                validate_models(Path(temporary))

    def test_curated_seed_rejects_recording_screenshot_and_payment_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unsafe.json").write_text(
                json.dumps(
                    {
                        "id": "unsafe",
                        "name": "Unsafe",
                        "purpose": "payment",
                        "variables": [{"name": "account_number", "default": "123456"}],
                        "steps": [{"type": "tap", "recording_screenshot": {}}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(WorkflowSeedError):
                validate_seed_directory(root)

    def test_empty_curated_seed_directory_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(validate_seed_directory(temporary), [])

    def test_frozen_ffmpeg_resource_precedes_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary) / "tools"
            tools.mkdir()
            executable = tools / "ffmpeg.exe"
            executable.write_bytes(b"approved-placeholder")
            self.assertEqual(
                resolve_ffmpeg_executable(
                    environment={}, frozen_root=temporary, which=lambda _: "PATH-ffmpeg"
                ),
                str(executable),
            )

    def test_frozen_ffmpeg_never_falls_back_to_global_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                resolve_ffmpeg_executable(
                    environment={},
                    frozen_root=temporary,
                    which=lambda _: "C:/global/ffmpeg.exe",
                ),
                str(Path(temporary) / "tools" / "ffmpeg.exe"),
            )

    def test_ffmpeg_falls_back_to_path_then_command_name(self):
        self.assertEqual(
            resolve_ffmpeg_executable(
                environment={}, frozen_root=None, which=lambda _: "C:/tools/ffmpeg.exe"
            ),
            "C:/tools/ffmpeg.exe",
        )
        self.assertEqual(
            resolve_ffmpeg_executable(
                environment={}, frozen_root=None, which=lambda _: None
            ),
            "ffmpeg",
        )

    @staticmethod
    def _write_x64_pe(path):
        payload = bytearray(256)
        payload[:2] = b"MZ"
        struct.pack_into("<I", payload, 0x3C, 0x80)
        payload[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", payload, 0x84, 0x8664)
        Path(path).write_bytes(payload)

    def test_ffmpeg_validator_requires_x64_and_directshow(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ffmpeg.exe"
            self._write_x64_pe(executable)
            version = Mock(
                returncode=0,
                stdout=b"ffmpeg version approved\x81",
                stderr=b"",
            )
            devices = Mock(
                returncode=0,
                stdout=b" D. = Demuxing supported\n dshow\x81",
                stderr=b"",
            )
            with patch.object(
                _ffmpeg.subprocess, "run", side_effect=[version, devices]
            ) as run:
                self.assertEqual(validate_ffmpeg(executable), executable.resolve())
            self.assertTrue(
                all(call.kwargs["text"] is False for call in run.call_args_list)
            )

    def test_ffmpeg_validator_rejects_build_without_directshow(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ffmpeg.exe"
            self._write_x64_pe(executable)
            version = Mock(returncode=0, stdout="ffmpeg version approved", stderr="")
            devices = Mock(returncode=0, stdout="file devices only", stderr="")
            with (
                patch.object(_ffmpeg.subprocess, "run", side_effect=[version, devices]),
                self.assertRaisesRegex(FFmpegValidationError, "DirectShow"),
            ):
                validate_ffmpeg(executable)

    def test_ffmpeg_provisioning_supports_direct_exe_and_single_zip_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct = root / "approved.exe"
            direct.write_bytes(b"MZdirect-approved")
            direct_target = root / "direct" / "ffmpeg.exe"
            direct_target.parent.mkdir()
            self.assertEqual(provision_ffmpeg(direct, direct_target), direct_target)
            self.assertEqual(direct_target.read_bytes(), b"MZdirect-approved")

            archive = root / "approved.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("approved-ffmpeg/bin/ffmpeg.exe", b"MZzip-approved")
                bundle.writestr("approved-ffmpeg/LICENSE.txt", b"license")
            zip_target = root / "zip" / "ffmpeg.exe"
            zip_target.parent.mkdir()
            self.assertEqual(provision_ffmpeg(archive, zip_target), zip_target)
            self.assertEqual(zip_target.read_bytes(), b"MZzip-approved")

    def test_ffmpeg_provisioning_rejects_unsafe_missing_and_ambiguous_zips(self):
        cases = {
            "unsafe": [("../ffmpeg.exe", b"MZunsafe")],
            "missing": [("bin/ffprobe.exe", b"MZprobe")],
            "ambiguous": [
                ("one/ffmpeg.exe", b"MZone"),
                ("two/FFMPEG.EXE", b"MZtwo"),
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, entries in cases.items():
                with self.subTest(name=name):
                    archive = root / f"{name}.zip"
                    with zipfile.ZipFile(archive, "w") as bundle:
                        for entry, payload in entries:
                            bundle.writestr(entry, payload)
                    target = root / f"{name}.exe"
                    with self.assertRaises(FFmpegProvisioningError):
                        provision_ffmpeg(archive, target)
                    self.assertFalse(target.exists())

    def test_ffmpeg_source_is_pinned_and_overrides_are_optional_but_paired(self):
        config_path = APP_ROOT / "packaging" / "ffmpeg_source.json"
        pinned_url, pinned_sha256 = resolve_ffmpeg_source(config_path, {})
        self.assertEqual(
            pinned_url,
            "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/"
            "autobuild-2026-11-11-12-39/"
            "ffmpeg-n9.0.2-49-gc179411bbb-win64-lgpl-9.0.zip",
        )
        self.assertEqual(
            pinned_sha256,
            "9ccd65428fb0e60a0d574b76145df41f402b754edd2d06c2e24fa506fc1c2b02",
        )
        override = {
            "PHONE_CONTROLLER_FFMPEG_URL": pinned_url.replace(
                "autobuild-2026-11-11-12-39", "autobuild-2026-11-10-12-39"
            ),
            "PHONE_CONTROLLER_FFMPEG_SHA256": "a" * 64,
        }
        self.assertEqual(
            resolve_ffmpeg_source(config_path, override),
            (override["PHONE_CONTROLLER_FFMPEG_URL"], "a" * 64),
        )
        for partial in (
            {"PHONE_CONTROLLER_FFMPEG_URL": override["PHONE_CONTROLLER_FFMPEG_URL"]},
            {"PHONE_CONTROLLER_FFMPEG_SHA256": "a" * 64},
        ):
            with self.assertRaisesRegex(FFmpegDownloadError, "provide both"):
                resolve_ffmpeg_source(config_path, partial)
        with self.assertRaisesRegex(FFmpegDownloadError, "pinned"):
            resolve_ffmpeg_source(
                config_path,
                {
                    "PHONE_CONTROLLER_FFMPEG_URL": pinned_url.replace(
                        "autobuild-2026-11-11-12-39", "latest"
                    ),
                    "PHONE_CONTROLLER_FFMPEG_SHA256": "a" * 64,
                },
            )

    def test_ffmpeg_download_allows_constrained_github_asset_redirect(self):
        source_url, expected_sha256 = resolve_ffmpeg_source(
            APP_ROOT / "packaging" / "ffmpeg_source.json", {}
        )
        asset_url = (
            "https://release-assets.githubusercontent.com/github-production-release-asset/"
            "123/approved?sig=redacted"
        )
        payload = b"approved complete ZIP bytes"
        responses = [
            FakeDownloadResponse(302, headers={"Location": asset_url}),
            FakeDownloadResponse(200, body=payload),
        ]
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(_download, "_request_once", side_effect=responses) as request,
        ):
            destination = Path(temporary) / "ffmpeg.zip"
            self.assertEqual(
                download_approved_ffmpeg(
                    source_url,
                    destination,
                    hashlib.sha256(payload).hexdigest(),
                ),
                destination,
            )
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(
                [item.args[1] for item in request.call_args_list],
                [source_url, asset_url],
            )
        self.assertNotEqual(expected_sha256, hashlib.sha256(payload).hexdigest())

    def test_ffmpeg_download_blocks_redirects_and_checksum_mismatch(self):
        source_url, _ = resolve_ffmpeg_source(
            APP_ROOT / "packaging" / "ffmpeg_source.json", {}
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, location in enumerate(
                (
                    "http://release-assets.githubusercontent.com/asset.zip",
                    "https://downloads.example/asset.zip",
                )
            ):
                with (
                    self.subTest(location=location),
                    patch.object(
                        _download,
                        "_request_once",
                        return_value=FakeDownloadResponse(
                            302, headers={"Location": location}
                        ),
                    ),
                ):
                    destination = root / f"blocked-{index}.zip"
                    with self.assertRaisesRegex(FFmpegDownloadError, "must use HTTPS"):
                        download_approved_ffmpeg(source_url, destination, "0" * 64)
                    self.assertFalse(destination.exists())

            with patch.object(
                _download,
                "_request_once",
                return_value=FakeDownloadResponse(200, body=b"wrong bytes"),
            ):
                destination = root / "mismatch.zip"
                with self.assertRaisesRegex(FFmpegDownloadError, "SHA-256 mismatch"):
                    download_approved_ffmpeg(source_url, destination, "0" * 64)
                self.assertFalse(destination.exists())

    def test_ffmpeg_workflow_verifies_archive_before_provision_and_validation(self):
        workflow_path = APP_ROOT.parent / ".github" / "workflows" / "build-windows.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        download = workflow.index("python packaging/download_ffmpeg.py")
        provision = workflow.index("python packaging/provision_ffmpeg.py")
        validation = workflow.index("python packaging/validate_ffmpeg.py $candidate")
        self.assertLess(download, provision)
        self.assertLess(provision, validation)
        self.assertIn("packaging/ffmpeg_source.json", workflow[download:provision])

    def test_build_requires_ffmpeg_and_validates_frozen_assets(self):
        build = (APP_ROOT / "packaging" / "build_windows.bat").read_text(
            encoding="utf-8"
        )
        spec = (APP_ROOT / "packaging" / "phone_controller.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn("exit /b 1", build)
        self.assertIn("validate_ffmpeg.py", build)
        self.assertIn("validate_windows_bundle.py", build)
        self.assertIn('binaries.append((str(ffmpeg), "tools"))', spec)

    def test_protected_build_compiles_all_behavior_modules(self):
        plan = _protected.build_plan()
        compiled = set(plan["compiled_modules"])
        bootstrap = set(plan["bootstrap_modules"])

        for module in (
            "app",
            "camera_manager",
            "diagnostic_logging",
            "automation.screen.camera_ocr",
            "automation.jobs.api",
            "automation.integrations.withdraw_v2.manager",
            "hardware.camera_identity",
            "hardware.camera_diagnostics",
            "hardware.raspberry_pi_input",
        ):
            self.assertIn(module, compiled)
        self.assertEqual(compiled & bootstrap, set())
        self.assertEqual(compiled & set(plan["hiddenimports"]), set())
        self.assertEqual(
            set(plan["excluded_modules"]),
            {
                "automation.screen.benchmark_paddle",
                "automation.screen.benchmark_preview_contention",
                "macos_camera_discovery",
            },
        )
        self.assertEqual(
            bootstrap,
            {
                "windows_launcher",
                "automation",
                "automation.input",
                "automation.integrations",
                "automation.integrations.withdraw_v2",
                "automation.jobs",
                "automation.screen",
                "automation.scripts",
                "automation.workflows",
                "hardware",
            },
        )

    def test_pyinstaller_requires_protected_stage_without_fallback(self):
        build = (APP_ROOT / "packaging" / "build_windows.bat").read_text(
            encoding="utf-8"
        )
        spec = (APP_ROOT / "packaging" / "phone_controller.spec").read_text(
            encoding="utf-8"
        )
        validator = (APP_ROOT / "packaging" / "validate_windows_bundle.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            build.index("build_protected_modules.py"),
            build.index("python -m PyInstaller"),
        )
        self.assertIn("ordinary source/bytecode packaging is disabled", spec)
        self.assertIn("pathex=[str(protected_stage)]", spec)
        self.assertIn("unexpectedly fell back to Python bytecode", spec)
        self.assertIn("unexpectedly entered the PYZ archive", spec)
        self.assertIn("protected_analysis_excludes", spec)
        self.assertIn("extension_sha256", spec)
        self.assertIn("CArchiveReader", validator)
        self.assertIn("--package-smoke-test", validator)
        self.assertIn("protected-attestation.json", validator)
        self.assertIn("clean_protected_build.py", build)
        self.assertIn("PYINSTALLER_CONFIG_DIR", build)
        self.assertIn('environment["CAMERA_OCR_LANGUAGE"] = "zh-HK"', validator)
        launcher = (APP_ROOT / "windows_launcher.py").read_text(encoding="utf-8")
        self.assertIn('report["ocr_worker_status"]', launcher)
        self.assertIn('report["ocr_language_statuses"]', launcher)
        self.assertIn('("en", "zh-HK", "zh-TW")', launcher)
        self.assertIn("start_worker()", launcher)

    def test_writable_data_is_rejected_inside_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary) / "bundle"
            resources.mkdir()
            with (
                patch("windows_runtime.resource_root", return_value=resources),
                self.assertRaisesRegex(RuntimeError, "cannot be stored inside"),
            ):
                RuntimePaths.create(resources / "writable")

    def test_spec_uses_curated_seeds_and_excludes_live_runtime_data(self):
        root = APP_ROOT
        spec = (root / "packaging" / "phone_controller.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn('"packaging" / "workflow_seeds"', spec)
        self.assertNotIn('"automation" / "workflow_data"', spec)
        self.assertNotIn("automation/job_data", spec)
        self.assertNotIn("client.env", spec)

    def test_launcher_redirects_model_cache_to_writable_cache(self):
        root = APP_ROOT
        launcher = (root / "windows_launcher.py").read_text(encoding="utf-8")
        self.assertIn('"PADDLE_PDX_CACHE_HOME"', launcher)
        self.assertIn('paths.cache / "paddlex"', launcher)

    def test_shutdown_command_is_available_without_starting_server(self):
        from windows_launcher import _parse_args

        args = _parse_args(["--shutdown"])
        self.assertTrue(args.shutdown)

        smoke = _parse_args(["--package-smoke-test"])
        self.assertTrue(smoke.package_smoke_test)

        diagnostics = _parse_args(["--camera-diagnostics"])
        self.assertEqual(diagnostics.camera_diagnostics, "")

        diagnostics_path = _parse_args(
            ["--camera-diagnostics", "C:/Temp/camera-report.json"]
        )
        self.assertEqual(
            diagnostics_path.camera_diagnostics,
            "C:/Temp/camera-report.json",
        )

    def test_camera_diagnostics_writes_utf8_without_starting_application(self):
        from windows_launcher import _run_camera_diagnostics

        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "camera-report.json"
            report = {
                "schema_version": 1,
                "code_pages": {"ansi": 950, "ansi_encoding": "cp950"},
                "directshow_devices": [{"name": "測試攝影機"}],
                "mapped_identities": [],
                "errors": [],
            }
            with (
                patch(
                    "hardware.camera_diagnostics.collect_windows_camera_diagnostics",
                    return_value=report,
                ),
                patch("windows_launcher._message_box") as message_box,
            ):
                self.assertEqual(_run_camera_diagnostics(report_path), 0)

            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8")), report
            )
            message_box.assert_called_once()


if __name__ == "__main__":
    unittest.main()
