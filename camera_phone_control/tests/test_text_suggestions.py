"""On-demand expected text never participates in physical action execution."""
from copy import deepcopy
import threading
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from automation.screen.camera_ocr import CameraOCRScreenStateProvider
from automation.screen.state import ScreenStateError
from automation.screen.text_learning import text_suggestions
from automation.workflows.recorder import Recorder
import test_pi_recording
import test_record_first
from test_control import authenticate_admin


class SuggestionsTests(unittest.TestCase):
    def test_filter_deduplicate_and_rank(self):
        result = text_suggestions([
            '', None, 'x', 'E', '------', '123456', '!@ab#$', 'aaaaaaa',
            'a b c d e', 'This is a long paragraph with many more words to read',
            'Pay & Transfer', ' pay  & transfer ', 'Pay&Transfer',
            'New to Maybank?', 'Open your account online', 'Accounts',
            'Transfer money', 'Settings', 'x' * 81,
        ])
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0], 'Accounts')
        self.assertEqual(result.count('Pay & Transfer'), 1)
        self.assertNotIn('This is a long paragraph with many more words to read', result)
        self.assertEqual(text_suggestions(['x', '--', 'Good label']), ['Good label'])

    def test_confidence_filters_garbage_and_preserves_maximum(self):
        result = text_suggestions([
            {"text": "SirelakeBiasa ie‘ule", "confidence": 42.5},
            {"text": "Pay & Transfer", "confidence": 91.4},
            {"text": " pay  & transfer ", "confidence": 88.0},
            {"text": "New to Maybank?", "confidence": 87.2},
            {"text": "Accounts", "confidence": 94.0},
            {"text": "Settings", "confidence": 90.0},
            {"text": "Transfer money", "confidence": 89.0},
            {"text": "More options", "confidence": 80.0},
            {"text": "Invalid confidence", "confidence": float("nan")},
        ])
        self.assertEqual(len(result), 5)
        self.assertNotIn("SirelakeBiasa ie‘ule", result)
        self.assertEqual(result.count("Pay & Transfer"), 1)
        self.assertIn("Accounts", result)

    def test_short_cjk_labels_and_chinese_phrases_are_preserved(self):
        values = ["確認", "取消", "轉賬成功", "可用餘額", "收款人", "转账成功"]
        result = text_suggestions(values)
        self.assertEqual(len(result), 5)
        self.assertTrue(set(values[:5]).issubset(result))

    def test_short_latin_and_mixed_noise_remain_rejected(self):
        self.assertEqual(
            text_suggestions(["OK", "abc", "中1", "-中", "確認"]),
            ["確認"],
        )

    def test_traditional_and_simplified_short_labels_are_allowed(self):
        self.assertEqual(set(text_suggestions(["確認", "确认"])), {"確認", "确认"})


