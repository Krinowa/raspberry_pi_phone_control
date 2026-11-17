"""Acknowledged Pi manual actions produce portable version-1 workflow steps."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import create_app
from android_control import AndroidControl, Device
from automation.input.appium import AppiumInputBridge
from automation.workflows.storage import WorkflowStorage
from automation.workflows.validation import validate_workflow
from device_lock import DeviceLocks
from hardware.pi_api_client import PiApiClient, PiApiError
from test_control import authenticate_admin


class Camera:
    def status(self):
        return dict(ready=True, width=1280, height=720, index=0, error=None)

    def validate_frame(self, frame_id):
        if frame_id != 123:
            raise ValueError('A fresh camera frame is required.')


class PiRecordingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, PHONE_INPUT_BACKEND='appium', PHONE_SCREEN_BACKEND='appium')
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = WorkflowStorage(Path(temporary.name) / 'flows')
        self.android = Mock(spec=AndroidControl)
        self.android.device.side_effect = AssertionError('Pi recording must not discover USB devices')
        self.pi = Mock(spec=PiApiClient)
        self.pi.base_url = 'http://pi.test:8766'
        self.pi.status.return_value = {'ok': True, 'connected': True}
        self.app = create_app(Camera(), self.android, DeviceLocks(Path(temporary.name) / 'locks'),
                              self.storage, manual_backend='raspberry_pi', pi_client=self.pi)
        self.state = self.app.extensions['control_state']
        self.recorder = self.app.extensions['workflow_recorder']
        self.addCleanup(self.state.disable)
        self.client = self.app.test_client()
        authenticate_admin(self.client)
        self.headers = {'X-Control-Token': self.app.config['CONTROL_TOKEN'],
                        'X-Control-Client': 'pi-record-browser', 'X-Recorder-Client': 'pi-record-owner'}
        self.client.get('/api/pi/status')
        self.post('/api/calibration', {'corners': [[.1,.1],[.9,.1],[.9,.9],[.1,.9]], 'frame_id':123})
        self.post('/api/control', {'enabled':True, 'frame_id':123})
        self.post('/api/recorder/start', {'name':'Test Recording', 'execute':True})
        self.pi.reset_mock()

    def post(self, path, data, status=200, headers=None):
        response = self.client.post(path, json=data, headers=headers or self.headers)
        self.assertEqual(response.status_code, status, response.json)
        return response.json

    def action(self, name, **extra):
        return self.post('/api/action', dict(action=name, frame_id=123, **extra))

    def key(self, value, kind='key'):
        return self.post('/api/key', dict(type=kind, value=value, frame_id=123))

    @property
    def steps(self):
        return self.recorder.workflow['steps']

    def test_calibrated_tap_records_exact_physical_normalized_point(self):
        result = self.action('tap', x=.5, y=.7)
        self.pi.tap.assert_called_once()
        self.assertEqual(len(self.steps), 1)
        self.assertEqual(self.steps[0]['type'], 'tap')
        self.assertAlmostEqual(self.steps[0]['x'], .5)
        self.assertAlmostEqual(self.steps[0]['y'], .75)
        self.assertEqual(tuple(result['normalized']), self.pi.tap.call_args.args)
        self.assertEqual(result['recording']['workflow']['steps'], self.steps)
        self.pi.status.assert_not_called()
        self.android.device.assert_not_called()

    def test_swipes_and_navigation_record_logical_geometry_and_keys(self):
        for action in ['swipe_up', 'swipe_down', 'swipe_left', 'swipe_right']:
            self.action(action)
            step = self.steps[-1]
            self.assertEqual(step['type'], 'swipe')
            self.assertEqual(tuple(step['start'] + step['end']), self.pi.swipe.call_args.args)
        for action, key in [('home','HOME'), ('back','BACK'), ('screen_on','SCREEN_ON')]:
            self.action(action)
            self.assertEqual(self.steps[-1]['key'], key)
        self.assertEqual(len(self.steps), 7)
        self.pi.home.assert_called_once()
        self.pi.back.assert_called_once()
        self.pi.wake.assert_called_once()
        self.pi.status.assert_not_called()

    def test_preview_drag_records_the_calibrated_custom_swipe(self):
        result = self.action(
            'swipe', start=[.1, .1], end=[.9, .9], duration_ms=420
        )
        self.pi.swipe.assert_called_once_with(0.0, 0.0, 1.0, 1.0)
        self.assertEqual(self.steps[-1]['type'], 'swipe')
        self.assertEqual(self.steps[-1]['start'], [0.0, 0.0])
        self.assertEqual(self.steps[-1]['end'], [1.0, 1.0])
        self.assertEqual(self.steps[-1]['duration_ms'], 420)
        self.assertEqual(result['recording']['workflow']['steps'], self.steps)

    def test_screen_normalized_tap_and_swipe_skip_calibration_mapping(self):
        original_map = self.state.calibration.map
        self.state.calibration.map = Mock(side_effect=AssertionError(
            'screen-normalized input must not be calibrated again'
        ))
        try:
            for point in ([.5, .5], [0, 0], [1, 0], [0, 1], [1, 1]):
                with self.subTest(point=point):
                    self.pi.reset_mock()
                    tap = self.action(
                        'tap',
                        x=point[0],
                        y=point[1],
                        coordinate_space='screen_normalized',
                    )
                    self.assertEqual(tap['normalized'], point)
                    self.pi.tap.assert_called_once_with(*point)

            self.pi.reset_mock()
            self.action(
                'swipe',
                start=[0, 0],
                end=[1, 1],
                duration_ms=300,
                coordinate_space='screen_normalized',
            )
            self.pi.swipe.assert_called_once_with(0, 0, 1, 1)
        finally:
            self.state.calibration.map = original_map

    def test_missing_coordinate_space_keeps_legacy_camera_mapping(self):
        original_map = self.state.calibration.map
        self.state.calibration.map = Mock(wraps=original_map)
        try:
            self.action('tap', x=.5, y=.5)
            self.state.calibration.map.assert_called_once_with([.5, .5])
        finally:
            self.state.calibration.map = original_map

    def test_recorder_screen_coordinates_are_stored_without_double_mapping(self):
        self.recorder.execute = False
        original_map = self.state.calibration.map
        self.state.calibration.map = Mock(side_effect=AssertionError(
            'screen-normalized recording must not be calibrated again'
        ))
        try:
            result = self.post(
                '/api/recorder/step',
                {
                    'step': {'type': 'swipe', 'duration_ms': 300},
                    'capture': False,
                    'frame_id': 123,
                    'camera_start': [0, 0],
                    'camera_end': [1, 1],
                    'coordinate_space': 'screen_normalized',
                },
            )
        finally:
            self.state.calibration.map = original_map
        step = result['workflow']['steps'][-1]
        self.assertEqual(step['start'], [0, 0])
        self.assertEqual(step['end'], [1, 1])

    def test_text_groups_only_recording_and_special_keys_break_groups(self):
        for index, char in enumerate('hello', 1):
            self.key(char, 'text')
            self.assertEqual(self.pi.type_text.call_count, index)
            self.pi.type_text.assert_called_with(char)
        self.key('enter')
        for char in 'world':
            self.key(char, 'text')
        self.key('backspace')
        self.key('!','text')
        self.assertEqual([s['type'] for s in self.steps], ['input','key','input','key','input'])
        self.assertEqual([s.get('value', s.get('key')) for s in self.steps],
                         ['hello','ENTER','world','BACKSPACE','!'])
        self.pi.status.assert_not_called()
        self.pi.health.assert_not_called()

    def test_all_manual_keys_and_text_route_are_supported(self):
        for name in ['enter','backspace','tab','left','right','up','down','delete','home','end','pageup','pagedown']:
            self.key(name)
            self.assertEqual(self.steps[-1]['key'], 'MOVE_HOME' if name == 'home' else name.upper())
            self.pi.key.assert_called_with(name)
        self.post('/api/text', {'text':'hello', 'frame_id':123})
        self.assertEqual(self.steps[-1]['value'], 'hello')
        validate_workflow(self.recorder.workflow)

    def test_failed_and_blocked_actions_are_not_successful_steps(self):
        self.post('/api/action', {'action':'tap','x':0,'y':0,'frame_id':123}, 400)
        self.post('/api/action', {'action':'recents','frame_id':123}, 400)
        self.post('/api/key', {'type':'key','value':'enter','frame_id':122}, 400)
        self.assertEqual(self.steps, [])
        self.post('/api/control', {'enabled':True,'frame_id':123})
        self.pi.reset_mock()
        self.pi.home.side_effect = PiApiError('Input may have occurred; no retry attempted.')
        result = self.post('/api/action', {'action':'home','frame_id':123}, 409)
        self.assertIn('may have occurred', result['error'])
        self.assertEqual(self.steps, [])
        self.assertFalse(self.recorder.active)
        self.assertIsNone(self.state.owner)
        self.pi.home.assert_called_once()
        self.pi.status.assert_not_called()

    def test_automation_ownership_and_foreign_recorder_cannot_capture(self):
        with self.state.device_locks.hold(self.state.device.serial, 'automation'):
            self.post('/api/action', {'action':'home','frame_id':123}, 409)
        self.assertEqual(self.steps, [])
        self.pi.home.assert_not_called()
        self.post('/api/control', {'enabled':True,'frame_id':123})
        self.post('/api/action', {'action':'home','frame_id':123}, 400,
                  headers=dict(self.headers, **{'X-Recorder-Client':'another-recorder'}))
        self.assertEqual(self.steps, [])
        self.pi.home.assert_not_called()

    def test_stop_review_save_and_old_workflow_compatibility(self):
        self.action('tap', x=.5, y=.5)
        self.key('a', 'text')
        self.post('/api/recorder/stop', {})
        self.action('back')
        self.assertEqual(len(self.steps), 2)
        text_id = self.steps[1]['id']
        self.post('/api/recorder/review', {'operation':'input','step_id':text_id,'source':'fixed','value':'edited'})
        self.post('/api/recorder/review', {'operation':'insert','step':{'type':'wait','seconds':1.5}})
        wait_id = self.steps[-1]['id']
        self.post('/api/recorder/review', {'operation':'wait','step_id':wait_id,'seconds':2.5})
        self.post('/api/recorder/review', {'operation':'move','step_id':wait_id,'index':0})
        self.post('/api/recorder/review', {'operation':'delete','step_id':text_id})
        self.post('/api/recorder/review', {'operation':'rename_flow','name':'Recorded Demo'})
        saved = self.post('/api/recorder/save', {}, 201)
        loaded = self.storage.read(saved['id'])
        self.assertEqual(loaded['name'], 'Recorded Demo')
        self.assertEqual(loaded['steps'][0]['seconds'], 2.5)
        self.assertEqual(loaded['version'], 1)
        encoded = json.dumps(loaded)
        for internal in ['http', '/tap', '/key', 'PiApiClient', 'RaspberryPiInputBridge', 'pi-api:', 'frame_id']:
            self.assertNotIn(internal, encoded)
        demo = Path(__file__).resolve().parents[1] / 'automation/workflow_data/demo_flow.json'
        validate_workflow(json.loads(demo.read_text()))

    def test_legacy_capture_endpoint_cannot_execute_pi_recording_via_automation(self):
        self.post('/api/recorder/step', {'step':{'type':'key','key':'ENTER'},'capture':True,'frame_id':123},400)
        self.assertEqual(self.steps, [])
        self.pi.key.assert_not_called()
        self.android.send_key.assert_not_called()

    def test_text_length_boundary_starts_a_new_step_without_blocking_input(self):
        self.post('/api/text', {'text':'x'*4096,'frame_id':123})
        self.key('y', 'text')
        self.assertEqual([len(s['value']) for s in self.steps], [4096, 1])
        self.assertEqual(self.pi.type_text.call_count, 2)

    def test_extended_keys_preserve_legacy_navigation_and_keyboard_semantics(self):
        device = Device('usb-test', 1080, 1920, 0)
        bridge = AppiumInputBridge(self.android, device, lambda: None)
        bridge.key('HOME')
        self.android.send.assert_called_once_with(device, 'home')
        bridge.key('MOVE_HOME')
        self.android.send_key.assert_called_with(device, 'key', 'home')
        for key in ['LEFT','RIGHT','UP','DOWN','DELETE','END','PAGEUP','PAGEDOWN']:
            bridge.key(key)
            self.android.send_key.assert_called_with(device, 'key', key.lower())


if __name__ == '__main__':
    unittest.main()
