"""Host acceptance gates for real inkMOD GT911 -> SSD1677 navigation."""
import unittest
from probe_inkmod_native_touch import assess_native_touch, classify_sequence


class InkmodNativeTouchAcceptance(unittest.TestCase):
    def setUp(self):
        self.before={'frames':3,'frame_sha256':'a'*64}
        self.after={'frames':4,'frame_sha256':'b'*64}
        self.delta={'status-reads':132,'point-reads':1,
                    'status-clears':2,'injected-frames':2}
        self.inject={'x':{'ok':True},'y':{'ok':True},
                     'press':{'ok':True},'release':{'ok':True}}

    def test_verified_requires_guest_read_and_new_panel(self):
        self.assertTrue(assess_native_touch(
            self.before,self.after,self.delta,self.inject))

    def test_qmp_injection_without_i2c_point_is_not_navigation(self):
        delta={**self.delta,'point-reads':0}
        self.assertFalse(assess_native_touch(
            self.before,self.after,delta,self.inject))

    def test_no_new_frame_is_not_navigation(self):
        after={**self.after,'frames':3}
        self.assertFalse(assess_native_touch(
            self.before,after,self.delta,self.inject))
        after={**self.after,'frame_sha256':self.before['frame_sha256']}
        self.assertFalse(assess_native_touch(
            self.before,after,self.delta,self.inject))

    def test_missing_sha_or_status_ack_disallows_claim(self):
        self.assertFalse(assess_native_touch(
            {**self.before,'frame_sha256':None},
            self.after,self.delta,self.inject))
        self.assertFalse(assess_native_touch(
            self.before,self.after,{**self.delta,'status-clears':0},self.inject))
        self.assertFalse(assess_native_touch(
            self.before,self.after,{**self.delta,'injected-frames':1},self.inject))

    def test_rejected_press_disallows_claim(self):
        self.assertFalse(assess_native_touch(
            self.before,self.after,self.delta,
            {**self.inject,'press':{'ok':False}}))

    def test_partial_cannot_be_pass(self):
        full={'native_ui_navigation_verified':True}
        failed={'native_ui_navigation_verified':False}
        self.assertEqual(classify_sequence([]),'NOT_VERIFIED')
        self.assertEqual(classify_sequence([full,full]),'PASS')
        self.assertEqual(classify_sequence([full,failed]),'PARTIAL')
        self.assertEqual(classify_sequence([failed,failed]),'NOT_VERIFIED')


if __name__=='__main__':
    unittest.main()