class CameraOCRCandidateTests(unittest.TestCase):
    def setUp(self):
        self.image = np.arange(120, dtype=np.uint8).reshape(12, 10)
        self.results = [{
            "res": {
                "rec_texts": ["Set up online banking", "Log In"],
                "rec_scores": np.array([0.942, 88.0]),
                "rec_boxes": np.array([[0, 30, 100, 50], [0, 5, 50, 20]]),
            }
        }]
        self.worker = Mock()
        self.worker.pid = 4321
        self.worker.request.side_effect = lambda image, **kwargs: (
            self.results,
            {"worker_start": 0.01, "ipc_send": 0.002, "ipc_wait": 0.03,
             "inference": 0.02, "worker_pid": 4321},
        )
        self.worker_class = patch(
            "automation.screen.camera_ocr._PaddleWorkerClient",
            return_value=self.worker,
        ).start()
        self.cache_setting = patch.dict(
            os.environ, {"CAMERA_OCR_CACHE_THRESHOLD": "0"}
        )
        self.cache_setting.start()
        self.addCleanup(patch.stopall)
        session = Mock()
        def capture_for_ocr(*, timings):
            timings.update(capture=0.001, decode=0.002, warp=0.003)
            return self.image, 1, 0.0
        session.capture_for_ocr.side_effect = capture_for_ocr
        self.external_screen = Mock()
        self.external_screen.session.return_value = session
        self.provider = CameraOCRScreenStateProvider(self.external_screen)

    def test_visible_texts_capture_once_and_preserve_screen_order(self):
        self.assertEqual(
            self.provider.visible_texts(),
            ["Log In", "Set up online banking"],
        )
        self.external_screen.session.assert_called_once_with()
        capture = self.external_screen.session.return_value.capture_for_ocr
        capture.assert_called_once()
        self.assertEqual(set(capture.call_args.kwargs), {"timings"})
        self.worker.request.assert_called_once()

    def test_candidates_expose_zero_to_one_hundred_confidence(self):
        self.assertEqual(self.provider.visible_text_candidates(), [
            {
                "text": "Log In",
                "confidence": 88.0,
                "min_confidence": 88.0,
                "box": [0.0, 5.0, 50.0, 20.0],
            },
            {
                "text": "Set up online banking",
                "confidence": 94.19999999999999,
                "min_confidence": 94.19999999999999,
                "box": [0.0, 30.0, 100.0, 50.0],
            },
        ])

    def test_contains_text_is_case_insensitive_and_reuses_passed_image(self):
        self.assertTrue(self.provider.contains_text("LOG in", image=self.image))
        self.assertFalse(self.provider.contains_text("Accounts", image=self.image))
        self.external_screen.session.assert_not_called()
        self.assertEqual(self.worker.request.call_count, 2)

    def test_unicode_chinese_text_is_preserved_and_matched(self):
        self.results[0]["res"].update(
            rec_texts=["转账成功", "可用餘額", "收款人"],
            rec_scores=np.array([0.99, 0.98, 0.97]),
            rec_boxes=np.array(
                [[0, 5, 80, 20], [0, 30, 80, 45], [0, 55, 80, 70]]
            ),
        )

        self.assertEqual(
            self.provider.visible_texts_from_image(self.image),
            ["转账成功", "可用餘額", "收款人"],
        )
        self.assertTrue(self.provider.contains_text("转账成功", image=self.image))
        self.assertTrue(self.provider.contains_text("可用餘額", image=self.image))
        self.assertFalse(self.provider.contains_text("可用余额", image=self.image))

    def test_chinese_target_uses_chinese_worker_and_remains_fail_closed(self):
        chinese = Mock()
        chinese.status.return_value = {
            "pool_size": 1,
            "busy_workers": 0,
            "queued_requests": 0,
        }
        chinese.request.return_value = (
            [{"res": {"rec_texts": ["取消"], "rec_scores": [0.98]}}],
            {"queue_wait": 0, "worker_start": 0, "ipc_send": 0,
             "ipc_wait": 0, "inference": 0, "worker_pid": 222},
        )
        self.provider._language_workers["chinese_cht"] = chinese

        self.assertTrue(self.provider.contains_text("取消", image=self.image))
        chinese.request.assert_called_once()
        self.worker.request.assert_not_called()
        chinese.request.return_value = (
            [{"res": {"rec_texts": ["HON"], "rec_scores": [0.91]}}],
            {"queue_wait": 0, "worker_start": 0, "ipc_send": 0,
             "ipc_wait": 0, "inference": 0, "worker_pid": 222},
        )
        self.provider._cache_by_scope.clear()
        self.assertFalse(self.provider.contains_text("取消", image=self.image))

    def test_english_target_keeps_english_worker(self):
        self.assertTrue(self.provider.contains_text("Log In", image=self.image))
        self.worker.request.assert_called_once()
        self.assertEqual(set(self.provider._language_workers), {"en"})

    def test_change_gate_skips_unchanged_frames_then_checks_settled_change(self):
        base = np.zeros((120, 80), dtype=np.uint8)
        changed = base.copy()
        changed[20:80, 10:70] = 255
        frames = iter([base, base, changed, changed, changed])

        def capture(*, timings):
            timings["capture"] = 0.001
            return next(frames), 1, 0.0

        self.external_screen.session.return_value.capture_for_ocr.side_effect = capture
        self.provider.capture_change_baseline()
        self.assertEqual(
            self.provider.poll_contains_text("Log in"), (False, False)
        )
        self.assertEqual(
            self.provider.poll_contains_text("Log in"), (False, False)
        )
        self.assertEqual(
            self.provider.poll_contains_text("Log in"), (False, False)
        )
        time.sleep(self.provider.settle_seconds + 0.01)
        self.assertEqual(self.provider.poll_contains_text("Log in"), (True, True))
        self.worker.request.assert_called_once()

    def test_low_latency_gate_defaults(self):
        self.assertAlmostEqual(self.provider.forced_ocr_interval, 0.45)
        self.assertAlmostEqual(self.provider.poll_interval, 0.075)
        self.assertAlmostEqual(self.provider.settle_seconds, 0.12)

    def test_forced_poll_checks_unchanged_frame(self):
        base = np.zeros((120, 80), dtype=np.uint8)
        self.external_screen.session.return_value.capture_for_ocr.side_effect = None
        self.external_screen.session.return_value.capture_for_ocr.return_value = (
            base, 1, 0.0
        )
        self.provider.capture_change_baseline()
        self.assertEqual(
            self.provider.poll_contains_text("Log in", force=True), (True, True)
        )
        self.worker.request.assert_called_once()

    def test_roi_first_uses_full_screen_fallback_on_miss(self):
        image = np.zeros((100, 80), dtype=np.uint8)
        self.external_screen.session.return_value.capture_for_ocr.side_effect = None
        self.external_screen.session.return_value.capture_for_ocr.return_value = (
            image, 4, 0.0
        )
        missed = [{"res": {"rec_texts": ["Other"], "rec_scores": [0.9]}}]
        self.provider.roi_fallback_misses = 1
        self.worker.request.side_effect = [
            (missed, {"worker_start": 0, "ipc_send": 0, "ipc_wait": 0,
                      "inference": 0, "worker_pid": 4321}),
            (self.results, {"worker_start": 0, "ipc_send": 0, "ipc_wait": 0,
                            "inference": 0, "worker_pid": 4321}),
        ]
        checked, found = self.provider.poll_contains_text(
            "Log in", roi=[0, 0, 0.5, 1], force=True
        )
        self.assertTrue(checked)
        self.assertTrue(found)
        self.assertEqual(self.worker.request.call_count, 2)
        self.assertEqual(self.worker.request.call_args_list[0].args[0].shape[:2], (100, 40))
        self.assertEqual(self.worker.request.call_args_list[1].args[0].shape[:2], (100, 80))

    def test_roi_misses_fallback_periodically_and_metrics(self):
        from automation.workflows.timing import WorkflowTiming
        self.results = []
        self.provider.roi_fallback_seconds = 300
        timing = WorkflowTiming()
        timing.start()
        timing.begin_step(1, "wait_for_text")
        try:
            for _ in range(7):
                self.assertEqual(self.provider.poll_contains_text(
                    "Missing", roi=[0, 0, .5, 1], force=True), (True, False))
        finally:
            timing.finish()
        sizes = [call.args[0].shape[1] for call in self.worker.request.call_args_list]
        self.assertEqual(sizes, [5, 5, 5, 10, 5, 5, 5, 10, 5])
        summary = timing.snapshot()["summary"]
        self.assertEqual(summary["roi_attempt_count"], 7)
        self.assertEqual(summary["roi_miss_count"], 7)
        self.assertEqual(summary["roi_hit_count"], 0)
        self.assertEqual(summary["full_fallback_count"], 2)
        self.assertGreater(summary["roi_ocr_seconds"], summary["full_ocr_seconds"])
        self.assertAlmostEqual(summary["ocr_wall_seconds"], summary["roi_ocr_seconds"] + summary["full_ocr_seconds"])
        for call in timing.snapshot()["steps"][0]["ocr_calls"]:
            self.assertIn(call["scope"], ("roi", "full"))
            self.assertEqual(call["roi"], dict(x=0, y=0, width=.5, height=1))
            self.assertEqual(call["region_count"], 0)
            self.assertIn("inference_ms", call)
            self.assertIn("queue_wait_ms", call)

    def test_roi_interval_fallback_hit_and_gate(self):
        self.provider.roi_fallback_misses = 100
        wait = self.provider.new_roi_wait()
        self.results = []
        self.provider.poll_contains_text("Log in", roi=[0, 0, .5, 1], roi_state=wait)
        wait["last_fallback"] -= 10
        self.assertEqual(self.provider.poll_contains_text(
            "Log in", roi=[0, 0, .5, 1], roi_state=wait), (False, False))
        self.worker.request.side_effect = lambda image, **kw: (
            [] if image.shape[1] == 5 else [{"rec_texts": ["Log in"], "rec_scores": [.9]}],
            {"inference": .02, "ipc_wait": .03},
        )
        self.assertEqual(self.provider.poll_contains_text(
            "Log in", roi=[0, 0, .5, 1], roi_state=wait, force=True), (True, True))
        self.assertEqual(wait["misses"], 0)

    def test_roi_devices_and_wait_sessions_are_independent(self):
        self.results = []
        other = self.provider.for_screen(self.external_screen)
        self.assertIs(other._worker, self.provider._worker)
        self.assertIsNot(other._cache_by_scope, self.provider._cache_by_scope)
        for _ in range(2):
            self.provider.poll_contains_text("Missing", roi=[0, 0, .5, 1], force=True)
        other.poll_contains_text("Missing", roi=[0, 0, .5, 1], force=True)
        self.assertEqual(self.worker.request.call_count, 3)
        self.provider.poll_contains_text("Missing", roi=[0, 0, .5, 1], force=True,
                                         roi_state=self.provider.new_roi_wait())
        self.assertEqual(self.worker.request.call_count, 4)

    def test_roi_hit_metrics_and_full_screen_metrics(self):
        from automation.workflows.timing import WorkflowTiming
        timing = WorkflowTiming()
        timing.start()
        try:
            timing.begin_step(1, "ROI wait")
            self.assertEqual(self.provider.poll_contains_text(
                "Log in", roi=dict(x=0, y=0, width=.5, height=1), force=True), (True, True))
            timing.end_step()
            timing.begin_step(2, "Legacy wait")
            self.assertEqual(self.provider.poll_contains_text("Log in", force=True), (True, True))
        finally:
            timing.finish()
        steps = timing.snapshot()["steps"]
        self.assertEqual(steps[0]["roi_hit_count"], 1)
        self.assertEqual(steps[0]["full_fallback_count"], 0)
        self.assertEqual(steps[0]["roi_miss_count"], 0)
        self.assertEqual(steps[1]["roi_attempt_count"], 0)
        self.assertEqual(steps[1]["scopes"], ["full"])
        self.assertIsNone(steps[1]["roi"])
        self.assertEqual(steps[1]["ocr_calls"][0]["scope"], "full")

    def test_roi_wait_timeout_and_cancel(self):
        from automation.screen.state import wait_for_text
        from automation.context import Cancelled
        from types import SimpleNamespace
        self.results = []
        now = [0.0]
        def advance(seconds):
            now[0] += seconds
        context = SimpleNamespace(check_cancelled=Mock(), cancel_event=SimpleNamespace(wait=advance))
        with self.assertRaises(ScreenStateError):
            wait_for_text(self.provider, context, "Missing", .6, roi=[0, 0, .5, 1], clock=lambda: now[0])
        context.check_cancelled.side_effect = Cancelled()
        with self.assertRaises(Cancelled):
            wait_for_text(self.provider, context, "Missing", .6, roi=[0, 0, .5, 1], clock=lambda: now[0])

    def test_roi_hit_avoids_full_screen_fallback(self):
        image = np.zeros((100, 80), dtype=np.uint8)
        self.external_screen.session.return_value.capture_for_ocr.side_effect = None
        self.external_screen.session.return_value.capture_for_ocr.return_value = (
            image, 4, 0.0
        )
        checked, found = self.provider.poll_contains_text(
            "Log in", roi=[0, 0, 0.5, 1], force=True
        )
        self.assertTrue(checked)
        self.assertTrue(found)
        self.worker.request.assert_called_once()

    def test_empty_results_return_empty_public_values(self):
        self.results = []
        self.assertEqual(self.provider.visible_text_candidates_from_image(self.image), [])
        self.assertEqual(self.provider.visible_texts_from_image(self.image), [])
        self.assertFalse(self.provider.contains_text("anything", image=self.image))

    def test_provider_uses_persistent_worker_pool_clients(self):
        self.provider.visible_texts_from_image(self.image)
        self.provider.visible_texts_from_image(self.image)
        self.assertEqual(self.worker_class.call_count, 2)
        self.assertEqual(self.worker.request.call_count, 2)

    def test_model_and_inference_are_only_started_on_demand(self):
        self.worker.request.assert_not_called()

        self.provider.capture_snapshot()

        self.worker.request.assert_not_called()
        self.provider.visible_texts_from_image(self.image)
        self.worker.request.assert_called_once()

    def test_capture_finishes_before_inference(self):
        capture_active = threading.Event()

        def capture_for_ocr(*, timings):
            capture_active.set()
            timings.update(capture=0.001, decode=0.002, warp=0.003)
            result = (self.image, 1, 0.0)
            capture_active.clear()
            return result

        def predict(image, **kwargs):
            self.assertFalse(capture_active.is_set())
            return self.results, {
                "worker_start": 0, "ipc_send": 0, "ipc_wait": 0,
                "inference": 0, "worker_pid": 4321,
            }

        self.external_screen.session.return_value.capture_for_ocr.side_effect = (
            capture_for_ocr
        )
        self.worker.request.side_effect = predict

        self.provider.visible_texts()

    def test_dedicated_ocr_lock_serializes_concurrent_inference(self):
        entered = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        active = 0
        maximum = 0

        def predict(image, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            entered.set()
            self.assertTrue(release.wait(2))
            with guard:
                active -= 1
            return self.results, {
                "worker_start": 0, "ipc_send": 0, "ipc_wait": 0,
                "inference": 0, "worker_pid": 4321,
            }

        self.worker.request.side_effect = predict
        threads = [
            threading.Thread(
                target=self.provider.visible_texts_from_image,
                args=(self.image,),
            )
            for _ in range(2)
        ]
        threads[0].start()
        self.assertTrue(entered.wait(1))
        threads[1].start()
        time.sleep(0.03)
        self.assertEqual(self.worker.request.call_count, 1)
        release.set()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.worker.request.call_count, 2)
        self.assertEqual(maximum, 1)

    def test_debug_directory_saves_exact_paddle_input(self):
        seen = []
        self.worker.request.side_effect = lambda image, **kwargs: (
            seen.append(image.copy()) or [],
            {"worker_start": 0, "ipc_send": 0, "ipc_wait": 0,
             "inference": 0, "worker_pid": 4321},
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"CAMERA_OCR_DEBUG_DIR": directory},
        ):
            provider = CameraOCRScreenStateProvider(self.external_screen)
            provider.visible_text_candidates_from_image(self.image)
            files = list(Path(directory).glob("*.png"))
            self.assertEqual(len(files), 1)
            np.testing.assert_array_equal(cv2.imread(str(files[0])), seen[0])

    def test_debug_log_separates_capture_warp_preprocess_and_inference(self):
        self.provider.debug_logging = True

        with self.assertLogs("camera_ocr", level="INFO") as logs:
            self.provider.visible_texts()

        output = " ".join(logs.output)
        for field in (
            "capture=",
            "decode=",
            "warp=",
            "copy=",
            "preprocess=",
            "worker_start=",
            "ipc_send=",
            "ipc_wait=",
            "child_inference=",
            "total=",
        ):
            self.assertIn(field, output)


