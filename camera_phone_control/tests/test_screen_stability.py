import json
import threading
import time
import tempfile
import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from calibration import Calibration
from camera import Camera
from automation.context import Cancelled
from automation.screen.camera import CalibratedCamera
from automation.screen.state import (
    ScreenStateProvider,
    TextDetectionUnavailable,
    UnavailableScreenStateProvider,
)
from automation.screen.stability import ScreenStabilityError, wait_until_stable
from automation.workflows.models import new_workflow
from automation.workflows.validation import validate_workflow
from automation.workflows.runner import WorkflowRunner
from automation.workflows.recorder import Recorder
from automation.workflows.storage import WorkflowStorage
from test_control import authenticate_admin


def workflow(**fields):
    result = new_workflow('Stable')
    result['steps'] = [dict(id='stable', type='wait_until_stable', stable_ms=700,
                            timeout_seconds=10) | fields]
    return result


class Simulation:
    def __init__(self, value=lambda t: 40, frozen=False):
        self.now = 0.
        self.value, self.frozen = value, frozen
        self.context = SimpleNamespace(cancel_event=SimpleNamespace(wait=self.advance),
                                       check_cancelled=Mock())

    def advance(self, seconds):
        self.now += seconds

    def session(self):
        return self

    def capture(self):
        t = 0 if self.frozen else self.now
        return np.full((40, 30), self.value(t), np.uint8), round(t * 1000), t

    def run(self, timeout=10):
        wait_until_stable(self, self.context, 700, timeout, clock=lambda: self.now)


