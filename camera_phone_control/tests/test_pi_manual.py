"""Pi HTTP and manual dashboard tests; no Pi, ADB, Appium or phone required."""
import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from android_control import AndroidControl
from app import create_app
from device_lock import DeviceLocks, DeviceBusy
from hardware.pi_api_client import PiApiClient, PiApiError, MAX_RESPONSE_BYTES
from hardware.raspberry_pi_input import RaspberryPiInputBridge
from automation.jobs.storage import DeviceWorkflowAssignmentStore
from automation.workflows.models import new_workflow
from automation.workflows.storage import WorkflowStorage
from test_control import FakeCamera, CORNERS, authenticate_admin


class PiClientTests(unittest.TestCase):
    def setUp(self):
        self.connection_patch = patch('hardware.pi_api_client.http.client.HTTPConnection')
        self.factory = self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.connection = self.factory.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"ok":true,"connected":true}'
        self.client = PiApiClient('http://pi.test:8766', 2, 5)

    def last_request(self):
        call = self.connection.request.call_args
        body = call.kwargs['body']
        return call.args, json.loads(body) if body else None

    def test_health_status_and_timeouts(self):
        self.assertTrue(self.client.health()['ok'])
        self.assertTrue(self.client.status()['connected'])
        self.factory.assert_called_with('pi.test', 8766, timeout=2.0)
        self.connection.sock.settimeout.assert_called_with(5.0)
        self.assertEqual([call.args[:2] for call in self.connection.request.call_args_list], [('GET','/health'),('GET','/devices/phone_A/status')])
        self.assertEqual(self.connection.close.call_count, 2)

    def test_all_input_endpoints_match_existing_contract(self):
        actions = [
            (lambda:self.client.tap(.5,.75), '/devices/phone_A/tap', {'x':.5,'y':.75}),
            (lambda:self.client.swipe(.5,.8,.5,.2), '/devices/phone_A/swipe', {'x1':.5,'y1':.8,'x2':.5,'y2':.2}),
            (lambda:self.client.type_text('hello world'), '/devices/phone_A/text', {'text':'hello world'}),
            (lambda:self.client.key('enter'), '/devices/phone_A/key', {'key':'ENTER'}),
            (self.client.home,'/devices/phone_A/home',None), (self.client.back,'/devices/phone_A/back',None), (self.client.wake,'/devices/phone_A/wake',None),
        ]
        for action, path, payload in actions:
            with self.subTest(path=path):
                self.connection.reset_mock()
                action()
                self.assertEqual(self.last_request(), (('POST',path),payload))
                self.connection.request.assert_called_once()
                self.connection.close.assert_called_once()

    def test_async_202_actions_succeed_and_retain_command_metadata(self):
        actions = [
            self.client.tap,
            self.client.swipe,
            self.client.type_text,
            self.client.key,
            self.client.home,
            self.client.back,
            self.client.wake,
        ]
        arguments = [(.5, .75), (.5, .8, .5, .2), ('hello',), ('enter',), (), (), ()]
        self.response.status = 202
        for index, (action, args) in enumerate(zip(actions, arguments), 1):
            command_id = f'command-{index}'
            self.response.read.return_value = json.dumps({
                'ok': True,
                'accepted': True,
                'status': 'queued',
                'command_id': command_id,
            }).encode()
            result = action(*args)
            self.assertTrue(result['accepted'])
            self.assertEqual(result['command_id'], command_id)
            self.assertEqual(self.client.last_command_id, command_id)
            self.assertEqual(self.client.last_action_status, 'queued')
            self.assertEqual(self.client.last_action_http_status, 202)

    def test_every_2xx_status_and_legacy_200_response_remain_successful(self):
        for status in (200, 201, 202, 299):
            with self.subTest(status=status):
                self.response.status = status
                self.response.read.return_value = b'{"ok":true}'
                self.assertTrue(self.client.home()['ok'])
                self.assertEqual(self.client.last_action_http_status, status)
                self.assertIsNone(self.client.last_command_id)

    def test_invalid_optional_command_id_does_not_turn_202_into_failure(self):
        self.response.status = 202
        self.response.read.return_value = b'{"ok":true,"command_id":"bad\\nvalue"}'
        self.assertTrue(self.client.home()['ok'])
        self.assertIsNone(self.client.last_command_id)
        self.assertEqual(self.client.last_action_http_status, 202)
        self.connection.request.assert_called_once()

    def test_invalid_coordinates_rejected_before_http(self):
        for value in [-.1,1.1,True,None,'0.5',float('nan'),float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError): self.client.tap(value,.5)
            with self.assertRaises(ValueError): self.client.swipe(.5,.8,value,.2)
        self.factory.assert_not_called()

    def test_offline_and_timeout_are_not_retried(self):
        for error in [ConnectionRefusedError(),TimeoutError(),http.client.RemoteDisconnected()]:
            with self.subTest(error=type(error).__name__):
                self.factory.reset_mock(); self.connection.connect.side_effect=error
                with self.assertRaises(PiApiError) as result: self.client.home()
                self.assertIn('Pi',str(result.exception))
                self.assertIn('no retry',str(result.exception))
                self.factory.assert_called_once(); self.connection.close.assert_called_once()

    def test_response_timeout_does_not_repeat_post(self):
        self.response.read.side_effect=TimeoutError()
        with self.assertRaises(PiApiError) as failed: self.client.tap(.5,.5)
        self.assertEqual(failed.exception.error_code, "pi_api_timeout")
        self.assertEqual(
            failed.exception.error_params,
            {
                "path": "/devices/phone_A/tap",
                "input_may_have_occurred": True,
            },
        )
        self.assertEqual(
            str(failed.exception),
            "Pi API /devices/phone_A/tap timed out. "
            "Input may have occurred; no retry attempted.",
        )
        self.connection.request.assert_called_once()

    def test_http_json_ok_and_connection_state_validation(self):
        for status, body in [(500,b'{"ok":true}'), (302,b'{}'), (200,b'not-json'),
                             (200,b'[]'), (200,b'{}'), (200,b'{"ok":1}'),
                             (200,b'{"ok":false,"error":"Bluetooth disconnected"}')]:
            with self.subTest(status=status,body=body):
                self.response.status=status; self.response.read.return_value=body
                with self.assertRaises(PiApiError): self.client.home()
        self.response.status=200
        self.response.read.return_value=b'{"ok":true,"connected":"yes"}'
        with self.assertRaises(PiApiError): self.client.status()
        self.response.read.return_value=b'x'*(MAX_RESPONSE_BYTES+1)
        with self.assertRaises(PiApiError): self.client.health()

    def test_error_does_not_echo_typed_values(self):
        self.response.read.return_value=b'{"ok":false,"error":"cannot type secret-value"}'
        with self.assertRaises(PiApiError) as result: self.client.type_text('secret-value')
        self.assertNotIn('secret-value',str(result.exception))
        self.assertIn('Pi rejected /devices/phone_A/text',str(result.exception))

    def test_configuration_and_invalid_input(self):
        with patch.dict(os.environ, PI_API_BASE_URL='http://pi.local:8766',PI_API_CONNECT_TIMEOUT='1.5',PI_API_READ_TIMEOUT='4'):
            client=PiApiClient()
            self.assertEqual(client.base_url,'http://pi.local:8766')
            self.assertEqual((client.connect_timeout,client.read_timeout),(1.5,4))
        for url in ['file:///tmp/test','http://user:password@pi.local','http://pi.local/path','http://pi.local?key=value']:
            with self.assertRaises(ValueError): PiApiClient(url)
        for value in [0,-1,'nan','inf',False]:
            with self.assertRaises(ValueError): PiApiClient('http://pi.local',value,5)
        for text in ['',None,'hello\n', 'a'*4097]:
            with self.assertRaises(ValueError): self.client.type_text(text)
        with self.assertRaises(ValueError): self.client.key('ENTER;home')

    def test_bridge_maps_navigation_without_extra_endpoints(self):
        bridge=RaspberryPiInputBridge(self.client)
        bridge.action('screen_on'); self.assertEqual(self.last_request()[0],('POST','/devices/phone_A/wake'))
        bridge.action('swipe_up')
        self.assertEqual(self.last_request(),(('POST','/devices/phone_A/swipe'),{'x1':.5,'y1':.75,'x2':.5,'y2':.25}))
        with self.assertRaises(ValueError): bridge.action('recents')
        with self.assertRaises(ValueError): bridge.action('screen_off')

    def test_device_management_endpoints_and_device_scoped_actions(self):
        self.response.read.return_value = b'{"ok":true,"devices":[],"enrollment":{"active":false}}'
        self.client.devices()
        self.assertEqual(self.last_request()[0], ('GET', '/devices'))
        self.client.device_status('phone_B')
        self.assertEqual(self.last_request()[0], ('GET', '/devices/phone_B/status'))
        self.client.enrollment()
        self.assertEqual(self.last_request()[0], ('GET', '/enrollment'))
        self.client.start_enrollment('phone_B')
        self.assertEqual(
            self.last_request(),
            (('POST', '/enrollment/start'), {'device_id': 'phone_B'}),
        )
        self.client.stop_enrollment()
        self.assertEqual(self.last_request()[0], ('POST', '/enrollment/stop'))
        self.client.replace_device('phone_B')
        self.assertEqual(self.last_request()[0], ('POST', '/devices/phone_B/replace'))
        self.client.delete_device('phone_B')
        self.assertEqual(self.last_request()[0], ('DELETE', '/devices/phone_B'))
        self.client.select_device('phone_B')
        self.client.home()
        self.assertEqual(self.last_request()[0], ('POST', '/devices/phone_B/home'))

    def test_management_errors_do_not_claim_physical_input_may_have_occurred(self):
        self.response.status = 503
        for operation in (
            lambda: self.client.start_enrollment('phone_B'),
            lambda: self.client.replace_device('phone_A'),
            lambda: self.client.delete_device('phone_A'),
        ):
            self.connection.reset_mock()
            with self.assertRaises(PiApiError) as failed:
                operation()
            self.assertNotIn('Input may have occurred', str(failed.exception))
            self.connection.request.assert_called_once()

        self.connection.reset_mock()
        with self.assertRaises(PiApiError) as physical:
            self.client.home()
        self.assertIn('Input may have occurred', str(physical.exception))
        self.connection.request.assert_called_once()


class PiDashboardTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,PHONE_INPUT_BACKEND='appium',PHONE_SCREEN_BACKEND='appium')
        self.env.start();self.addCleanup(self.env.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.locks=DeviceLocks(Path(self.temp.name)/'locks')
        self.android=Mock(spec=AndroidControl)
        self.android.device.side_effect=AssertionError('Pi manual controls must never discover ADB devices')
        self.pi=Mock(spec=PiApiClient);self.pi.base_url='http://pi.test:8766'
        self.pi.health.return_value={'ok':True,'api':'ready'}
        self.pi.status.return_value={'ok':True,'connected':True}
        self.assignments=DeviceWorkflowAssignmentStore(Path(self.temp.name)/'device_workflows.json')
        self.app=create_app(FakeCamera(),self.android,self.locks,manual_backend='raspberry_pi',pi_client=self.pi,device_workflow_store=self.assignments)
        self.client=self.app.test_client()
        authenticate_admin(self.client)
        self.headers={'X-Control-Token':self.app.config['CONTROL_TOKEN'],'X-Control-Client':'pi-browser-test'}
        self.state=self.app.extensions['control_state']
        self.addCleanup(self.state.disable)

    def post(self,path,data,status=200):
        response=self.client.post(path,json=data,headers=self.headers)
        self.assertEqual(response.status_code,status,response.json)
        return response.json

    def enable(self):
        self.client.get('/api/pi/status')
        self.client.get('/api/status', headers=self.headers)
        self.post('/api/calibration',{'corners':CORNERS,'frame_id':123})
        self.post('/api/control',{'enabled':True,'frame_id':123})

    def test_status_from_pi_not_adb_and_dashboard_renders(self):
        response=self.client.get('/api/pi/status')
        self.assertEqual(response.json['api'],'online');self.assertEqual(response.json['phone'],'connected')
        status=self.client.get('/api/status',headers=self.headers).json
        self.assertEqual(status['manual_backend'],'raspberry_pi')
        self.assertTrue(status['device']['normalized'])
        self.assertIsNone(status['device']['width'])
        page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'pi-api-state',page.data);self.assertNotIn(b'send-manual-text',page.data);self.assertIn(b'keyboard-help',page.data)
        self.android.device.assert_not_called()

    def test_device_catalog_enrollment_replace_and_selection_are_proxied(self):
        catalog = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [
                {'id': 'phone_A', 'name': 'Phone A', 'connected': True,
                 'bluetooth_address': 'F8:AD:CB:28:4E:75', 'default': True},
            ],
            'enrollment': {'active': False, 'remaining_seconds': None,
                           'target_device_id': None},
        }
        self.pi.devices.return_value = catalog
        result = self.client.get('/api/devices', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['selected_device'], 'phone_A')

        self.pi.start_enrollment.return_value = {'ok': True, 'status': 'waiting'}
        started = self.post('/api/enrollment/start', {
            'target_device_id': 'phone_B',
            'display_name': '接待處 Phone #2 (KL)',
        })
        self.assertTrue(started['ok'])
        self.pi.start_enrollment.assert_called_once_with('phone_B')
        self.pi.replace_device.assert_not_called()

        self.pi.stop_enrollment.return_value = {'ok': True}
        self.post('/api/enrollment/stop', {})
        self.pi.stop_enrollment.assert_called_once_with()

        catalog['devices'].append(
            {'id': 'phone_B', 'name': 'Phone B', 'connected': False,
             'bluetooth_address': None, 'default': False}
        )
        refreshed = self.client.get('/api/devices', headers=self.headers)
        self.assertEqual(
            refreshed.json['devices'][1]['name'],
            '接待處 Phone #2 (KL)',
        )
        existing = self.client.post(
            '/api/enrollment/start',
            json={'target_device_id': 'phone_B'},
            headers=self.headers,
        )
        self.assertEqual(existing.status_code, 400)
        self.pi.start_enrollment.assert_called_once_with('phone_B')
        self.pi.replace_device.return_value = {'ok': True, 'status': 'waiting'}
        self.post('/api/devices/phone_B/replace', {})
        self.pi.replace_device.assert_called_once_with('phone_B')

        selected = self.post('/api/devices/select', {'device_id': 'phone_B'})
        self.assertEqual(selected['selected_device'], 'phone_B')
        self.pi.select_device.assert_called_once_with('phone_B')
        self.assertEqual(self.state.pi_bridge.device_id, 'phone_B')
        self.assertEqual(
            self.app.extensions['workflow_service'].device_id,
            'phone_B',
        )

    def test_new_phone_defaults_not_ready_and_operator_toggle_persists(self):
        catalog = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_A', 'connected': True}],
            'enrollment': {'active': False},
        }
        self.pi.devices.return_value = catalog
        self.pi.start_enrollment.return_value = {'ok': True, 'status': 'waiting'}

        self.assertTrue(
            self.client.get('/api/devices', headers=self.headers).json['devices'][0][
                'ready_for_work'
            ]
        )
        self.post('/api/enrollment/start', {'target_device_id': 'phone_B'})
        self.assertFalse(self.assignments.ready_for_work('phone_B'))

        catalog['devices'].append({'id': 'phone_B', 'connected': True})
        devices = self.client.get('/api/devices', headers=self.headers).json['devices']
        phone_b = next(item for item in devices if item['id'] == 'phone_B')
        self.assertFalse(phone_b['ready_for_work'])
        changed = self.client.put(
            '/api/devices/phone_B/work-readiness',
            json={'ready': True},
            headers=self.headers,
        )
        self.assertEqual(changed.status_code, 200, changed.json)
        self.assertTrue(changed.json['ready_for_work'])
        restarted = DeviceWorkflowAssignmentStore(
            Path(self.temp.name) / 'device_workflows.json'
        )
        self.assertTrue(restarted.ready_for_work('phone_B'))

        invalid = self.client.put(
            '/api/devices/phone_B/work-readiness',
            json={'ready': 'yes'},
            headers=self.headers,
        )
        self.assertEqual(invalid.status_code, 400)

    def test_device_selection_rejects_unregistered_id(self):
        self.pi.devices.return_value = {
            'ok': True,
            'devices': [{'id': 'phone_A'}],
            'enrollment': {'active': False},
        }
        response = self.client.post(
            '/api/devices/select',
            json={'device_id': 'phone_Z'},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.pi.select_device.assert_not_called()

        replaced = self.client.post(
            '/api/devices/phone_Z/replace',
            json={},
            headers=self.headers,
        )
        self.assertEqual(replaced.status_code, 400)
        self.pi.replace_device.assert_not_called()

    def test_delete_selected_device_falls_back_without_clearing_calibration(self):
        both = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_A'}, {'id': 'phone_B'}],
            'enrollment': {'active': False},
        }
        after = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_A'}],
            'enrollment': {'active': False},
        }
        self.pi.devices.side_effect = [both, both, after]
        self.post('/api/devices/select', {'device_id': 'phone_B'})
        calibration_storage = Mock()
        self.state.calibration_storage = calibration_storage
        self.state.camera.source = 0
        self.state.camera.rotate = 0
        self.pi.delete_device.return_value = {'ok': True, 'deleted': 'phone_B'}

        response = self.client.delete(
            '/api/devices/phone_B',
            json={},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.pi.delete_device.assert_called_once_with('phone_B')
        self.assertEqual(response.json['selected_device'], 'phone_A')
        self.assertEqual(self.state.pi_bridge.device_id, 'phone_A')
        self.assertEqual(
            self.app.extensions['workflow_service'].device_id,
            'phone_A',
        )
        calibration_storage.clear.assert_not_called()

    def test_delete_last_device_clears_selection_safely(self):
        self.pi.delete_device.return_value = {'ok': True, 'deleted': 'phone_A'}
        before = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_A'}],
            'enrollment': {'active': False},
        }
        after = {
            'ok': True,
            'default_device': None,
            'devices': [],
            'enrollment': {'active': False},
        }
        self.pi.devices.side_effect = [before, after]
        response = self.client.delete(
            '/api/devices/phone_A',
            json={},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(response.json['selected_device'])
        self.assertIsNone(self.state.pi_bridge.device_id)
        self.assertIsNone(self.state.device)
        self.assertFalse(self.state.snapshot('pi-browser-test')['calibrated'])

    def test_delete_unknown_device_preserves_selection_and_returns_404(self):
        self.pi.devices.return_value = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_A'}],
            'enrollment': {'active': False},
        }
        response = self.client.delete(
            '/api/devices/phone_Z',
            json={},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.state.pi_bridge.device_id, 'phone_A')
        self.pi.delete_device.assert_not_called()

    def test_device_poll_selects_first_remaining_only_when_current_is_missing(self):
        self.pi.devices.return_value = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_C', 'connected': True}],
            'enrollment': {'active': False},
        }
        response = self.client.get('/api/devices', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['selected_device'], 'phone_C')
        self.assertEqual(self.state.pi_bridge.device_id, 'phone_C')
        self.assertEqual(self.state.pi_connection['phone'], 'connected')

        self.pi.reset_mock()
        self.pi.devices.return_value = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [
                {'id': 'phone_A', 'connected': True},
                {'id': 'phone_C', 'connected': False},
            ],
            'enrollment': {'active': False},
        }
        preserved = self.client.get('/api/devices', headers=self.headers)
        self.assertEqual(preserved.json['selected_device'], 'phone_C')
        self.assertEqual(self.state.pi_connection['phone'], 'waiting')
        self.pi.select_device.assert_not_called()

        self.pi.devices.return_value = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_A', 'connected': True}],
            'enrollment': {'active': False},
        }
        repaired = self.client.get('/api/devices', headers=self.headers)
        self.assertEqual(repaired.json['selected_device'], 'phone_A')
        self.pi.select_device.assert_called_once_with('phone_A')

    def test_disconnected_selected_phone_blocks_workflow_start(self):
        storage = WorkflowStorage(Path(self.temp.name) / 'workflows')
        workflow = new_workflow('Disconnected phone')
        workflow['steps'] = [{'id': 'wait', 'type': 'wait', 'seconds': 0}]
        workflow = storage.save(workflow, create=True)
        app = create_app(
            FakeCamera(),
            self.android,
            self.locks,
            storage,
            manual_backend='raspberry_pi',
            pi_client=self.pi,
        )
        client = app.test_client()
        authenticate_admin(client)
        headers = {
            'X-Control-Token': app.config['CONTROL_TOKEN'],
            'X-Control-Client': 'workflow-browser-test',
        }
        self.pi.devices.return_value = {
            'ok': True,
            'default_device': 'phone_A',
            'devices': [{'id': 'phone_A', 'connected': False}],
            'enrollment': {'active': False},
        }
        self.assertEqual(client.get('/api/devices', headers=headers).status_code, 200)
        response = client.post(
            f"/api/workflows/{workflow['id']}/run",
            json={},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('disconnected', response.json['error'])
        for method in ('tap', 'swipe', 'type_text', 'key', 'home', 'back', 'wake'):
            getattr(self.pi, method).assert_not_called()

    def test_calibrated_tap_directional_swipes_and_navigation_use_pi_only(self):
        self.enable()
        result=self.post('/api/action',{'action':'tap','x':.5,'y':.7,'frame_id':123})
        self.assertIsNone(result['pixel'])
        self.assertAlmostEqual(result['normalized'][0],.5);self.assertAlmostEqual(result['normalized'][1],.75)
        x,y=self.pi.tap.call_args.args
        self.assertAlmostEqual(x,.5);self.assertAlmostEqual(y,.75)
        for name,method in [('screen_on','wake'),('home','home'),('back','back')]:
            self.post('/api/action',{'action':name,'frame_id':123})
            getattr(self.pi,method).assert_called_once()
        for name in ['swipe_up','swipe_down','swipe_left','swipe_right']:
            self.post('/api/action',{'action':name,'frame_id':123})
        self.assertEqual(self.pi.swipe.call_count,4)
        self.post('/api/action',{'action':'recents','frame_id':123},400)
        self.android.device.assert_not_called();self.android.send.assert_not_called()

    def test_preview_drag_uses_calibrated_custom_swipe_once(self):
        self.enable()
        result = self.post('/api/action', {
            'action': 'swipe',
            'start': [.1, .1],
            'end': [.9, .9],
            'duration_ms': 420,
            'frame_id': 123,
        })
        self.assertTrue(result['recording'] is None)
        self.pi.swipe.assert_called_once_with(0.0, 0.0, 1.0, 1.0)
        self.android.swipe.assert_not_called()

        for invalid in (
            {'start': None, 'end': [.5, .5], 'duration_ms': 300},
            {'start': [.5, .5], 'end': [1.1, .5], 'duration_ms': 300},
            {'start': [.5, .5], 'end': [.6, .6], 'duration_ms': True},
        ):
            self.post('/api/action', {
                'action': 'swipe', 'frame_id': 123, **invalid
            }, 400)
        self.pi.swipe.assert_called_once()

    def test_text_named_key_and_existing_keyboard_forwarding(self):
        self.enable()
        self.post('/api/text',{'text':'hello world','frame_id':123})
        self.pi.type_text.assert_called_with('hello world')
        self.post('/api/key',{'type':'text','value':'a','frame_id':123})
        self.pi.type_text.assert_called_with('a')
        self.post('/api/key',{'type':'key','value':'enter','frame_id':123})
        self.pi.key.assert_called_once_with('enter')
        for key in ['left', 'right', 'up', 'down', 'delete', 'home', 'end', 'pageup', 'pagedown', 'backspace', 'tab']:
            response = self.post('/api/key', {'type':'key', 'value':key, 'frame_id':123})
            self.assertTrue(response['sent'])
            self.pi.key.assert_called_with(key)
        self.android.send_key.assert_not_called();self.android.device.assert_not_called()

    def test_offline_waiting_invalid_status_disable_input_without_fallback(self):
        self.enable()
        self.pi.status.return_value={'ok':True,'connected':False}
        self.client.get('/api/pi/status')
        result=self.client.get('/api/status',headers=self.headers).json
        self.assertEqual(result['pi']['api'],'online');self.assertEqual(result['pi']['phone'],'waiting')
        self.assertFalse(result['enabled'])
        self.pi.status.side_effect=PiApiError('Pi API timed out.')
        self.client.get('/api/pi/status')
        self.assertEqual(self.client.get('/api/pi/status').json['api'],'offline')
        self.post('/api/action',{'action':'home','frame_id':123},409)
        self.pi.home.assert_not_called();self.android.send.assert_not_called()
        self.android.device.assert_not_called()

    def test_failed_post_is_not_repeated_or_sent_via_adb(self):
        self.enable();self.pi.home.side_effect=PiApiError('Pi API /home timed out; no retry attempted.')
        result=self.post('/api/action',{'action':'home','frame_id':123},409)
        self.assertIn('Pi API',result['error']);self.assertIsNone(self.state.owner)
        self.pi.home.assert_called_once();self.android.send.assert_not_called()

    def test_existing_calibration_frame_ownership_and_lock_gates(self):
        self.client.get('/api/pi/status')
        self.client.get('/api/status', headers=self.headers)
        self.post('/api/action',{'action':'home','frame_id':123},400)
        self.enable()
        self.post('/api/action',{'action':'tap','x':0,'y':0,'frame_id':123},400)
        with self.locks.hold(self.state.device.serial,'automation'):
            self.post('/api/action',{'action':'home','frame_id':123},409)
        self.pi.home.assert_not_called();self.pi.tap.assert_not_called()
        self.enable()
        self.post('/api/text',{'text':'hello','frame_id':122},400)
        self.pi.type_text.assert_not_called()

    def test_takeover_signals_same_pi_endpoint_lock_without_input(self):
        self.enable();key=self.state.device.serial
        with self.locks.hold(key,'automation') as ownership:
            result=self.post('/api/takeover',{},202)
            self.assertTrue(result['pending']);self.assertTrue(ownership.takeover_requested())
            self.post('/api/action',{'action':'home','frame_id':123},409)
        self.post('/api/heartbeat',{'frame_id':123})
        self.post('/api/action',{'action':'home','frame_id':123})
        self.pi.home.assert_called_once()
        self.android.device.assert_not_called()

    def test_default_dashboard_uses_pi_without_discovering_android(self):
        with patch.dict(os.environ, {}, clear=False), patch('app.AndroidControl', return_value=self.android):
            os.environ.pop('PHONE_MANUAL_INPUT_BACKEND', None)
            app=create_app(FakeCamera(), pi_client=self.pi)
            response=authenticate_admin(app.test_client()).get('/api/status', headers=self.headers)
            self.assertEqual(response.json['manual_backend'],'raspberry_pi')
            self.android.device.assert_not_called()

    def test_existing_global_mock_mode_cannot_send_real_pi_manual_input(self):
        with patch.dict(os.environ, PHONE_INPUT_BACKEND='mock'):
            os.environ.pop('PHONE_MANUAL_INPUT_BACKEND', None)
            with patch('app.RaspberryPiInputBridge') as bridge:
                app=create_app(FakeCamera())
                response=authenticate_admin(app.test_client()).get('/api/status',headers=self.headers)
                self.assertEqual(response.status_code,200)
                self.assertIsNone(app.extensions['control_state'].pi_bridge)
                bridge.assert_not_called()

    def test_only_explicit_status_requests_check_pi_and_always_refresh(self):
        for _ in range(3):
            self.client.get('/api/status', headers=self.headers)
            self.client.get('/api/frame')
            self.post('/api/heartbeat', {'frame_id':123})
        self.pi.status.assert_not_called()
        for count in range(1, 4):
            self.assertEqual(self.client.get('/api/pi/status').json['phone'], 'connected')
            self.assertEqual(self.pi.status.call_count, count)
        self.pi.status.side_effect = PiApiError('Pi unavailable')
        self.assertEqual(self.client.get('/api/pi/status').json['api'], 'offline')
        for _ in range(3):
            status = self.client.get('/api/status', headers=self.headers).json
            self.assertEqual(status['pi']['api'], 'offline')
        self.assertEqual(self.pi.status.call_count, 4)
        self.pi.status.side_effect = None
        self.assertEqual(self.client.get('/api/pi/status').json['phone'], 'connected')
        self.assertEqual(self.pi.status.call_count, 5)
        self.pi.health.assert_not_called()

    def test_recovered_status_clears_stale_timeout_notice(self):
        self.pi.status.side_effect = PiApiError(
            'Pi API /devices/phone_A/status timed out.',
            error_code='pi_api_timeout',
            error_params={'path': '/devices/phone_A/status'},
        )
        pi_status = self.client.get('/api/pi/status').json
        self.assertEqual(pi_status['error_code'], 'pi_api_timeout')
        self.assertEqual(
            pi_status['error_params'], {'path': '/devices/phone_A/status'}
        )
        failed = self.client.get('/api/status', headers=self.headers).json
        self.assertEqual(
            failed['message'], 'Pi API /devices/phone_A/status timed out.'
        )
        self.assertEqual(failed['message_error_code'], 'pi_api_timeout')
        self.assertEqual(
            failed['message_error_params'], {'path': '/devices/phone_A/status'}
        )
        self.assertIsNone(failed['device'])

        self.pi.status.side_effect = None
        self.client.get('/api/pi/status')
        recovered = self.client.get('/api/status', headers=self.headers).json
        self.assertEqual(recovered['pi']['phone'], 'connected')
        self.assertIsNotNone(recovered['device'])
        self.assertNotIn('timed out', recovered['message'])
        self.assertIn('Calibrate', recovered['message'])
        self.assertFalse(recovered['enabled'])
        self.assertEqual(self.pi.status.call_count, 2)

    def test_recovery_clears_displayed_error_after_intermediate_status_change(self):
        error = 'Cannot reach Pi API for /status. Check the Pi address and private Wi-Fi.'
        self.pi.status.side_effect = PiApiError(error)
        self.client.get('/api/pi/status')
        failed = self.client.get('/api/status', headers=self.headers).json
        self.assertEqual(failed['message'], error)
        # An explicit check can change the cached Pi result before a snapshot.
        self.pi.status.side_effect = None
        self.pi.status.return_value = {'ok':True, 'connected':False}
        self.client.get('/api/pi/status')
        self.pi.status.return_value = {'ok':True, 'connected':True}
        self.client.get('/api/pi/status')
        recovered = self.client.get('/api/status', headers=self.headers).json
        self.assertEqual(recovered['pi']['phone'], 'connected')
        self.assertNotIn('Cannot reach', recovered['message'])
        self.assertIn('Calibrate', recovered['message'])
        self.assertFalse(recovered['enabled'])
        self.assertEqual(self.pi.status.call_count, 3)

    def test_recovered_status_preserves_unrelated_safety_notice(self):
        self.pi.status.side_effect = PiApiError('Pi API /status timed out.')
        self.client.get('/api/pi/status')
        self.state.disable('Camera resolution changed. Recalibrate before enabling input.')
        self.pi.status.side_effect = None
        self.client.get('/api/pi/status')
        status = self.client.get('/api/status', headers=self.headers).json
        self.assertEqual(status['message'], 'Camera resolution changed. Recalibrate before enabling input.')
        self.assertFalse(status['enabled'])

    def test_enable_always_checks_fresh_hid_status_once_without_retry(self):
        for connected in [True, False, None]:
            with self.subTest(connected=connected):
                self.pi.status.side_effect = None
                self.pi.status.return_value = {'ok':True, 'connected':True}
                self.enable()
                self.post('/api/control', {'enabled':False})
                self.pi.reset_mock()
                self.pi.status.return_value = {'ok':True, 'connected':connected}
                if connected is None:
                    self.pi.status.side_effect = PiApiError('Pi API /status timed out.')
                result = self.post('/api/control', {'enabled':True, 'frame_id':123},
                                   200 if connected else 409)
                self.pi.status.assert_called_once_with()
                self.assertEqual(self.state.owner is not None, connected is True)
                if not connected:
                    self.assertIn('Pi', result['error'])
                self.client.get('/api/status', headers=self.headers)
                self.post('/api/heartbeat', {'frame_id':123})
                self.pi.status.assert_called_once_with()

    def test_actions_and_heartbeats_keep_last_known_status_without_checking_pi(self):
        self.enable()
        self.pi.reset_mock()
        last_known = dict(self.state.pi_connection)
        # Even if a new status check would fail, physical inputs go straight through.
        self.pi.status.side_effect = PiApiError('Pi status must not be called')
        for action in ['tap', 'swipe_up', 'home', 'back', 'screen_on']:
            data = {'action':action, 'frame_id':123}
            if action == 'tap': data.update(x=.5, y=.5)
            self.post('/api/action', data)
        self.post('/api/text', {'text':'hello', 'frame_id':123})
        self.post('/api/key', {'type':'key', 'value':'enter', 'frame_id':123})
        self.post('/api/key', {'type':'text', 'value':'a', 'frame_id':123})
        self.post('/api/heartbeat', {'frame_id':123})
        self.client.get('/api/status', headers=self.headers)
        self.assertEqual(self.state.pi_connection, last_known)
        self.pi.status.assert_not_called()
        self.pi.health.assert_not_called()

    def test_failed_actions_disable_control_without_status_check_or_retry(self):
        cases = [
            ('/api/action', {'action':'tap', 'x':.5, 'y':.5}, 'tap'),
            ('/api/action', {'action':'swipe_up'}, 'swipe'),
            ('/api/action', {'action':'home'}, 'home'),
            ('/api/action', {'action':'back'}, 'back'),
            ('/api/action', {'action':'screen_on'}, 'wake'),
            ('/api/key', {'type':'key', 'value':'enter'}, 'key'),
            ('/api/key', {'type':'text', 'value':'a'}, 'type_text'),
            ('/api/text', {'text':'hello'}, 'type_text'),
        ]
        for path, data, method in cases:
            with self.subTest(path=path, method=method):
                self.enable()
                self.pi.reset_mock(side_effect=True)
                action = getattr(self.pi, method)
                action.side_effect = PiApiError('Pi input timed out; no retry attempted.')
                result = self.post(path, dict(data, frame_id=123), 409)
                self.assertIn('no retry', result['error'])
                self.assertIsNone(self.state.owner)
                action.assert_called_once()
                self.pi.status.assert_not_called()
                self.pi.health.assert_not_called()

    def test_new_text_route_keeps_local_token_protection(self):
        response=self.client.post('/api/text',json={'text':'hello','frame_id':123})
        self.assertEqual(response.status_code,403)
        self.pi.type_text.assert_not_called()