class RecorderSuggestionTests(unittest.TestCase):
    def test_annotation_state_clears_on_cancel_stop_and_failure(self):
        for operation in ("cancel_expected_text", "stop", "fail_recording"):
            recorder = Recorder()
            recorder.start("owner", "Recording")
            recorder.begin_current_screen_annotation("owner")
            if operation == "cancel_expected_text":
                snapshot = recorder.cancel_expected_text("owner")
            elif operation == "stop":
                snapshot = recorder.stop("owner")
            else:
                recorder.fail_recording()
                snapshot = recorder.snapshot()
            self.assertFalse(snapshot["expected_text_pending"])

    def test_secure_and_manual_steps_reject_current_screen_annotation(self):
        recorder = Recorder()
        recorder.start("owner", "Recording")
        checkpoint = recorder.prepare(
            "owner",
            {"type": "manual_checkpoint", "message": "Enter OTP manually"},
        )
        snapshot = recorder.commit_recording("owner", checkpoint)
        self.assertFalse(snapshot["expected_text_pending"])
        with self.assertRaisesRegex(ValueError, "secure or manual checkpoint"):
            recorder.begin_current_screen_annotation("owner")

    def test_review_insert_change_remove_and_standalone(self):
        r = Recorder()
        r.start('owner', 'Review')
        for step in [dict(type='tap', x=.5, y=.5),
                     dict(type='swipe', start=[.5,.8], end=[.5,.2], duration_ms=300),
                     dict(type='wait_for_text', text='Unrelated', timeout_seconds=5)]:
            r.workflow = r.prepare('owner', step)
        r.stop('owner')
        action = r.workflow['steps'][0]['id']
        unrelated = deepcopy(r.workflow['steps'][-1])
        r.review('owner', 'expected_text', dict(step_id=action, text='  Manual phrase  '))
        self.assertEqual([s['type'] for s in r.workflow['steps']],
                         ['tap', 'wait_for_text', 'swipe', 'wait_for_text'])
        wait = deepcopy(r.workflow['steps'][1])
        self.assertEqual(wait['text'], 'Manual phrase')
        self.assertEqual(wait['timeout_seconds'], 10)
        r.review('owner', 'expected_text', dict(step_id=action, text='Changed', timeout_seconds=3))
        self.assertEqual(r.workflow['steps'][1]['id'], wait['id'])
        self.assertEqual(r.workflow['steps'][1]['timeout_seconds'], 3)
        before = deepcopy(r.workflow)
        with self.assertRaises(ValueError):
            r.review('owner', 'expected_text', dict(step_id=action, text=' ', timeout_seconds=0))
        self.assertEqual(r.workflow, before)
        r.review('owner', 'remove_expected_text', dict(step_id=action))
        r.review('owner', 'remove_expected_text', dict(step_id=action))
        self.assertEqual(len(r.workflow['steps']), 3)
        self.assertEqual(r.workflow['steps'][-1], unrelated)
        r.review('owner', 'fields', dict(step_id=unrelated['id'], fields=dict(text='Edited standalone')))
        self.assertEqual(r.workflow['steps'][-1]['text'], 'Edited standalone')

    def test_sensitive_steps_reject_shortcut(self):
        for step in [dict(type='manual_checkpoint', message='Authorize payment manually'),
                     dict(type='input', source='fixed', value='hello'),
                     dict(type='key', key='ENTER'),
                     dict(type='tap', x=.5, y=.5, label='Confirm payment'),
                     dict(type='tap', x=.5, y=.5, label='Password')]:
            r = Recorder()
            r.start('owner', 'Review')
            r.workflow = r.prepare('owner', step)
            r.stop('owner')
            with self.assertRaises(ValueError):
                r.review('owner', 'expected_text', dict(step_id=r.workflow['steps'][0]['id'], text='Ready'))


