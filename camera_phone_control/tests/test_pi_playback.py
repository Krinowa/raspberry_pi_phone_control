"""Workflow Pi input uses real client serialization over a mocked HTTP connection."""
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from android_control import AndroidControl, Device, DeviceError
from app import create_app
from automation.backends import backend_names, select_device
from automation.context import AutomationContext
from automation.input.base import UnsupportedOperation
from automation.runner import Runner
from automation.workflows.models import new_workflow
from automation.workflows.runner import WorkflowRunner
from automation.workflows.service import WorkflowService
from automation.workflows.storage import WorkflowStorage
from device_lock import DeviceLocks
from test_control import authenticate_admin


class PiPlaybackTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, PHONE_INPUT_BACKEND='raspberry_pi',
                         PHONE_SCREEN_BACKEND='appium', PHONE_MANUAL_INPUT_BACKEND='appium',
                         PI_API_BASE_URL='http://pi.test:8766')
        env.start()
        self.addCleanup(env.stop)
        http = patch('hardware.pi_api_client.http.client.HTTPConnection')
        self.factory = http.start()
        self.addCleanup(http.stop)
        self.connection = self.factory.return_value
        response = self.connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"ok":true}'
        self.android = Mock(spec=AndroidControl)
        self.android.device.side_effect = AssertionError('Input playback must not discover USB')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.locks = DeviceLocks(Path(temporary.name) / 'locks')
        self.storage = WorkflowStorage(Path(temporary.name) / 'flows')
        self.logger = logging.Logger('pi-playback-test')

    def workflow(self, *steps):
        workflow = new_workflow('Pi playback')
        workflow['steps'] = [dict(step, id=f'step_{i}') for i, step in enumerate(steps)]
        return workflow

    def run_workflow(self, workflow, variables=None, cancel=None, device_id=None):
        runner = Runner(self.android, self.locks, self.logger)
        result = runner.run_task('test', lambda ctx: WorkflowRunner(ctx).run(workflow, variables),
                                 cancel_event=cancel, device_id=device_id)
        return result

    def requests(self):
        return [(call.args[:2], json.loads(call.kwargs['body']) if call.kwargs['body'] else None)
                for call in self.connection.request.call_args_list]

    @staticmethod
    def pi_path(operation, device_id='phone_A'):
        return f'/devices/{device_id}/{operation}'

    def test_logical_workflow_reaches_pi_without_usb_status_or_health(self):
        workflow = self.workflow(
            {'type':'tap','x':.5144619210016607,'y':.09930441494402194},
            {'type':'swipe','start':[.5,.8],'end':[.5,.2],'duration_ms':350},
            {'type':'input','source':'fixed','value':'hello world'},
            {'type':'key','key':'ENTER'}, {'type':'key','key':'BACKSPACE'},
            {'type':'key','key':'HOME'}, {'type':'key','key':'BACK'},
            {'type':'key','key':'SCREEN_ON'}, {'type':'wait','seconds':0})
        state = self.run_workflow(workflow)
        self.assertEqual(state.status, 'completed', state.message)
        self.assertEqual(state.device_serial, 'pi-api:http://pi.test:8766')
        self.assertEqual(state.device_id, 'phone_A')
        self.assertEqual(self.requests(), [
            (('POST',self.pi_path('tap')),{'x':.5144619210016607,'y':.09930441494402194}),
            (('POST',self.pi_path('swipe')),{'x1':.5,'y1':.8,'x2':.5,'y2':.2}),
            (('POST',self.pi_path('text')),{'text':'hello world'}),
            (('POST',self.pi_path('key')),{'key':'ENTER'}), (('POST',self.pi_path('key')),{'key':'BACKSPACE'}),
            (('POST',self.pi_path('home')),None), (('POST',self.pi_path('back')),None), (('POST',self.pi_path('wake')),None)])
        self.assertEqual(self.android.mock_calls, [])
        self.assertEqual(self.factory.call_count, 8)
        self.assertEqual(self.connection.close.call_count, 8)

    def test_async_202_action_is_accepted_without_completion_polling(self):
        response = self.connection.getresponse.return_value
        response.status = 202
        response.read.return_value = (
            b'{"ok":true,"accepted":true,"status":"queued",'
            b'"command_id":"workflow-command"}'
        )
        state = self.run_workflow(
            self.workflow({'type':'key','key':'HOME'}),
            device_id='phone_A',
        )
        self.assertEqual(state.status, 'completed', state.message)
        self.assertEqual(state.device_id, 'phone_A')
        self.assertEqual(self.requests(), [(('POST',self.pi_path('home')),None)])
        self.connection.getresponse.assert_called_once()

    def test_explicit_second_phone_routes_workflow_to_its_device_endpoint(self):
        state = self.run_workflow(
            self.workflow({'type':'key','key':'HOME'}),
            device_id='phone_B',
        )
        self.assertEqual(state.status, 'completed', state.message)
        self.assertEqual(state.device_id, 'phone_B')
        self.assertEqual(state.device_serial, 'pi-api:http://pi.test:8766#phone_B')
        self.assertEqual(
            self.requests(),
            [(('POST', self.pi_path('home', 'phone_B')), None)],
        )

    def test_simultaneous_devices_keep_distinct_hid_routes(self):
        barrier = threading.Barrier(2)

        def overlap_requests(*_args, **_kwargs):
            barrier.wait(timeout=2)

        self.connection.request.side_effect = overlap_requests
        service = WorkflowService(self.android, self.locks)
        workflow = self.workflow({'type':'key','key':'HOME'})
        run_1 = service.start(workflow, {}, device_id='phone_1')
        run_2 = service.start(workflow, {}, device_id='phone_2')
        result_1 = service.wait(run_1['run_id'], 2)
        result_2 = service.wait(run_2['run_id'], 2)

        self.assertEqual(result_1['status'], 'passed', result_1)
        self.assertEqual(result_2['status'], 'passed', result_2)
        self.assertEqual(result_1['device_id'], 'phone_1')
        self.assertEqual(result_2['device_id'], 'phone_2')
        self.assertCountEqual(
            self.requests(),
            [
                (('POST', self.pi_path('home', 'phone_1')), None),
                (('POST', self.pi_path('home', 'phone_2')), None),
            ],
        )

    def test_ambiguous_failure_is_not_retried_or_rerouted_to_other_device(self):
        barrier = threading.Barrier(2)

        def fail_phone_1_once(_method, path, **_kwargs):
            barrier.wait(timeout=2)
            if path == self.pi_path('home', 'phone_1'):
                raise TimeoutError()

        self.connection.request.side_effect = fail_phone_1_once
        service = WorkflowService(self.android, self.locks)
        workflow = self.workflow({'type':'key','key':'HOME'})
        run_1 = service.start(workflow, {}, device_id='phone_1')
        run_2 = service.start(workflow, {}, device_id='phone_2')
        result_1 = service.wait(run_1['run_id'], 2)
        result_2 = service.wait(run_2['run_id'], 2)

        self.assertEqual(result_1['status'], 'failed', result_1)
        self.assertIn('no retry attempted', result_1['message'])
        self.assertEqual(result_2['status'], 'passed', result_2)
        routed_paths = [call.args[1] for call in self.connection.request.call_args_list]
        self.assertEqual(routed_paths.count(self.pi_path('home', 'phone_1')), 1)
        self.assertEqual(routed_paths.count(self.pi_path('home', 'phone_2')), 1)
        self.assertEqual(len(routed_paths), 2)
        self.assertIsNone(self.locks.owner('pi-api:http://pi.test:8766#phone_1'))
        self.assertIsNone(self.locks.owner('pi-api:http://pi.test:8766#phone_2'))

    def test_all_keyboard_keys_and_navigation_home_are_distinct(self):
        names = ['TAB','LEFT','RIGHT','UP','DOWN','DELETE','MOVE_HOME','END','PAGEUP','PAGEDOWN']
        state = self.run_workflow(self.workflow(*({'type':'key','key':key} for key in names)))
        self.assertEqual(state.status, 'completed')
        self.assertEqual(self.requests(), [(('POST',self.pi_path('key')),{'key':'HOME' if k=='MOVE_HOME' else k}) for k in names])

    def test_variables_and_empty_optional_input_remain_compatible(self):
        workflow = self.workflow({'type':'input','source':'variable','variable':'message'},
                                 {'type':'input','source':'fixed','value':''})
        workflow['variables'] = [dict(name='message',label='Message',type='text',required=True,default=None)]
        state = self.run_workflow(workflow, {'message':'actual-value'})
        self.assertEqual(state.status, 'completed')
        self.assertEqual(self.requests(), [(('POST',self.pi_path('text')),{'text':'actual-value'})])

    def test_timeout_stops_run_without_retry_or_later_input(self):
        self.connection.getresponse.side_effect = TimeoutError()
        state = self.run_workflow(self.workflow({'type':'tap','x':.2,'y':.3}, {'type':'key','key':'HOME'}))
        self.assertEqual(state.status, 'failed')
        self.assertIn('Pi API /devices/phone_A/tap timed out', state.message)
        self.assertIn('Input may have occurred; no retry attempted', state.message)
        self.assertEqual(self.requests(), [(('POST',self.pi_path('tap')),{'x':.2,'y':.3})])
        self.assertEqual(self.android.mock_calls, [])
        self.assertIsNone(self.locks.owner(state.device_serial))

    def test_rejected_text_does_not_leak_payload_in_status_or_logs(self):
        response = self.connection.getresponse.return_value
        response.read.return_value = b'{"ok":false,"error":"cannot type private-value"}'
        service = WorkflowService(self.android, self.locks)
        run = service.start(self.workflow({'type':'input','source':'fixed','value':'private-value'}))
        service.runs[run['run_id']]['thread'].join(2)
        result = service.snapshot(run['run_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Pi rejected /devices/phone_A/text', result['message'])
        self.assertNotIn('private-value', json.dumps(result))
        self.connection.request.assert_called_once()

    def test_unsupported_actions_fail_without_guessing_an_endpoint(self):
        for key in ['RECENTS','SCREEN_OFF']:
            with self.subTest(key=key):
                state = self.run_workflow(self.workflow({'type':'key','key':key}, {'type':'key','key':'HOME'}))
                self.assertEqual(state.status, 'failed')
                self.assertIn('not supported', state.message)
        self.factory.assert_not_called()

    def test_manual_lock_and_takeover_share_the_pi_endpoint_identity(self):
        target = select_device(self.android)
        workflow = self.workflow({'type':'key','key':'HOME'}, {'type':'key','key':'BACK'})
        with self.locks.hold(target.serial, 'manual'):
            state = self.run_workflow(workflow)
            self.assertEqual(state.status, 'failed')
            self.assertIn('Device busy', state.message)
        self.factory.assert_not_called()
        claims = []
        def takeover(*args, **kwargs):
            claim = self.locks.request_takeover(target.serial)
            claims.append(claim)
        self.connection.request.side_effect = takeover
        try:
            state = self.run_workflow(workflow)
            self.assertEqual(state.status, 'cancelled')
            self.assertIn('takeover', state.message)
            self.assertEqual(self.requests(), [(('POST',self.pi_path('home')),None)])
        finally:
            for claim in claims:
                claim.close()

    def test_checkpoint_releases_lock_and_resume_needs_no_usb_or_status(self):
        service = WorkflowService(self.android, self.locks)
        run = service.start(self.workflow({'type':'manual_checkpoint','message':'Continue manually.'},
                                          {'type':'tap','x':.5,'y':.5}))
        run_id = run['run_id']
        deadline = time.monotonic() + 2
        while service.snapshot(run_id)['status'] != 'waiting_manual':
            if time.monotonic() > deadline:
                self.fail(str(service.snapshot(run_id)))
            time.sleep(.005)
        target = select_device(self.android)
        with self.locks.hold(target.serial, 'manual'):
            self.factory.assert_not_called()
        service.resume(run_id, 1)
        service.runs[run_id]['thread'].join(2)
        self.assertEqual(service.snapshot(run_id)['status'], 'passed')
        self.assertEqual(self.requests(), [(('POST',self.pi_path('tap')),{'x':.5,'y':.5})])
        self.assertEqual(self.android.mock_calls, [])

    def test_cancelled_run_sends_nothing(self):
        cancel = threading.Event()
        cancel.set()
        state = self.run_workflow(self.workflow({'type':'key','key':'HOME'}), cancel=cancel)
        self.assertEqual(state.status, 'cancelled')
        self.factory.assert_not_called()
        self.assertEqual(self.android.mock_calls, [])

    def test_screen_stays_appium_and_is_discovered_only_on_explicit_screen_read(self):
        context = AutomationContext(select_device(self.android), self.android, threading.Event(),
                                    self.logger, SimpleNamespace())
        context.input.tap(.5,.5)
        self.android.device.assert_not_called()
        usb = Device('usb-screen',1080,1920,0)
        self.android.device.side_effect = None
        self.android.device.return_value = usb
        context.screen.capture()
        self.android.screenshot.assert_called_once_with(usb)
        context.screen.inspect()
        self.android.dump_ui.assert_called_once_with(usb)
        self.assertEqual(backend_names(), ('raspberry_pi','appium'))
        with patch.dict(os.environ, PHONE_SCREEN_BACKEND='mock'):
            context = AutomationContext(select_device(self.android), self.android, threading.Event(),
                                        self.logger, SimpleNamespace())
            with self.assertRaises(UnsupportedOperation):
                context.screen.capture()

    def test_workflow_http_route_works_without_usb_and_reports_pi_backend(self):
        camera = Mock()
        app = create_app(camera, self.android, self.locks, self.storage, manual_backend='appium')
        client = app.test_client()
        authenticate_admin(client)
        headers = {'X-Control-Token':app.config['CONTROL_TOKEN'], 'X-Control-Client':'workflow-test'}
        workflow = self.storage.save(self.workflow({'type':'key','key':'HOME'}), create=True)
        names = client.get('/api/workflows').json
        self.assertEqual((names['input_backend'],names['screen_backend']), ('raspberry_pi','appium'))
        result = client.post(f"/api/workflows/{workflow['id']}/run", json={}, headers=headers)
        self.assertEqual(result.status_code,202,result.json)
        service = app.extensions['workflow_service']
        service.runs[result.json['run_id']]['thread'].join(2)
        self.assertEqual(service.snapshot(result.json['run_id'])['status'],'passed')
        self.assertEqual(self.android.mock_calls, [])
        self.assertEqual(self.requests(), [(('POST',self.pi_path('home')),None)])

    def test_appium_manual_input_does_not_inherit_pi_automation(self):
        self.android.device.side_effect = None
        self.android.device.return_value = Device('usb-manual',1080,1920,0)
        camera = Mock()
        camera.status.return_value = dict(ready=True,width=1280,height=720,index=0,error=None)
        app = create_app(camera, self.android, self.locks, self.storage, manual_backend='appium')
        self.addCleanup(app.extensions['control_state'].disable)
        client = app.test_client()
        authenticate_admin(client)
        headers = {'X-Control-Token':app.config['CONTROL_TOKEN'],'X-Control-Client':'manual-test'}
        self.assertEqual(client.get('/api/status',headers=headers).json['device']['serial'],'usb-manual')
        for path, data in [('/api/calibration', {'corners':[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],'frame_id':123}),
                           ('/api/control',{'enabled':True,'frame_id':123}),
                           ('/api/key',{'type':'key','value':'enter','frame_id':123}),
                           ('/api/action',{'action':'home','frame_id':123})]:
            response = client.post(path,json=data,headers=headers)
            self.assertEqual(response.status_code,200,response.json)
        self.android.send_key.assert_called_with(self.android.device.return_value,'key','enter')
        self.android.send.assert_called_with(self.android.device.return_value,'home',None)
        self.factory.assert_not_called()

    def test_legacy_recorder_stays_on_appium_when_playback_is_pi(self):
        self.android.device.side_effect = None
        self.android.device.return_value = Device('usb-recorder',1080,1920,0)
        self.android.dump_ui.return_value = (
            '<hierarchy><node class="android.widget.EditText" focused="true" '
            'password="false" resource-id="app:id/customer_name" '
            'hint="Customer Name" bounds="[10,10][300,90]" /></hierarchy>'
        )
        camera = Mock()
        camera.status.return_value = dict(ready=True,width=1280,height=720,index=0,error=None)
        app = create_app(camera,self.android,self.locks,self.storage,manual_backend='appium')
        self.addCleanup(app.extensions['control_state'].disable)
        client = app.test_client()
        authenticate_admin(client)
        headers = {'X-Control-Token':app.config['CONTROL_TOKEN'],'X-Control-Client':'recorder-test'}
        for path, data in [('/api/calibration',{'corners':[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],'frame_id':123}),
                           ('/api/control',{'enabled':True,'frame_id':123}),
                           ('/api/recorder/start',{'name':'Legacy recording','execute':True}),
                           ('/api/recorder/step',{'step':{'type':'input','source':'fixed','value':'hi'},'capture':True,'frame_id':123})]:
            response = client.post(path,json=data,headers=headers)
            self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['workflow']['steps'][0]['value'],'hi')
        self.assertEqual([call.args[2] for call in self.android.send_key.call_args_list],['h','i'])
        self.factory.assert_not_called()

    def test_manual_pi_setting_does_not_select_pi_automation(self):
        with patch.dict(os.environ, PHONE_INPUT_BACKEND='appium',PHONE_MANUAL_INPUT_BACKEND='raspberry_pi'):
            self.android.device.side_effect = None
            self.android.device.return_value = Device('usb-playback',1080,1920,0)
            state = self.run_workflow(self.workflow({'type':'key','key':'HOME'}))
            self.assertEqual(state.status,'completed')
            self.android.send.assert_called_with(self.android.device.return_value,'home')
            self.factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