class StabilityTests(unittest.TestCase):
    def test_validation(self):
        valid = workflow()
        self.assertEqual(validate_workflow(valid), valid)
        for field, values in [('stable_ms', [None, True, '700', 99, 10001, float('nan')]),
                              ('timeout_seconds', [None, False, '10', 0, 301, float('inf')])]:
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    validate_workflow(workflow(**{field: value}))
        with self.assertRaisesRegex(ValueError, 'stability window'):
            validate_workflow(workflow(stable_ms=2000, timeout_seconds=1))
        validate_workflow(workflow(stable_ms=1000, timeout_seconds=1))

    def test_legacy_visual_fields_load_run_once_and_strip_on_save(self):
        legacy = new_workflow("Legacy")
        legacy["visual_references"] = [
            {"id": "visual_old", "type": "gray_png_v1", "image": "old-data"}
        ]
        legacy["steps"] = [{
            "id": "tap", "type": "tap", "x": .5, "y": .5,
            "expect_after": {
                "type": "visual", "reference_id": "visual_old", "timeout_seconds": 10,
            },
        }]
        with tempfile.TemporaryDirectory() as directory:
            storage = WorkflowStorage(Path(directory))
            path = storage.path(legacy["id"])
            path.write_text(json.dumps(legacy))
            loaded = storage.read(legacy["id"])
            self.assertNotIn("visual_references", loaded)
            self.assertNotIn("expect_after", loaded["steps"][0])

            sim = Simulation()
            sim.context.logger = Mock()
            sim.context.input = Mock()
            sim.context.state = SimpleNamespace()
            WorkflowRunner(sim.context, sim).run(loaded)
            sim.context.input.tap.assert_called_once_with(.5, .5)

            storage.save(loaded)
            persisted = json.loads(path.read_text())
            self.assertNotIn("visual_references", persisted)
            self.assertNotIn("expect_after", persisted["steps"][0])

    def test_screen_state_provider_accepts_future_implementations(self):
        from automation.screen.accessibility import AccessibilityScreenStateProvider
        from automation.screen.camera_ocr import CameraOCRScreenStateProvider

        class FakeProvider:
            def contains_text(self, text):
                return text == "Ready"

            def visible_texts(self):
                return ["Ready"]

        provider = FakeProvider()
        self.assertIsInstance(provider, ScreenStateProvider)
        self.assertTrue(provider.contains_text("Ready"))
        self.assertEqual(provider.visible_texts(), ["Ready"])
        # Contract checks only: neither camera capture nor HTTP is performed.
        self.assertIsInstance(CameraOCRScreenStateProvider(Mock()), ScreenStateProvider)
        self.assertIsInstance(
            AccessibilityScreenStateProvider("http://unused.test"), ScreenStateProvider
        )

    def test_stable_and_noise(self):
        for value in [lambda t: 40, lambda t: 40 + round(t * 100) % 3]:
            sim = Simulation(value)
            sim.run()
            self.assertGreaterEqual(sim.now, .7)
            self.assertLess(sim.now, 1)

    def test_changing_then_stable(self):
        sim = Simulation(lambda t: (round(t * 100) % 2) * 200 if t < 2 else 50)
        sim.run()
        self.assertGreaterEqual(sim.now, 2.7)

    def test_timeout_and_gradual_change(self):
        for value in [lambda t: (round(t * 100) % 2) * 200,
                      lambda t: round(t * 14)]:
            sim = Simulation(value)
            with self.assertRaisesRegex(ScreenStabilityError, 'did not settle within 3 seconds'):
                sim.run(3)

    def test_frozen_and_unavailable(self):
        sim = Simulation(frozen=True)
        with self.assertRaisesRegex(ScreenStabilityError, 'stale'):
            sim.run()
        with self.assertRaisesRegex(ScreenStabilityError, 'unavailable'):
            wait_until_stable(None, sim.context, 700, 10)

    def test_duplicate_ids_cannot_finish_and_timestamp_regression_fails(self):
        sim = Simulation()
        original = sim.capture
        sim.capture = lambda: (original()[0], 1, 0)
        with self.assertRaisesRegex(ScreenStabilityError, 'stale'):
            sim.run()
        sim = Simulation()
        original = sim.capture
        sim.capture = lambda: (original()[0], round(sim.now * 1000), 0)
        with self.assertRaisesRegex(ScreenStabilityError, 'timestamps'):
            sim.run()

    def test_long_frame_gap_restarts_window(self):
        sim = Simulation()
        original = sim.advance
        skipped = False
        def advance(seconds):
            nonlocal skipped
            if sim.now > .3 and not skipped:
                skipped = True
                original(.6)
            else:
                original(seconds)
        sim.context.cancel_event.wait = advance
        sim.run()
        self.assertGreaterEqual(sim.now, 1.65)

    def test_cancel(self):
        sim = Simulation()
        sim.context.check_cancelled.side_effect = [None, Cancelled()]
        with self.assertRaises(Cancelled):
            sim.run()

    def test_takeover_interrupts_wait(self):
        sim = Simulation()
        sim.context.check_cancelled.side_effect = [None, Cancelled(takeover=True)]
        with self.assertRaises(Cancelled) as result:
            sim.run()
        self.assertTrue(result.exception.takeover)
        self.assertLess(sim.now, .7)

    def test_camera_disappearing_during_wait_does_not_pass(self):
        sim = Simulation()
        capture = sim.capture
        def disconnect():
            if sim.now >= .3:
                raise ScreenStabilityError("External camera is unavailable.")
            return capture()
        sim.capture = disconnect
        with self.assertRaisesRegex(ScreenStabilityError, 'unavailable'):
            sim.run()

    def test_runner_and_fixed_wait(self):
        sim = Simulation()
        ctx = sim.context
        ctx.logger = Mock()
        ctx.wait = Mock()
        ctx.input = Mock()
        ctx.state = SimpleNamespace()
        runner = WorkflowRunner(ctx, sim)
        from unittest.mock import patch
        with patch('automation.workflows.runner.wait_until_stable') as detector:
            runner.execute_step(workflow()['steps'][0])
            detector.assert_called_once_with(sim, ctx, 700, 10)
        runner.execute_step({'type': 'wait', 'seconds': 1.25})
        ctx.wait.assert_called_once_with(1.25)
        self.assertEqual(ctx.input.mock_calls, [])

    def test_runner_waits_for_text_without_retrying_previous_action(self):
        sim = Simulation()
        ctx = sim.context
        ctx.logger = Mock()
        ctx.input = Mock()
        ctx.state = SimpleNamespace()
        provider = Mock()
        provider.contains_text.return_value = True
        flow = new_workflow("Text")
        flow["steps"] = [
            {"id": "tap", "type": "tap", "x": .5, "y": .5},
            {"id": "text", "type": "wait_for_text", "text": "Transfer",
             "timeout_seconds": 10},
        ]
        WorkflowRunner(ctx, sim, provider).run(flow)
        ctx.input.tap.assert_called_once_with(.5, .5)
        provider.contains_text.assert_called_once_with("Transfer")
        messages = [call.args[0] for call in ctx.logger.info.call_args_list]
        self.assertIn("Waiting for expected text.", messages)
        self.assertIn("Expected text detected.", messages)

    def test_runner_reports_unavailable_text_provider(self):
        sim = Simulation()
        ctx = sim.context
        ctx.logger = Mock()
        ctx.input = Mock()
        ctx.state = SimpleNamespace()
        runner = WorkflowRunner(ctx, sim, UnavailableScreenStateProvider())
        with self.assertRaisesRegex(TextDetectionUnavailable, "not available"):
            runner.execute_step({
                "type": "wait_for_text", "text": "Transfer",
                "timeout_seconds": 10,
            })

    def test_recorder_insert_edit_move_delete_and_legacy(self):
        rec = Recorder()
        legacy = new_workflow('Legacy')
        legacy['steps'] = [{'id': 'wait', 'type': 'wait', 'seconds': 1}]
        validate_workflow(legacy)
        rec.edit('owner', legacy)
        rec.review('owner', 'insert', {'step': workflow()['steps'][0], 'index': 1})
        sid = rec.workflow['steps'][1]['id']
        rec.review('owner', 'fields', {'step_id': sid, 'fields': {'stable_ms': 900, 'timeout_seconds': 3}})
        with self.assertRaises(ValueError):
            rec.review('owner', 'fields', {'step_id': sid, 'fields': {'stable_ms': -1}})
        self.assertEqual(rec.workflow['steps'][1]['stable_ms'], 900)
        rec.review('owner', 'move', {'step_id': sid, 'index': 0})
        self.assertEqual(rec.workflow['steps'][0]['id'], sid)
        rec.review('owner', 'delete', {'step_id': sid})
        self.assertEqual(rec.workflow['steps'], legacy['steps'])

        rec.review('owner', 'insert', {'step': {
            'type': 'wait_for_text', 'text': ' Transfer ', 'timeout_seconds': 10,
        }, 'index': 1})
        text_id = rec.workflow['steps'][1]['id']
        self.assertEqual(rec.workflow['steps'][1]['text'], 'Transfer')
        rec.review('owner', 'fields', {'step_id': text_id, 'fields': {
            'text': 'Confirm', 'timeout_seconds': 3,
        }})
        self.assertEqual(rec.workflow['steps'][1]['text'], 'Confirm')
        rec.review('owner', 'move', {'step_id': text_id, 'index': 0})
        rec.review('owner', 'delete', {'step_id': text_id})
        self.assertEqual(rec.workflow['steps'], legacy['steps'])

    def camera_state(self):
        camera = Camera()
        calibration = Calibration([[.25,.25],[.75,.25],[.75,.75],[.25,.75]])
        return SimpleNamespace(camera=camera, calibration=calibration,
                               signature=(None, 400, 400), lock=threading.RLock())

    def publish(self, state, background, seq):
        image = np.full((400,400,3), background, np.uint8)
        image[100:301,100:301] = 80
        encoded = cv2.imencode('.jpg', image)[1].tobytes()
        state.camera.latest = (encoded, seq, 400, 400)
        state.camera.timestamps.append((seq, seq * .15, 400, 400))

    def test_crop_ignores_background_and_rejects_geometry_changes(self):
        state = self.camera_state()
        session = CalibratedCamera(state).session()
        self.publish(state, 0, 1)
        first, _, _ = session.capture()
        self.publish(state, 255, 2)
        second, seq, stamp = session.capture()
        self.assertLess(np.mean(cv2.absdiff(first, second)), 1)
        self.assertEqual((seq, stamp), (2, .3))
        state.camera.latest = (state.camera.latest[0], 3, 800, 400)
        with self.assertRaisesRegex(ScreenStabilityError, 'resolution changed'):
            session.capture()
        state.calibration = None
        with self.assertRaisesRegex(ScreenStabilityError, 'calibration changed'):
            session.capture()
        with self.assertRaisesRegex(ScreenStabilityError, 'must be calibrated'):
            CalibratedCamera(state).session()

    def test_ocr_crop_keeps_bottom_edge_content(self):
        state = self.camera_state()
        session = CalibratedCamera(state).session()
        image = np.full((400, 400, 3), 255, np.uint8)
        image[100:301, 100:301] = 200
        image[296:301, 120:281] = 0
        encoded = cv2.imencode('.jpg', image)[1].tobytes()
        state.camera.latest = (encoded, 1, 400, 400)
        state.camera.timestamps.append((1, .15, 400, 400))

        crop, _, _ = session.capture_for_ocr(output_width=100, output_height=200)

        self.assertEqual(crop.shape, (196, 96))
        self.assertLess(np.mean(crop[-4:]), 250)

    def test_ocr_crop_preserves_calibrated_screen_aspect(self):
        state = self.camera_state()
        session = CalibratedCamera(state).session()
        self.publish(state, 255, 1)

        crop, _, _ = session.capture_for_ocr(output_height=200)

        # The calibrated region is square in camera pixels, so the OCR image is
        # square apart from the two-pixel interpolation trim on every side.
        self.assertEqual(crop.shape, (196, 196))

    def test_color_capture_and_ocr_share_calibration_but_keep_separate_channels(self):
        state = self.camera_state()
        session = CalibratedCamera(state).session()
        image = np.zeros((400, 400, 3), np.uint8)
        image[:] = (240, 10, 10)
        image[100:301, 100:301] = (20, 100, 220)
        encoded = cv2.imencode(".jpg", image)[1].tobytes()
        state.camera.latest = (encoded, 1, 400, 400)
        state.camera.timestamps.append((1, .15, 400, 400))

        color, color_sequence, _ = session.capture_calibrated_color(
            output_height=200
        )
        grayscale, ocr_sequence, _ = session.capture_for_ocr(output_height=200)

        self.assertEqual(color.shape, (196, 196, 3))
        self.assertEqual(grayscale.shape, (196, 196))
        self.assertEqual((color_sequence, ocr_sequence), (1, 1))
        channel_means = color.mean(axis=(0, 1))
        self.assertGreater(float(channel_means.max() - channel_means.min()), 100)
        self.assertLess(np.max(np.abs(channel_means - [20, 100, 220])), 8)
        self.assertLess(float(grayscale.std()), 3)

    def test_ocr_decode_and_warp_run_after_camera_and_state_locks_release(self):
        class TrackingLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.held = False

            def __enter__(self):
                self.lock.acquire()
                self.held = True

            def __exit__(self, *_):
                self.held = False
                self.lock.release()

        state = self.camera_state()
        state.lock = TrackingLock()
        session = CalibratedCamera(state).session()
        self.publish(state, 255, 1)
        timings = {}
        original_warp = cv2.warpPerspective

        def checked_warp(*args, **kwargs):
            self.assertFalse(state.lock.held)
            self.assertFalse(state.camera.lock.locked())
            return original_warp(*args, **kwargs)

        with patch(
            "automation.screen.camera.cv2.warpPerspective",
            side_effect=checked_warp,
        ):
            session.capture_for_ocr(output_height=200, timings=timings)

        self.assertTrue(
            {"capture", "decode", "warp"}.issubset(timings)
        )
        self.assertEqual(
            (timings["camera_frame_width"], timings["camera_frame_height"]),
            (400, 400),
        )
        self.assertEqual(
            (timings["warped_width"], timings["warped_height"]),
            (200, 200),
        )
        self.assertEqual(
            (timings["calibrated_width"], timings["calibrated_height"]),
            (196, 196),
        )

    def test_missing_and_closed_camera(self):
        state = self.camera_state()
        session = CalibratedCamera(state).session()
        with self.assertRaisesRegex(ScreenStabilityError, 'unavailable'):
            session.capture()

    def test_ocr_snapshot_rejects_stale_active_camera_frame(self):
        state = self.camera_state()
        session = CalibratedCamera(state).session()
        self.publish(state, 80, 1)
        state.camera.thread = Mock()
        state.camera.frame_stale_seconds = 0.1
        jpeg, sequence, width, height = state.camera.latest
        state.camera.observation = Mock(return_value=(
            jpeg, sequence, width, height, time.monotonic() - 1,
        ))

        with self.assertRaisesRegex(ScreenStabilityError, 'stale'):
            session.capture_for_ocr()
        state.camera.thread = None
        state.camera.latest = None
        state.camera.observation = Mock(return_value=None)
        with self.assertRaisesRegex(ScreenStabilityError, 'unavailable'):
            session.capture()

    def test_display_change_is_detected_and_invalid_jpeg_fails(self):
        state = self.camera_state()
        session = CalibratedCamera(state).session()
        self.publish(state, 0, 1)
        first, _, _ = session.capture()
        jpeg = cv2.imencode('.jpg', np.full((400, 400, 3), 160, np.uint8))[1].tobytes()
        state.camera.latest = (jpeg, 2, 400, 400)
        state.camera.timestamps.append((2, .3, 400, 400))
        second, _, _ = session.capture()
        self.assertGreater(np.mean(cv2.absdiff(first, second)), 3)
        state.camera.latest = (b'not a jpeg', 3, 400, 400)
        state.camera.timestamps.append((3, .45, 400, 400))
        with self.assertRaisesRegex(ScreenStabilityError, 'could not be decoded'):
            session.capture()


