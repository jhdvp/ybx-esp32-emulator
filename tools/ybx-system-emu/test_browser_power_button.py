"""Opt-in virtual Power API: command truth, watchdog and real HTTP contracts."""
import json
import threading
import time
import unittest
from unittest.mock import Mock

from serve_x4pro_browser import (
    PAGE, BadRequestError, FactoryX4Pro, GuestMissingError,
)
from test_serve_x4pro_browser import browser_guest, ServerHarnessMixin


def power_guest(**overrides):
    guest = browser_guest()
    guest.power_enabled = True
    guest.power_pressed = False
    guest.power_generation = 0
    guest.power_timer = None
    guest._relaunch = Mock()
    for key, value in overrides.items():
        setattr(guest, key, value)
    return guest


class PowerStateTests(unittest.TestCase):
    def test_disabled_by_default_and_never_sends_qmp(self):
        guest = power_guest(power_enabled=False)
        with self.assertRaises(BadRequestError):
            guest.power_update(True)
        self.assertFalse(guest.power_status()['available'])
        guest.qmp.assert_not_called()

    def test_single_press_and_release_use_board_property_not_reset(self):
        guest = power_guest()
        try:
            pressed = guest.power_update(True)
            self.assertTrue(pressed['pressed'])
            self.assertEqual(pressed['max_hold_ms'], 1200)
            self.assertTrue(pressed['virtual_only'])
            self.assertEqual(guest.qmp.call_args_list[0].args[0], 'qom-set')
            self.assertEqual(guest.qmp.call_args_list[0].args[1], {
                'path':'/machine/soc',
                'property':'ybx-x4pro-power-button-pressed', 'value': True})
            released = guest.power_update(False)
            self.assertFalse(released['pressed'])
            self.assertEqual([x.args[1]['value'] for x in guest.qmp.call_args_list],
                             [True, False])
        finally:
            guest._reset_power_state_locked()

    def test_repeated_press_cannot_extend_watchdog_deadline(self):
        guest = power_guest(POWER_MAX_HOLD_SECONDS=0.075)
        try:
            guest.power_update(True)
            timer = guest.power_timer
            guest.power_update(True)
            self.assertIs(guest.power_timer, timer)
            time.sleep(0.17)
            self.assertFalse(guest.power_status()['pressed'])
            self.assertEqual([x.args[1]['value'] for x in guest.qmp.call_args_list],
                             [True, False])
        finally:
            guest._reset_power_state_locked()

    def test_late_timer_cannot_release_next_press(self):
        guest = power_guest()
        try:
            guest.power_update(True)
            generation = guest.power_generation
            guest.power_update(False)
            guest.power_update(True)
            guest._power_watchdog(generation)
            self.assertTrue(guest.power_pressed)
            guest.power_update(False)
            self.assertEqual([x.args[1]['value'] for x in guest.qmp.call_args_list],
                             [True, False, True, False])
        finally:
            guest._reset_power_state_locked()

    def test_failed_release_relaunches_guest_not_fake_success(self):
        guest = power_guest()
        try:
            guest.qmp.side_effect = [{'return': {}}, {'error': 'QMP failed'}]
            guest.power_update(True)
            with self.assertRaises(GuestMissingError):
                guest.power_update(False)
            self.assertFalse(guest.power_pressed)
            guest._relaunch.assert_called_once()
        finally:
            guest._reset_power_state_locked()

    def test_failed_press_recovers_from_ambiguous_qmp_state(self):
        guest = power_guest()
        guest.qmp.return_value = {'error': 'lost'}
        with self.assertRaises(GuestMissingError):
            guest.power_update(True)
        guest._relaunch.assert_called_once()
        self.assertFalse(guest.power_pressed)

    def test_reboot_releases_before_qemu_system_reset(self):
        guest = power_guest()
        try:
            guest.power_update(True)
            guest.reboot()
            calls = guest.qmp.call_args_list
            commands = [x.args[0] for x in calls]
            self.assertIn('system_reset', commands)
            idx = commands.index('system_reset')
            self.assertEqual(calls[idx-1].args[1], {
                'path':'/machine/soc',
                'property':'ybx-touch-y', 'value':0})
            self.assertTrue(any(c.args[1].get('property') ==
                                'ybx-x4pro-power-button-pressed' and
                                c.args[1]['value'] is False
                                for c in calls[:idx]))
            self.assertFalse(guest.power_pressed)
        finally:
            guest._reset_power_state_locked()

    def test_invalid_input_never_touches_guest(self):
        guest = power_guest()
        for bad in (1,'true',{},None):
            with self.subTest(bad=bad), self.assertRaises(BadRequestError):
                guest.power_update(bad)
        guest.qmp.assert_not_called()

    def test_page_declares_virtual_not_physical_and_implements_browser_safety(self):
        for item in ('id="powerbox" hidden','id="power"',
                     "power.addEventListener('pointerdown'",
                     "'pointercancel','lostpointercapture'",
                     "window.addEventListener('blur'",
                     "document.addEventListener('visibilitychange'",
                     "window.addEventListener('pagehide'",
                     "navigator.sendBeacon('/api/power'",
                     "keepalive:!pressed",'Experimental X4 Pro GPIO3'):
            self.assertIn(item,PAGE)


