"""Hardware-free backend, workflow, recorder/API and ownership checks."""
import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from android_control import AndroidControl, Device
from app import create_app
from automation.backends import BackendConfigurationError, backend_names
from automation.context import AutomationContext, Cancelled
from automation.coordinates import normalized_to_device, device_to_normalized
from automation.input.base import UnsupportedOperation
from automation.input.mock import MockInputBridge
from automation.runner import Runner
from automation.workflows.models import new_workflow
from automation.workflows.recorder import Recorder
from automation.workflows.runner import WorkflowRunner
from automation.workflows.service import WorkflowService
from automation.workflows.storage import WorkflowStorage
from automation.workflows.validation import validate_workflow, resolve_variables
from automation.integrations.withdraw_v2.balance import balance_check_workflow
from device_lock import DeviceLocks, DeviceBusy
from test_control import FakeCamera, CORNERS, authenticate_admin

DEVICE = Device('usb-test', 1080, 1920, 0)


def flow(steps=None):
    result = new_workflow('Demo')
    result['id'] = 'demo'
    result['steps'] = steps if steps is not None else [{'id': 'tap1', 'type': 'tap', 'x': .5, 'y': .75}]
    return result


def wait_until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition(): return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for test condition')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, PHONE_INPUT_BACKEND='appium', PHONE_SCREEN_BACKEND='appium')
        self.env.start(); self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.locks = DeviceLocks(Path(self.temp.name) / 'locks')
        self.controller = Mock(spec=AndroidControl); self.controller.device.return_value = DEVICE
        self.logger = logging.Logger('test-workflow', logging.INFO)
        self.output = io.StringIO(); self.logger.addHandler(logging.StreamHandler(self.output))

    def context(self, cancel=None, takeover=None):
        return AutomationContext(DEVICE, self.controller, cancel or threading.Event(), self.logger,
                                 SimpleNamespace(), takeover)

    def test_coordinates_edges_roundtrip_and_rejections(self):
        self.assertEqual(normalized_to_device(1, 1, 1080, 1920), (1079, 1919))
        self.assertEqual(normalized_to_device(0, 0, 1080, 1920), (0, 0))
        self.assertEqual(normalized_to_device(.5, .5, 3, 3), (1, 1))
        self.assertEqual(device_to_normalized(1079, 1919, 1080, 1920), (1, 1))
        for bad in [True, -1, 1.1, float('nan'), float('inf'), '0.5']:
            with self.assertRaises(ValueError): normalized_to_device(bad, .5, 1080, 1920)
        with self.assertRaises(ValueError): normalized_to_device(.5, .5, 1, 100)

    def test_workflow_retry_offset_skips_only_steps_before_start_index(self):
        ctx = self.context()
        workflow = flow(
            [
                {"id": "first", "type": "tap", "x": 0.1, "y": 0.1},
                {"id": "failed", "type": "tap", "x": 0.2, "y": 0.2},
                {"id": "last", "type": "tap", "x": 0.3, "y": 0.3},
            ]
        )

        WorkflowRunner(ctx).run(workflow, start_step_index=1)

        self.assertEqual(
            self.controller.send.call_args_list,
            [
                unittest.mock.call(DEVICE, "tap", (216, 384)),
                unittest.mock.call(DEVICE, "tap", (324, 576)),
            ],
        )
        self.assertEqual(ctx.state.step_index, 3)
        self.assertEqual(ctx.state.step_id, "last")

    def test_workflow_runner_retains_exact_failed_step_position_and_id(self):
        ctx = self.context()
        self.controller.send.side_effect = [None, RuntimeError("input failed")]
        workflow = flow(
            [
                {"id": "first", "type": "tap", "x": 0.1, "y": 0.1},
                {"id": "failed", "type": "tap", "x": 0.2, "y": 0.2},
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "input failed"):
            WorkflowRunner(ctx).run(workflow)

        self.assertEqual(ctx.state.step_index, 2)
        self.assertEqual(ctx.state.step_id, "failed")

    def test_existing_script_and_context_compatibility(self):
        ctx = self.context()
        ctx.send('tap', (333, 999)); ctx.send_key('key', 'enter'); ctx.launch_app('com.android.settings')
        ctx.dump_ui(); ctx.screen.capture(); ctx.input.swipe(0, 1, 1, 0, 350)
        self.controller.send.assert_called_with(DEVICE, 'tap', (333, 999))
        self.controller.send_key.assert_called_with(DEVICE, 'key', 'enter')
        self.controller.launch_app.assert_called_with(DEVICE, 'com.android.settings')
        self.controller.dump_ui.assert_called_with(DEVICE)
        self.controller.screenshot.assert_called_with(DEVICE)
        self.controller.swipe.assert_called_with(DEVICE, (0, 1919), (1079, 0), 350)
        with self.assertRaises(UnsupportedOperation): ctx.input.move(1, 0)
        with self.assertRaises(UnsupportedOperation): ctx.input.click()

    def test_all_new_input_and_screen_methods_check_cancellation(self):
        cancel = threading.Event(); ctx = self.context(cancel); cancel.set()
        calls = [lambda: ctx.input.tap(.5, .5), lambda: ctx.input.swipe(0, 0, 1, 1),
                 lambda: ctx.input.key('ENTER'), lambda: ctx.input.type_text('hello'),
                 ctx.input.click, lambda: ctx.input.move(1, 1), ctx.screen.capture, ctx.screen.inspect]
        for call in calls:
            with self.assertRaises(Cancelled): call()
        self.assertEqual(self.controller.mock_calls, [])

    def test_text_takes_over_between_characters_without_logging_values(self):
        event = threading.Event(); ctx = self.context(event)
        self.controller.send_key.side_effect = lambda *args: event.set()
        with self.assertRaises(Cancelled): ctx.input.type_text('hello')
        self.controller.send_key.assert_called_once_with(DEVICE, 'text', 'h')
        self.assertNotIn('hello', self.output.getvalue())

    def test_invalid_text_sends_nothing(self):
        ctx = self.context()
        with self.assertRaises(ValueError): ctx.input.type_text('hello\n')
        self.controller.send_key.assert_not_called()

    def test_validation_rejects_invalid_entire_flow_before_input(self):
        for step in [{'id': 'bad', 'type': 'unknown'}, {'id': 'bad', 'type': 'tap', 'x': float('nan'), 'y': .2},
                     {'id': 'bad', 'type': 'wait', 'seconds': True},
                     {'id': 'bad', 'type': 'input', 'source': 'variable', 'variable': 'missing'},
                     {'id': 'bad', 'type': 'tap', 'target': {'strategy': 'template'}, 'x': .5, 'y': .5}]:
            workflow = flow(); workflow['steps'].append(step)
            with self.assertRaises(ValueError): WorkflowRunner(self.context()).run(workflow)
        self.controller.send.assert_not_called()

    def test_variables_defaults_required_type_unknown_and_privacy(self):
        workflow = flow([{'id': 'text1', 'type': 'input', 'source': 'variable', 'variable': 'search_text'}])
        workflow['variables'] = [{'name': 'search_text', 'label': 'Search Text', 'type': 'text', 'required': True, 'default': None}]
        with self.assertRaises(ValueError): resolve_variables(workflow)
        with self.assertRaises(ValueError): resolve_variables(workflow, {'unknown': 'abc'})
        self.assertEqual(resolve_variables(workflow, {'search_text': 'hello'}), {'search_text': 'hello'})
        workflow['variables'][0]['default'] = 'default'
        self.assertEqual(resolve_variables(workflow), {'search_text': 'default'})
        self.assertEqual(resolve_variables(workflow, {'search_text': ''}), {'search_text': ''})
        workflow['variables'][0]['default'] = ''
        self.assertEqual(resolve_variables(workflow), {'search_text': ''})
        workflow['variables'][0]['name'] = 'otp'
        with self.assertRaises(ValueError): validate_workflow(workflow)
        with self.assertRaises(ValueError): validate_workflow(flow([{'id': 'a', 'type': 'input', 'source': 'manual', 'value': 'secret'}]))
        workflow = flow(); workflow['financial'] = True
        with self.assertRaises(ValueError): validate_workflow(workflow)
        workflow['steps'].append({'id': 'confirm', 'type': 'manual_checkpoint', 'message': 'Review and authorize manually.'})
        validate_workflow(workflow)

    def test_storage_crud_metadata_paths_and_symlinks(self):
        store = WorkflowStorage(Path(self.temp.name) / 'flows')
        saved = store.save(flow(), create=True)
        self.assertEqual(store.list()[0]['step_count'], 1)
        self.assertEqual(store.read('demo'), saved)
        changed = deepcopy(saved); changed['name'] = 'Changed'
        updated = store.save(changed)
        self.assertEqual(updated['created_at'], saved['created_at'])
        for bad in ['../evil', '/tmp/evil', 'a/b', '..']:
            with self.assertRaises(ValueError): store.read(bad)
        target = Path(self.temp.name) / 'outside.json'; target.write_text('{}')
        (store.directory / 'link.json').symlink_to(target)
        with self.assertRaises(ValueError): store.save({**flow(), 'id': 'link'})
        store.delete('demo')
        with self.assertRaises(FileNotFoundError): store.read('demo')

    def test_workflow_purpose_validation_and_legacy_migration(self):
        store = WorkflowStorage(Path(self.temp.name) / "purpose-flows")
        payment = store.save(flow(), create=True)
        self.assertEqual(payment["purpose"], "payment")
        self.assertEqual(store.list()[0]["purpose"], "payment")

        balance = new_workflow("Legacy balance", "balance_check")
        balance["id"] = "legacy_balance"
        balance["steps"] = [
            {
                "id": "read",
                "type": "ocr_value",
                "parser": "currency",
                "output": "payer_balance",
                "roi": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.2},
            }
        ]
        saved_balance = store.save(balance, create=True)
        for saved in (payment, saved_balance):
            path = store.path(saved["id"])
            raw = json.loads(path.read_text())
            raw.pop("purpose")
            path.write_text(json.dumps(raw))

        migrated = store.migrate_legacy_purposes(
            {"legacy_balance"}, balance_check_workflow
        )
        self.assertEqual(
            dict(migrated), {"demo": "payment", "legacy_balance": "balance_check"}
        )
        self.assertEqual(store.read("demo")["purpose"], "payment")
        self.assertEqual(store.read("legacy_balance")["purpose"], "balance_check")

        for unsupported in ("general", "custom", "debug", "other", "receipt"):
            with self.subTest(unsupported=unsupported):
                with self.assertRaisesRegex(ValueError, "purpose"):
                    validate_workflow({**flow(), "purpose": unsupported})

    def test_mock_input_no_hardware_and_redacted_commands(self):
        with patch.dict(os.environ, PHONE_INPUT_BACKEND='mock'):
            ctx = self.context()
            WorkflowRunner(ctx).run(flow([
                {'id': 't', 'type': 'tap', 'x': .5, 'y': .7},
                {'id': 's', 'type': 'swipe', 'start': [.5, .8], 'end': [.5, .2], 'duration_ms': 350},
                {'id': 'i', 'type': 'input', 'source': 'fixed', 'value': 'private-value'},
                {'id': 'k', 'type': 'key', 'key': 'ENTER'}, {'id': 'w', 'type': 'wait', 'seconds': 0}]))
            self.assertEqual(self.controller.mock_calls, [])
            logs = self.output.getvalue()
            self.assertIn('MOCK TAP', logs); self.assertIn('MOCK SWIPE', logs)
            self.assertNotIn('private-value', logs)
            with self.assertRaises(UnsupportedOperation): ctx.screen.inspect()
            self.assertEqual(self.controller.mock_calls, [])

    def test_unsupported_backend_fails_clearly(self):
        with patch.dict(os.environ, PHONE_INPUT_BACKEND='unsupported'):
            with self.assertRaises(BackendConfigurationError): backend_names()

    def test_manual_checkpoint_releases_lock_and_resumes(self):
        service = WorkflowService(self.controller, self.locks)
        workflow = flow([{'id': 'manual', 'type': 'manual_checkpoint', 'message': 'Complete manually.'},
                         {'id': 'tap', 'type': 'tap', 'x': .5, 'y': .5}])
        run = service.start(workflow); rid = run['run_id']
        wait_until(lambda: service.snapshot(rid)['status'] == 'waiting_manual')
        with self.locks.hold(DEVICE.serial, 'manual'): self.controller.send.assert_not_called()
        with self.assertRaises(ValueError): service.resume(rid, 999)
        self.assertFalse(service.runs[rid]['resume'].is_set())
        service.resume(rid, 1)
        service.runs[rid]['thread'].join(3)
        self.assertEqual(service.snapshot(rid)['status'], 'passed')
        self.controller.send.assert_called_once_with(DEVICE, 'tap', (540, 960))
        self.assertIsNone(self.locks.owner(DEVICE.serial))

    def test_manual_checkpoint_cancel_and_failed_reacquisition_send_nothing(self):
        for resume in [False, True]:
            service = WorkflowService(self.controller, self.locks)
            rid = service.start(flow([{'id': 'm', 'type': 'manual_checkpoint', 'message': 'Manual'},
                                     {'id': 't', 'type': 'tap', 'x': 0, 'y': 0}]))['run_id']
            wait_until(lambda: service.snapshot(rid)['status'] == 'waiting_manual')
            with self.locks.hold(DEVICE.serial, 'manual'):
                (service.resume if resume else service.cancel)(rid)
                service.runs[rid]['thread'].join(3)
            self.assertEqual(service.snapshot(rid)['status'], 'failed' if resume else 'cancelled')
            self.controller.send.assert_not_called()

    def test_takeover_stops_wait_and_prevents_next_step(self):
        service = WorkflowService(self.controller, self.locks)
        rid = service.start(flow([{'id': 'w', 'type': 'wait', 'seconds': 30},
                                 {'id': 't', 'type': 'tap', 'x': 0, 'y': 0}]))['run_id']
        wait_until(lambda: service.snapshot(rid)['step_index'] == 1)
        claim = self.locks.request_takeover(DEVICE.serial)
        try:
            service.runs[rid]['thread'].join(3)
            self.assertEqual(service.snapshot(rid)['status'], 'cancelled')
            self.assertTrue(claim.try_acquire())
            self.controller.send.assert_not_called()
        finally: claim.close()

    def test_api_recorder_mapping_save_variable_run_and_token(self):
        store = WorkflowStorage(Path(self.temp.name) / 'data')
        app = create_app(FakeCamera(), self.controller, self.locks, store)
        client = app.test_client()
        authenticate_admin(client)
        headers = {'X-Control-Token': app.config['CONTROL_TOKEN'], 'X-Control-Client': 'test-browser-123'}
        def post(path, data):
            response = client.post(path, json=data, headers=headers)
            self.assertLess(response.status_code, 300, response.json)
            return response.json
        self.assertEqual(client.post('/api/recorder/start', json={'name': 'Demo'}).status_code, 403)
        post('/api/calibration', {'corners': CORNERS, 'frame_id': 123})
        post('/api/recorder/start', {'name': 'Recorded', 'execute': False})
        result = post('/api/recorder/step', {'step': {'type': 'tap'}, 'camera_start': [.5, .7], 'frame_id': 123})
        tap = result['workflow']['steps'][0]
        self.assertAlmostEqual(tap['x'], .5); self.assertAlmostEqual(tap['y'], .75)
        post('/api/recorder/step', {'step': {'type': 'swipe', 'duration_ms': 420}, 'camera_start': [.5, .7], 'camera_end': [.5, .3], 'frame_id': 123})
        definition = {'name': 'search_text', 'label': 'Search Text', 'type': 'text', 'required': True, 'default': None}
        post('/api/recorder/step', {'step': {'type': 'input', 'source': 'variable', 'variable': 'search_text'}, 'variable': definition})
        self.controller.send.assert_not_called()
        post('/api/recorder/stop', {})
        saved = post('/api/recorder/save', {})
        self.assertEqual(len(saved['steps']), 3)
        run = post('/api/workflows/' + saved['id'] + '/run', {'variables': {'search_text': 'hello'}})
        service = app.extensions['workflow_service']; service.runs[run['run_id']]['thread'].join(3)
        status = client.get('/api/workflow-runs/' + run['run_id']).json
        self.assertEqual(status['status'], 'passed')
        self.assertNotIn('hello', json.dumps(status))
        self.controller.send.assert_called_once_with(DEVICE, 'tap', (540, 1439))
        self.assertEqual(self.controller.send_key.call_count, 5)
        self.assertEqual(client.delete('/api/workflows/' + saved['id'], json={}, headers=headers).status_code, 200)

    def test_recorder_undo_stop_cancel_and_sensitive_validation(self):
        recorder = Recorder(); recorder.start('owner', 'Demo')
        candidate = recorder.prepare('owner', {'type': 'key', 'key': 'ENTER'})
        recorder.workflow = candidate
        self.assertEqual(len(recorder.undo('owner')['workflow']['steps']), 0)
        with self.assertRaises(ValueError): recorder.prepare('other', {'type': 'wait', 'seconds': 1})
        recorder.stop('owner')
        with self.assertRaises(ValueError): recorder.prepare('owner', {'type': 'wait', 'seconds': 1})
        recorder.cancel('owner'); self.assertIsNone(recorder.workflow)

    def test_recorder_can_mark_and_clear_structured_irreversible_step(self):
        recorder = Recorder(); recorder.start('owner', 'Payment')
        candidate = recorder.prepare('owner', {'type': 'tap', 'x': .5, 'y': .5})
        recorder.workflow = candidate
        recorder.stop('owner')
        step_id = recorder.workflow['steps'][0]['id']

        marked = recorder.review('owner', 'set_critical', {
            'step_id': step_id, 'critical': True,
        })
        self.assertTrue(marked['workflow']['steps'][0]['critical'])
        cleared = recorder.review('owner', 'set_critical', {
            'step_id': step_id, 'critical': False,
        })
        self.assertNotIn('critical', cleared['workflow']['steps'][0])

    def test_current_mae_script_still_uses_context_bridge(self):
        import xml.etree.ElementTree as ET
        from automation.scripts import open_mae
        state = open_mae.STARTUP_STATES[0]
        node = ET.fromstring('<node bounds="[100,200][300,400]"/>')
        context = self.context()
        with patch.object(context, 'wait'), patch.object(open_mae, 'wait_for_any_button',
                side_effect=[(state, '<hierarchy/>', node), (None, '<hierarchy/>', None)]), \
                patch.object(open_mae, 'wait_for_transition', return_value=True):
            open_mae.run(context)
        self.controller.launch_app.assert_called_once_with(DEVICE, open_mae.PACKAGE)
        self.assertEqual(self.controller.send.call_args_list[-1].args, (DEVICE, 'tap', (200, 300)))

    def test_recorder_live_execution_obeys_manual_lock_frame_and_geometry(self):
        camera = FakeCamera()
        app = create_app(camera, self.controller, self.locks, WorkflowStorage(Path(self.temp.name) / 'data'))
        client = app.test_client()
        authenticate_admin(client)
        headers = {'X-Control-Token': app.config['CONTROL_TOKEN'], 'X-Control-Client': 'test-browser-123'}
        def post(path, data): return client.post(path, json=data, headers=headers)
        post('/api/calibration', {'corners': CORNERS, 'frame_id': 123})
        post('/api/recorder/start', {'name': 'Live', 'execute': True})
        step = {'step': {'type': 'tap'}, 'camera_start': [.5, .5], 'frame_id': 123}
        self.assertEqual(post('/api/recorder/step', step).status_code, 400)
        self.controller.send.assert_not_called()
        post('/api/recorder/cancel', {})
        post('/api/control', {'enabled': True, 'frame_id': 123})
        post('/api/recorder/start', {'name': 'Live', 'execute': True})
        self.assertEqual(post('/api/recorder/step', {**step, 'frame_id': 122}).status_code, 400)
        self.controller.send.assert_not_called()
        post('/api/control', {'enabled': True, 'frame_id': 123})
        self.assertEqual(post('/api/recorder/step', step).status_code, 200)
        self.controller.send.assert_called_once_with(DEVICE, 'tap', (540, 960))
        with self.locks.hold(DEVICE.serial, 'automation'):
            self.assertEqual(post('/api/recorder/step', step).status_code, 409)
        self.assertEqual(self.controller.send.call_count, 1)
        self.assertFalse(app.extensions['workflow_recorder'].active)
        self.assertEqual(len(app.extensions['workflow_recorder'].workflow['steps']), 1)
        with app.extensions['control_state'].lock: app.extensions['control_state'].disable()

    def test_recording_identity_survives_refresh_without_granting_manual_ownership(self):
        app = create_app(FakeCamera(), self.controller, self.locks, WorkflowStorage(Path(self.temp.name) / 'data'))
        client = app.test_client()
        authenticate_admin(client)
        headers = {'X-Control-Token': app.config['CONTROL_TOKEN'], 'X-Control-Client': 'original-browser', 'X-Recorder-Client': 'recording-tab-id'}
        self.assertEqual(client.post('/api/recorder/start', json={'name': 'Draft'}, headers=headers).status_code, 200)
        refreshed = {**headers, 'X-Control-Client': 'refreshed-browser'}
        self.assertEqual(client.get('/api/recorder', headers=refreshed).status_code, 200)
        self.assertEqual(client.get('/api/recorder', headers={**refreshed, 'X-Recorder-Client': 'other-recorder'}).status_code, 400)
        self.controller.send.assert_not_called()

    def test_mock_service_never_discovers_a_device(self):
        with patch.dict(os.environ, PHONE_INPUT_BACKEND='mock'):
            service = WorkflowService(self.controller, self.locks)
            rid = service.start(flow())['run_id']; service.runs[rid]['thread'].join(3)
            self.assertEqual(service.snapshot(rid)['status'], 'passed')
            self.assertEqual(self.controller.mock_calls, [])


if __name__ == '__main__': unittest.main()