class EndpointTests(unittest.TestCase):
    post = test_pi_recording.PiRecordingTests.post
    action = test_pi_recording.PiRecordingTests.action

    def setUp(self):
        provider = patch('automation.workflows.api.CameraOCRScreenStateProvider', autospec=True)
        self.ocr = provider.start().return_value
        self.addCleanup(provider.stop)
        test_pi_recording.PiRecordingTests.setUp(self)
        self.ocr.visible_texts.side_effect = AssertionError('Live input must never read text')
        self.ocr.visible_text_candidates.side_effect = AssertionError(
            'Live input must never read candidates'
        )

    def test_roi_preview_test_uses_saved_calibrated_crop_without_actions(self):
        image = np.arange(8000, dtype=np.uint8).reshape(100, 80)
        self.ocr.capture_snapshot.return_value = image
        self.ocr._crop_roi.side_effect = CameraOCRScreenStateProvider._crop_roi
        isolated = self.ocr.for_screen.return_value
        isolated._recognize_candidates.return_value = [{"text": "Ready"}]
        self.ocr._candidates_contain.return_value = True
        preview = self.post('/api/ocr/roi-preview', {})
        self.assertEqual((preview['width'], preview['height']), (80, 100))
        result = self.post('/api/ocr/roi-test', dict(token=preview['token'],
            roi=dict(x=0, y=.5, width=1, height=.5), text='Ready'))
        self.assertTrue(result['found'])
        np.testing.assert_array_equal(isolated._recognize_candidates.call_args.args[0], image[50:])
        self.ocr.capture_snapshot.assert_called_once()
        self.ocr._recognize_candidates.assert_not_called()
        self.pi.tap.assert_not_called()
        self.post('/api/ocr/roi-test', dict(token='expired',
            roi=[0, 0, 1, 1], text='Ready'), status=400)
        self.post('/api/ocr/roi-test', dict(token=preview['token'],
            roi=[0, 0, 0, 1], text='Ready'), status=400)

    def test_roi_test_passes_chinese_and_english_targets_to_language_routing(self):
        image = np.arange(8000, dtype=np.uint8).reshape(100, 80)
        self.ocr.capture_snapshot.return_value = image
        self.ocr._crop_roi.side_effect = CameraOCRScreenStateProvider._crop_roi
        isolated = self.ocr.for_screen.return_value
        preview = self.post('/api/ocr/roi-preview', {})
        for target in ("取消", "確認", "轉賬成功", "Cancel"):
            isolated._recognize_candidates.return_value = [
                {"text": target, "confidence": 98.0}
            ]
            self.ocr._candidates_contain.return_value = True
            result = self.post('/api/ocr/roi-test', {
                'token': preview['token'],
                'roi': [0, 0, 1, 1],
                'text': target,
            })
            self.assertTrue(result['found'])
            self.assertEqual(
                isolated._recognize_candidates.call_args.kwargs['expected'],
                target.casefold(),
            )

    def test_currency_roi_test_reuses_preview_crop_and_rejects_ambiguous_values(self):
        image = np.arange(8000, dtype=np.uint8).reshape(100, 80)
        self.ocr.capture_snapshot.return_value = image
        self.ocr._crop_roi.side_effect = CameraOCRScreenStateProvider._crop_roi
        self.ocr._candidates_contain.return_value = True
        isolated = self.ocr.for_screen.return_value
        isolated._recognize_candidates.return_value = [{"text": "RM 8,230.50"}]
        preview = self.post('/api/ocr/roi-preview', {})
        payload = dict(token=preview['token'], parser='currency',
                       roi=dict(x=.25, y=.5, width=.5, height=.25))
        result = self.post('/api/ocr/roi-test', payload)
        self.assertEqual(result['value'], '8230.50')
        np.testing.assert_array_equal(
            isolated._recognize_candidates.call_args.args[0], image[50:75, 20:60]
        )
        isolated._recognize_candidates.return_value = [
            {"text": "RM 8,230.50"}, {"text": "RM 99.00"},
        ]
        self.post('/api/ocr/roi-test', payload, status=400)
        self.ocr.capture_snapshot.assert_called_once()
        self.ocr._recognize_candidates.assert_not_called()
        self.pi.tap.assert_not_called()

    def test_manual_tap_swipe_key_and_heartbeat_do_not_read_screen(self):
        self.action('tap', x=.5, y=.5)
        self.action('swipe_up')
        self.action('back')
        self.post('/api/heartbeat', {'frame_id':123})
        self.ocr.visible_texts.assert_not_called()
        self.ocr.visible_text_candidates.assert_not_called()
        # Recorder screenshots capture the calibrated frame for authoring, but
        # must not invoke OCR recognition during physical actions.
        self.assertEqual(self.ocr.capture_snapshot.call_count, 3)
        self.pi.tap.assert_called_once()
        self.pi.swipe.assert_called_once()
        self.pi.back.assert_called_once()
        self.assertEqual(len(self.recorder.workflow['steps']), 3)

    def test_debug_ocr_endpoint_keeps_existing_response_shape(self):
        self.ocr.visible_texts.side_effect = None
        self.ocr.visible_texts.return_value = ['Log In', 'Accounts']

        response = self.client.get('/api/ocr/texts')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'texts': ['Log In', 'Accounts'],
            'count': 2,
        })
        self.ocr.visible_texts.assert_called_once_with()

    def test_debug_ocr_endpoint_returns_worker_failure_as_json(self):
        self.ocr.visible_texts.side_effect = ScreenStateError(
            "PaddleOCR worker stopped unexpectedly."
        )
        response = self.client.get('/api/ocr/texts')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {
            'error': 'PaddleOCR worker stopped unexpectedly.',
            'error_code': 'workflow_screen_failed',
            'error_params': {},
        })

    def test_endpoint_ownership_errors_and_stopped_draft(self):
        self.action('tap', x=.5, y=.5)
        data = dict(step_id=self.recorder.workflow['steps'][0]['id'])
        before = deepcopy(self.recorder.workflow)
        self.post('/api/recorder/text-suggestions', data, 503)
        self.assertEqual(self.recorder.workflow, before)
        self.ocr.visible_text_candidates.side_effect = None
        self.ocr.visible_text_candidates.return_value = [
            {"text": value, "confidence": 90}
            for value in ['Accounts', 'Settings', 'Transfer money', 'Pay & Transfer',
                          'New to Maybank?', 'More options', 'x', '----']
        ]
        for stopped in (False, True):
            if stopped:
                self.post('/api/recorder/stop', {})
            result = self.post('/api/recorder/text-suggestions', data)
            self.assertEqual(len(result['suggestions']), 5)
            self.assertNotIn('x', result['suggestions'])
        self.ocr.visible_text_candidates.reset_mock()
        self.post('/api/recorder/text-suggestions', data, 400,
                  headers={**self.headers, 'X-Recorder-Client':'other-browser'})
        self.post('/api/recorder/text-suggestions', data, 403,
                  headers={**self.headers, 'X-Control-Token':'invalid'})
        self.ocr.visible_text_candidates.assert_not_called()
        self.post('/api/recorder/cancel', {})
        self.post('/api/recorder/text-suggestions', data, 400)

    def test_endpoint_falls_back_to_visible_texts_without_candidate_api(self):
        self.action('tap', x=.5, y=.5)
        data = dict(step_id=self.recorder.workflow['steps'][0]['id'])
        del self.ocr.visible_text_candidates
        self.ocr.visible_texts.side_effect = None
        self.ocr.visible_texts.return_value = ['Accounts', 'Settings']

        result = self.post('/api/recorder/text-suggestions', data)

        self.assertEqual(result['suggestions'], ['Accounts', 'Settings'])
        self.ocr.visible_texts.assert_called_once_with()

    def test_slow_ocr_does_not_hold_heartbeat_or_input_lock(self):
        self.action('tap', x=.5, y=.5)
        entered, release = threading.Event(), threading.Event()
        results = []
        def slow_ocr():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test release timed out')
            return ['Accounts']
        self.ocr.visible_text_candidates.side_effect = slow_ocr
        def request():
            with self.app.test_client() as client:
                authenticate_admin(client)
                results.append(client.post('/api/recorder/text-suggestions',
                    json=dict(step_id=self.recorder.workflow['steps'][0]['id']), headers=self.headers))
        thread = threading.Thread(target=request)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.state.lock.acquire(timeout=1))
            self.state.lock.release()
            self.post('/api/heartbeat', {'frame_id':123})
            self.state.camera.frame = lambda: (b'jpeg', 123, 0.0, (1280, 720))
            self.assertEqual(self.client.get('/api/frame').status_code, 200)
            self.action('back')
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 400)
        self.pi.back.assert_called_once()

    def test_current_screen_failure_manual_entry_and_cancel_keep_recording(self):
        before = deepcopy(self.recorder.workflow)
        self.post('/api/recorder/text-suggestions', {'current': True}, 503)
        self.assertTrue(self.recorder.active)
        self.assertEqual(self.recorder.workflow, before)
        completed = self.post(
            '/api/recorder/expected-text',
            {'text': 'Entered manually', 'timeout_seconds': 4},
        )
        self.assertTrue(completed['active'])
        self.assertEqual(completed['workflow']['steps'][-1]['text'], 'Entered manually')

        self.post('/api/recorder/text-suggestions', {'current': True}, 503)
        cancelled = self.post('/api/recorder/expected-text/cancel', {})
        self.assertTrue(cancelled['active'])
        self.assertEqual(len(cancelled['workflow']['steps']), 1)
        self.assertFalse(cancelled['expected_text_pending'])

    def test_stop_and_save_clear_expected_text_state(self):
        self.action('tap', x=.5, y=.5)
        self.post('/api/recorder/text-suggestions', {'current': True}, 503)
        stopped = self.post('/api/recorder/stop', {})
        self.assertFalse(stopped['expected_text_pending'])
        self.post('/api/recorder/save', {}, 201)
        self.assertIsNone(self.recorder.workflow)
        self.assertIsNone(self.recorder.pending_expected_text)