class StabilityIntegrationTests(unittest.TestCase):
    def test_api_pi_playback_and_review_use_external_camera_only(self):
        from android_control import AndroidControl
        from app import create_app
        from device_lock import DeviceLocks
        from automation.workflows.storage import WorkflowStorage
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, PHONE_INPUT_BACKEND='raspberry_pi',
            PHONE_MANUAL_INPUT_BACKEND='appium', PI_API_BASE_URL='http://pi.test:8766'
        ), patch('hardware.pi_api_client.http.client.HTTPConnection') as http:
            android = Mock(spec=AndroidControl)
            camera = Mock()
            jpeg = cv2.imencode('.jpg', np.full((400,400,3), 80, np.uint8))[1].tobytes()
            sequence = 0
            def observation():
                nonlocal sequence
                sequence += 1
                return jpeg, sequence, 400, 400, time.monotonic()
            camera.observation.side_effect = observation
            storage = WorkflowStorage(Path(directory) / 'flows')
            app = create_app(camera, android, DeviceLocks(Path(directory) / 'locks'),
                             storage, manual_backend='appium')
            state = app.extensions['control_state']
            state.calibration = Calibration([[0,0],[1,0],[1,1],[0,1]])
            state.signature = (None, 400, 400)
            client = app.test_client()
            authenticate_admin(client)
            headers = {'X-Control-Token': app.config['CONTROL_TOKEN'],
                       'X-Control-Client': 'stability-test'}
            def post(url, data):
                result = client.post(url, json=data, headers=headers)
                self.assertLess(result.status_code, 300, result.json)
                return result.json
            saved = storage.save(workflow(stable_ms=100, timeout_seconds=2), create=True)
            post('/api/recorder/edit', {'workflow_id': saved['id']})
            edited = post('/api/recorder/review', {'operation': 'fields', 'step_id': 'stable',
                          'fields': {'stable_ms': 150}})
            self.assertEqual(edited['workflow']['steps'][0]['stable_ms'], 150)
            post('/api/recorder/save', {})
            service = app.extensions['workflow_service']
            def run():
                result = post(f"/api/workflows/{saved['id']}/run", {})
                service.runs[result['run_id']]['thread'].join(3)
                return service.snapshot(result['run_id'])
            result = run()
            self.assertEqual(result['status'], 'passed', result)
            self.assertEqual(result['preview']['stable_ms'], 150)
            self.assertIn('Screen stable for 150 ms.', result['logs'])
            camera.observation.side_effect = None
            camera.observation.return_value = (jpeg, sequence, 400, 400, time.monotonic() - 3)
            result = run()
            self.assertEqual(result['status'], 'failed')
            self.assertIn('stale', result['message'])
            camera.observation.side_effect = observation
            started = post(f"/api/workflows/{saved['id']}/run", {})
            service.cancel(started['run_id'])
            service.runs[started['run_id']]['thread'].join(1)
            self.assertEqual(service.snapshot(started['run_id'])['status'], 'cancelled')

            state.calibration = None
            result = run()
            self.assertEqual(result['status'], 'failed')
            self.assertIn('must be calibrated', result['message'])
            http.assert_not_called()
            self.assertEqual(android.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
