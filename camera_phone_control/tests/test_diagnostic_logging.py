"""Security, retention, correlation, and lifecycle logging tests."""

import logging
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from diagnostic_logging import (
    EVENT_DEDUPLICATOR,
    PRIMARY_BACKUP_COUNT,
    PRIMARY_LOG_NAME,
    PRIMARY_MAX_BYTES,
    RedactionFilter,
    STARTUP_BACKUP_COUNT,
    STARTUP_MAX_BYTES,
    EventDeduplicator,
    application_version,
    cleanup_old_rotated_logs,
    configure_production_logging,
    log_event,
    register_sensitive_environment,
    sanitize_text,
    sanitize_url,
    write_startup_error,
)


def _child_log_producer(log_queue):
    from diagnostic_logging import configure_child_logging, log_event

    configure_child_logging(log_queue)
    log_event(
        logging.getLogger("child-test"),
        logging.ERROR,
        "ocr",
        "child_process_failed",
        device_id="phone_child",
        exception_type="SyntheticChildFailure",
        error_code="child_process_failed",
    )


def _ocr_protocol_worker(connection, config):
    import os

    from diagnostic_logging import configure_child_logging

    configure_child_logging(config.get("_diagnostic_log_queue"))
    connection.send(("ready", os.getpid(), 0.001))
    message = connection.recv()
    if message[0] == "ocr":
        connection.send(
            (
                "result",
                message[1],
                [],
                0.001,
                os.getpid(),
                {"cpu_user": 0, "cpu_system": 0, "native_threads": 1},
            )
        )
    if connection.recv()[0] == "shutdown":
        connection.close()


class DiagnosticLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _contents(self, path=None):
        path = path or self.root / PRIMARY_LOG_NAME
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return path.read_text(encoding="utf-8")
            time.sleep(0.01)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_sanitizer_removes_authentication_and_url_secrets(self):
        raw = (
            "password=hunter2 otp=123456 access_token=access-secret "
            "refresh_token=refresh-secret Authorization: Bearer bearer-secret "
            "Cookie: session=session-secret "
            "account=1234567890 "
            "url=https://alice:password@example.test/private?q=token#fragment "
            "path=C:\\Users\\client-name\\Phone Controller\\data.json"
        )
        sanitized = sanitize_text(raw)
        for forbidden in (
            "hunter2",
            "123456",
            "access-secret",
            "refresh-secret",
            "bearer-secret",
            "session-secret",
            "alice",
            "password@example",
            "q=token",
            "fragment",
            "1234567890",
            "client-name",
        ):
            self.assertNotIn(forbidden, sanitized)
        self.assertIn("******7890", sanitized)
        self.assertEqual(
            sanitize_url("https://user:pass@example.test:8443/path?q=1#part"),
            "https://example.test:8443/path",
        )

    def test_developer_mode_key_environment_value_is_redacted(self):
        developer_key = "developer-secret-" + "x" * 32
        register_sensitive_environment(
            {"PHONE_DEVELOPER_MODE_KEY": developer_key}
        )

        record = logging.LogRecord(
            "developer-key-test",
            logging.INFO,
            __file__,
            0,
            f"developer URL /?key={developer_key}",
            (),
            None,
        )
        RedactionFilter().filter(record)
        sanitized = record.getMessage()

        self.assertNotIn(developer_key, sanitized)
        self.assertIn("<redacted>", sanitized)

    def test_developer_mode_query_redacts_a_short_configured_value(self):
        sanitized = sanitize_text("developer URL /?key=x")

        self.assertNotIn("?key=x", sanitized)
        self.assertIn("?key=<redacted>", sanitized)

    def test_persistent_handler_redacts_sensitive_structured_fields(self):
        runtime = configure_production_logging(
            self.root, version="9.8.7-test", packaged=True
        )
        try:
            log_event(
                logging.getLogger("security-test"),
                logging.ERROR,
                "payment",
                "payment_readiness_failed",
                device_id="phone_A",
                password="secret-password",
                otp="654321",
                authorization="Bearer abc.def.ghi",
                session_cookie="session-value",
                payment_payload={"account": "1234567890", "amount": "88.12"},
                balance="1234.56",
                expected_text="Sensitive OCR expected text",
                ocr_text="Captured banking text",
            )
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()
        contents = self._contents()
        for forbidden in (
            "secret-password",
            "654321",
            "abc.def.ghi",
            "session-value",
            "1234567890",
            "88.12",
            "1234.56",
            "Sensitive OCR expected text",
            "Captured banking text",
        ):
            self.assertNotIn(forbidden, contents)
        self.assertIn("event=payment_readiness_failed", contents)
        self.assertIn("device_id=phone_A", contents)
        self.assertIn("app_version=9.8.7-test", contents)
        self.assertRegex(contents, r"session_id=[0-9a-f]{32}")

    def test_recent_diagnostics_also_redact_structured_sensitive_fields(self):
        from automation.diagnostics import RecentLogHandler

        handler = RecentLogHandler()
        record = logging.LogRecord(
            "security-test",
            logging.ERROR,
            __file__,
            1,
            "Payment processing failed.",
            (),
            None,
        )
        record.diagnostic_fields = {
            "payment_payload": {"amount": "77.42", "account": "1234567890"},
            "balance": "998.10",
            "ocr_text": "captured banking text",
        }
        handler.handle(record)
        displayed = str(handler.recent())
        for forbidden in (
            "77.42",
            "1234567890",
            "998.10",
            "captured banking text",
        ):
            self.assertNotIn(forbidden, displayed)

    def test_rotation_constants_and_retention_cleanup(self):
        self.assertEqual(PRIMARY_MAX_BYTES, 10 * 1024 * 1024)
        self.assertEqual(PRIMARY_BACKUP_COUNT, 10)
        active = self.root / PRIMARY_LOG_NAME
        old = self.root / (PRIMARY_LOG_NAME + ".1")
        recent = self.root / (PRIMARY_LOG_NAME + ".2")
        active.write_text("active", encoding="utf-8")
        old.write_text("old", encoding="utf-8")
        recent.write_text("recent", encoding="utf-8")
        now = time.time()
        stale = now - 15 * 86400
        os.utime(active, (stale, stale))
        os.utime(old, (stale, stale))
        cleanup_old_rotated_logs(self.root, now=now)
        self.assertTrue(active.exists())
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

        runtime = configure_production_logging(self.root, version="rotation-test")
        try:
            handler = runtime.listener.handlers[0]
            self.assertEqual(handler.maxBytes, 10 * 1024 * 1024)
            self.assertEqual(handler.backupCount, 10)
            self.assertEqual(Path(handler.baseFilename).parent, self.root)
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()

    def test_cleanup_failure_does_not_escape(self):
        with patch("diagnostic_logging.Path.glob", side_effect=OSError("denied")):
            cleanup_old_rotated_logs(self.root)

    def test_packaged_version_marker_is_used(self):
        marker = self.root / "phone-controller-version.txt"
        marker.write_text("4.5.6-test\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch(
            "diagnostic_logging.sys.frozen", True, create=True
        ), patch("diagnostic_logging.sys._MEIPASS", str(self.root), create=True):
            self.assertEqual(application_version(), "4.5.6-test")

    def test_startup_error_is_bounded_and_redacted(self):
        write_startup_error(
            self.root,
            "password=do-not-store Authorization: Bearer abcdef account=1234567890",
            exception_type="RuntimeError",
        )
        contents = self._contents(self.root / "startup-error.log")
        self.assertNotIn("do-not-store", contents)
        self.assertNotIn("abcdef", contents)
        self.assertNotIn("1234567890", contents)
        self.assertIn("event=app_start_failed", contents)
        self.assertEqual(STARTUP_MAX_BYTES, 1024 * 1024)
        self.assertEqual(STARTUP_BACKUP_COUNT, 2)

    def test_deduplication_is_scoped_by_device(self):
        deduplicator = EventDeduplicator(interval_seconds=60)
        self.assertTrue(deduplicator.should_log("pi", "pi_unreachable", ("phone_A",)))
        self.assertFalse(deduplicator.should_log("pi", "pi_unreachable", ("phone_A",)))
        self.assertTrue(deduplicator.should_log("pi", "pi_unreachable", ("phone_B",)))

    def test_unhandled_thread_failure_is_persisted(self):
        runtime = configure_production_logging(self.root, version="thread-test")
        try:

            def fail():
                raise RuntimeError("password=must-not-appear")

            thread = threading.Thread(target=fail, name="test-worker")
            thread.start()
            thread.join()
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()
        contents = self._contents()
        self.assertIn("event=background_thread_failed", contents)
        self.assertIn("thread_name=test-worker", contents)
        self.assertIn("exception_type=RuntimeError", contents)
        self.assertNotIn("must-not-appear", contents)

    def test_exception_traceback_and_arbitrary_detail_are_not_persisted(self):
        runtime = configure_production_logging(self.root, version="exception-test")
        try:
            try:
                raise RuntimeError("opaque-sensitive-freeform-value")
            except RuntimeError:
                logging.getLogger("exception-test").exception(
                    "A background operation failed."
                )
        finally:
            runtime.stop()
        contents = self._contents()
        self.assertIn("exception_type=RuntimeError", contents)
        self.assertIn("A background operation failed.", contents)
        self.assertNotIn("opaque-sensitive-freeform-value", contents)
        self.assertNotIn("Traceback", contents)

    def test_spawned_child_feeds_the_single_main_log_writer(self):
        runtime = configure_production_logging(self.root, version="child-test")
        try:
            process = multiprocessing.get_context("spawn").Process(
                target=_child_log_producer,
                args=(runtime.queue,),
            )
            process.start()
            process.join(3)
            self.assertEqual(process.exitcode, 0)
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()
        contents = self._contents()
        self.assertIn("event=child_process_failed", contents)
        self.assertIn("device_id=phone_child", contents)
        self.assertEqual(list(self.root.glob("*.log")), [self.root / PRIMARY_LOG_NAME])

    def test_ocr_spawn_receives_main_queue_without_standalone_log(self):
        import numpy as np

        from automation.screen.camera_ocr import _PaddleWorkerClient

        runtime = configure_production_logging(self.root, version="ocr-child-test")
        try:
            client = _PaddleWorkerClient(
                {
                    "language": "en",
                    "textline_orientation": False,
                    "cpu_threads": 1,
                },
                2,
                target=_ocr_protocol_worker,
            )
            client.request(np.zeros((2, 2), dtype=np.uint8))
            client.close()
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()
        contents = self._contents()
        self.assertIn("event=ocr_worker_ready", contents)
        self.assertEqual(list(self.root.glob("*.log")), [self.root / PRIMARY_LOG_NAME])

    def test_pi_timeout_and_recovery_use_persistent_events_not_print(self):
        import json
        import socket

        from hardware.pi_api_client import PiApiClient, PiApiError

        class Response:
            status = 200

            @staticmethod
            def read(_size):
                return json.dumps({"ok": True}).encode()

        class Connection:
            def __init__(self, fail):
                self.fail = fail
                self.sock = Mock()

            def connect(self):
                if self.fail:
                    raise socket.timeout()

            def request(self, *_args, **_kwargs):
                return None

            @staticmethod
            def getresponse():
                return Response()

            @staticmethod
            def close():
                return None

        connections = iter((Connection(True), Connection(False)))
        runtime = configure_production_logging(self.root, version="pi-test")
        try:
            client = PiApiClient(
                "http://pi.test:8766",
                connect_timeout=0.01,
                read_timeout=0.01,
                device_id="phone_logging_test",
            )
            with patch(
                "hardware.pi_api_client.http.client.HTTPConnection",
                side_effect=lambda *_args, **_kwargs: next(connections),
            ), patch("builtins.print") as output:
                with self.assertRaises(PiApiError):
                    client.health()
                self.assertEqual(client.health(), {"ok": True})
                output.assert_not_called()
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()
        contents = self._contents()
        self.assertIn("event=pi_request_timeout", contents)
        self.assertIn("event=pi_recovered", contents)
        self.assertIn("device_id=phone_logging_test", contents)

    def test_camera_failure_recovery_transitions_are_persisted_once(self):
        import numpy as np

        from camera import Camera

        raw = np.zeros((20, 30, 3), dtype=np.uint8)
        results = [(True, raw), (False, None), (False, None), (True, raw)]

        def read():
            ok, frame = results.pop(0) if results else (True, raw)
            return ok, None if frame is None else frame.copy()

        capture = Mock()
        capture.isOpened.return_value = True
        capture.read.side_effect = read
        capture.getBackendName.return_value = "test-camera"
        capture.get.return_value = 0
        capture.set.return_value = True
        runtime = configure_production_logging(self.root, version="camera-test")
        try:
            with patch.object(
                Camera, "_should_use_capture_subprocess", return_value=False
            ):
                camera = Camera(
                    source=0,
                    fps=120,
                    read_failure_grace_seconds=0.2,
                    read_retry_delay_seconds=0.005,
                    avfoundation_modes=(),
                )
                camera.device_id = "phone_camera_test"
                camera.configured_source = "camera-safe-id"
                with patch.object(camera, "_open_capture", return_value=capture):
                    camera.start()
                    deadline = time.monotonic() + 2
                    while camera.sequence < 2 and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertGreaterEqual(camera.sequence, 2)
                    camera.close()
        finally:
            runtime.stop()
        contents = self._contents()
        self.assertEqual(contents.count("event=camera_read_failed"), 1)
        self.assertEqual(contents.count("event=camera_recovery_started"), 1)
        self.assertEqual(contents.count("event=camera_recovered"), 1)
        self.assertIn("device_id=phone_camera_test", contents)

    def test_workflow_summaries_are_durable_and_runlog_remains_available(self):
        from android_control import Device
        from automation.workflows.models import new_workflow
        from automation.workflows.service import WorkflowService
        from device_lock import DeviceLocks

        controller = Mock()
        controller.device.return_value = Device("usb-log-test", 1080, 1920, 0)
        workflow = new_workflow("Diagnostic success")
        workflow["id"] = "diagnostic_success"
        workflow["steps"] = [{"id": "brief_wait", "type": "wait", "seconds": 0.001}]
        runtime = configure_production_logging(self.root, version="workflow-test")
        try:
            with patch.dict(
                os.environ,
                {"PHONE_INPUT_BACKEND": "appium", "PHONE_SCREEN_BACKEND": "appium"},
            ):
                service = WorkflowService(
                    controller,
                    DeviceLocks(self.root / "locks"),
                    device_id="phone_A",
                )
                started = service.start(workflow, {}, job_id="job_123")
                snapshot = service.wait(started["run_id"], timeout=2)
            self.assertEqual(snapshot["status"], "passed")
            self.assertTrue(
                any("Workflow started" in line for line in snapshot["logs"])
            )
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()
        contents = self._contents()
        self.assertIn("event=workflow_started", contents)
        self.assertIn("event=workflow_completed", contents)
        self.assertIn("device_id=phone_A", contents)
        self.assertIn("job_id=job_123", contents)
        self.assertIn("workflow_id=diagnostic_success", contents)
        self.assertIn(f"workflow_run_id={started['run_id']}", contents)

    def test_workflow_ocr_failure_is_correlated_without_expected_text(self):
        from android_control import Device
        from automation.screen.state import ScreenStateError
        from automation.workflows.models import new_workflow
        from automation.workflows.service import WorkflowService
        from device_lock import DeviceLocks

        expected = "Sensitive Account 99887766"
        provider = Mock()

        def fail_ocr(*_args, **_kwargs):
            log_event(
                logging.getLogger("ocr-test"),
                logging.WARNING,
                "ocr",
                "ocr_timeout",
                ocr_request_id="ocr_safe_request",
                error_code="ocr_timeout",
            )
            raise ScreenStateError("OCR request timed out during inference.")

        provider.contains_text.side_effect = fail_ocr
        controller = Mock()
        controller.device.return_value = Device("usb-log-test", 1080, 1920, 0)
        workflow = new_workflow("Diagnostic OCR failure")
        workflow["id"] = "diagnostic_ocr_failure"
        workflow["steps"] = [
            {
                "id": "read_balance",
                "type": "wait_for_text",
                "text": expected,
                "timeout_seconds": 1,
            }
        ]
        runtime = configure_production_logging(self.root, version="workflow-test")
        try:
            with patch.dict(
                os.environ,
                {"PHONE_INPUT_BACKEND": "appium", "PHONE_SCREEN_BACKEND": "appium"},
            ):
                service = WorkflowService(
                    controller,
                    DeviceLocks(self.root / "locks-failure"),
                    screen_state_provider=provider,
                    device_id="phone_A",
                )
                started = service.start(workflow, {}, job_id="job_ocr")
                snapshot = service.wait(started["run_id"], timeout=2)
            self.assertEqual(snapshot["status"], "failed")
            self.assertNotIn(expected, "\n".join(snapshot["logs"]))
        finally:
            runtime.stop()
            logging.getLogger().handlers.clear()
        contents = self._contents()
        self.assertIn("event=step_failed", contents)
        self.assertIn("event=workflow_failed", contents)
        self.assertIn("step_id=read_balance", contents)
        self.assertIn("error_code=ocr_timeout", contents)
        self.assertIn("exception_type=ScreenStateError", contents)
        self.assertNotIn(expected, contents)
        ocr_line = next(
            line
            for line in contents.splitlines()
            if "component=ocr event=ocr_timeout" in line
        )
        for field in (
            "device_id=phone_A",
            "job_id=job_ocr",
            "workflow_id=diagnostic_ocr_failure",
            f"workflow_run_id={started['run_id']}",
            "step_id=read_balance",
            "ocr_request_id=ocr_safe_request",
        ):
            self.assertIn(field, ocr_line)


if __name__ == "__main__":
    unittest.main()
