"""Tenth round: the inner Ctrl+V check carries the target window and does not wait; every click of a multi-click
is checked again. Stubs only: no LINE window, no real input.
"""
import os
import types
import unittest

import test_round9 as r9

TARGET, OTHER = r9.TARGET, r9.OTHER


@unittest.skipUnless(os.name == "nt", "the native backend is Windows-only")
class InnerCheckTests(unittest.TestCase):
    setUp = r9.NativeInputGuardTests.setUp
    tearDown = r9.NativeInputGuardTests.tearDown
    mouse_flags = r9.NativeInputGuardTests.mouse_flags

    def real_key_path(self):
        """Keep the real _native_key (and its inner guard); stub only what talks to Windows."""
        m = self.m
        self.clip = []
        m._native_clip_has_other = lambda: False
        m._clip_text = lambda: "user text"
        m._native_clip_set = lambda text: self.clip.append(text)
        m._native_clip_seq = lambda: 1
        m._split_key = lambda key: (["CTRL"], "v")
        m._w32 = lambda: {"user32": types.SimpleNamespace(VkKeyScanW=lambda ch: 0x56, SetCursorPos=lambda x, y: None)}
        self.waits = []
        real_guard = m._guard_before_input

        def spy(hwnd=None, check_human=True, wait=True):
            self.waits.append(wait)
            return real_guard(hwnd, check_human, wait)
        m._guard_before_input = spy

    def test_the_foreground_changing_during_the_inner_check_stops_ctrl_v(self):
        self.real_key_path()
        self.front = [TARGET, OTHER]                  # outer check sees the target, the inner check sees another window
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m._native_paste("a reply", TARGET)
        self.assertIn("no longer in front", str(ctx.exception))
        self.assertEqual(self.sent, [])               # not a single key event went out
        self.assertEqual(self.clip, ["a reply", "user text"])          # and the clipboard is back

    def test_the_inner_check_carries_the_hwnd_and_does_not_wait(self):
        self.real_key_path()
        self.m._native_paste("a reply", TARGET)
        self.assertEqual(self.waits, [True, False])   # outer may wait for a pause, the inner one only stops
        self.assertEqual(len(self.sent), 1)           # Ctrl+V went out
        self.assertEqual(self.checks, 2)              # the foreground was read by both checks

    def test_user_activity_between_the_two_checks_stops_without_waiting(self):
        self.real_key_path()
        waited = []
        self.m._check_human = lambda: waited.append(True)        # the waiting check: must be used by the outer one only
        self.human = [True]                           # the outer (waiting) check is the stub above; the inner one sees the user
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m._native_paste("a reply", TARGET)
        self.assertEqual(waited, [True])
        self.assertIn("Stopped midway", str(ctx.exception))
        self.assertEqual(self.sent, [])

    def test_a_double_click_checks_again_before_the_second_click(self):
        self.real_key_path()
        self.front = [TARGET, OTHER]                  # the first guard passes, the one before click two fails
        with self.assertRaises(self.m.Measurement):
            self.m._native(["computer", "click", "--window-id", str(TARGET), "--x", "5", "--y", "5", "--click-count", "2"])
        self.assertEqual(len(self.sent), 1)           # only the first click went out
        self.assertEqual(self.waits.count(False), 1)  # the re-check before click two does not wait

    def test_user_activity_between_clicks_stops_a_triple_click(self):
        self.real_key_path()
        self.m._real_input = lambda: object()
        self.human = [False, False, True]
        with self.assertRaises(self.m.Measurement):
            self.m._native(["computer", "click", "--window-id", str(TARGET), "--x", "5", "--y", "5", "--click-count", "3"])
        self.assertEqual(len(self.sent), 2)

    def test_a_complete_double_click_still_works(self):
        self.real_key_path()
        self.m._native(["computer", "click", "--window-id", str(TARGET), "--x", "5", "--y", "5", "--click-count", "2"])
        self.assertEqual(len(self.sent), 2)

    def test_no_native_call_injects_input_without_the_target_window(self):
        text = r9.SERVER.read_text(encoding="utf-8")
        body = text[text.index("def _native(args"):text.index("# ---- Direct control of LINE on Mac")]
        for line in body.splitlines():
            if "_native_key(" in line or "_native_paste(" in line:
                self.assertIn('win["id"]', line, line)
        paste = text[text.index("def _native_paste"):text.index("def _native(args")]
        self.assertIn('_native_key("ctrl+v", hwnd, wait=False)', paste)


if __name__ == "__main__":
    unittest.main()