class LiveCaptureTests(unittest.TestCase):
    post = test_record_first.RecordFirstTests.post
    capture = test_record_first.RecordFirstTests.capture

    def test_capture_tap_swipe_key_never_reads_ocr_or_retries(self):
        with patch('automation.workflows.api.CameraOCRScreenStateProvider', autospec=True) as provider:
            env = patch.dict(os.environ, PHONE_MANUAL_INPUT_BACKEND="appium")
            env.start()
            self.addCleanup(env.stop)
            test_record_first.RecordFirstTests.setUp(self)
            provider.return_value.visible_texts.side_effect = AssertionError('No OCR')
            provider.return_value.visible_text_candidates.side_effect = AssertionError(
                'No candidate OCR'
            )
            for step in [dict(type='tap', x=.5, y=.5),
                         dict(type='swipe', start=[.5,.8], end=[.5,.2], duration_ms=300),
                         dict(type='key', key='BACK')]:
                self.capture(step)
            provider.return_value.visible_texts.assert_not_called()
            provider.return_value.visible_text_candidates.assert_not_called()
            # Capturing the recorder thumbnail is not OCR inference.
            self.assertEqual(provider.return_value.capture_snapshot.call_count, 3)
            self.assertEqual([call.args[1] for call in self.controller.send.call_args_list], ["tap", "back"])
            self.controller.swipe.assert_called_once()
            self.controller.send_key.assert_not_called()