class PowerHttpTests(ServerHarnessMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.guest.power_calls=[]
        self.guest.power_available=False

        def update(pressed):
            if not self.guest.power_available:
                raise BadRequestError('Power disabled')
            self.guest.power_calls.append(pressed)
            return {'available':True,'pressed':pressed,
                    'max_hold_ms':1200,'virtual_only':True}
        self.guest.power_update=update

    def call(self, payload, content_type='application/json'):
        return self.json_request('POST','/api/power',
             body=json.dumps(payload).encode(),
             headers={'Content-Type':content_type})

    def test_default_disabled_fails_closed(self):
        code,data=self.call({'pressed':True})
        self.assertEqual(code,400)
        self.assertEqual(data['type'],'bad-request')
        self.assertEqual(self.guest.power_calls,[])

    def test_valid_request_reaches_guest_boolean_and_release(self):
        self.guest.power_available=True
        self.assertEqual(self.call({'pressed':True})[0],200)
        status,result=self.call({'pressed':False})
        self.assertEqual(status,200)
        self.assertEqual(result['action'],'virtual-power')
        self.assertEqual(self.guest.power_calls,[True,False])

    def test_invalid_json_types_and_unexpected_keys_rejected(self):
        self.guest.power_available=True
        for payload in ([],{}, {'pressed':'true'}, {'pressed':1},
                        {'pressed':False,'duration_ms':1500}):
            with self.subTest(payload=payload):
                code,result=self.call(payload)
                self.assertEqual(code,400)
                self.assertEqual(result['type'],'bad-request')
        self.assertEqual(self.guest.power_calls,[])

    def test_content_type_and_large_body_rejected(self):
        self.guest.power_available=True
        self.assertEqual(self.call({'pressed':True},'text/plain')[0],400)
        code,result=self.json_request('POST','/api/power',
             body=b'x'*65,headers={'Content-Type':'application/json'})
        self.assertEqual(code,400)
        self.assertEqual(self.guest.power_calls,[])

    def test_state_can_report_power_enabled_without_claiming_physical_reader(self):
        guest=power_guest()
        try:
            guest._get=Mock(return_value=2)
            guest.frame = 2
            guest.png_file=Mock()
            guest.firmware_dir=guest.firmware_path.parent
            guest.rom_name='factory.bin'
            guest.rom_sha='a'*64
            state=guest.state()
            self.assertTrue(state['power']['available'])
            self.assertTrue(state['power']['virtual_only'])
            self.assertFalse(state['physical_device'])
        finally:
            guest._reset_power_state_locked()


if __name__=='__main__':
    unittest.main()
