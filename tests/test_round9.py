"""Ninth round: the Windows native backend checks the user and the target window before every injected event, and a
drag always lets go of the mouse button. Everything is stubbed: no LINE window and no real input.
"""
import importlib.util
import os
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "line_desktop_mcp.py"
FAST_TIME = types.SimpleNamespace(sleep=lambda s: None, time=time.time, monotonic=time.monotonic)
TARGET, OTHER = 5, 9
DOWN, UP, WHEEL = 0x0002, 0x0004, 0x0800


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_KEY", "LINE_ACTION_LOG"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round9", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name == "nt", "the native backend is Windows-only")
class NativeInputGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        m = self.m = load_server(self.tmp.name)
        m.time = FAST_TIME
        self.sent, self.cursor = [], []
        self.front = [TARGET]                                    # the foreground window at each check (the last repeats)
        self.human = [False]                                     # is the user active at each check (the last repeats)
        self.checks = 0

        def front():
            self.checks += 1
            return self.front.pop(0) if len(self.front) > 1 else self.front[0]

        def human():
            return self.human.pop(0) if len(self.human) > 1 else self.human[0]
        m._foreground_hwnd = front
        m._human_active = human
        m._check_human = lambda: (_ for _ in ()).throw(m.Measurement("user active")) if human() else None
        m._native_windows = lambda include_iconic=False: [{"id": TARGET, "x": 0, "y": 0, "width": 767, "height": 722}]
        user32 = types.SimpleNamespace(SetCursorPos=lambda x, y: self.cursor.append((x, y)))
        m._w32 = lambda: {"user32": user32}
        m._real_input = lambda: None
        m._mouse_input = lambda flags, data=0: ("mouse", flags, data)
        m._key_input = lambda vk, up, ch=None: ("key", vk, up, ch)
        m._send = lambda inputs: self.sent.append(list(inputs))

    def tearDown(self):
        self.tmp.cleanup()

    def mouse_flags(self):
        return [item[1] for batch in self.sent for item in batch if item[0] == "mouse"]

    def drag(self):
        return self.m._native(["computer", "drag", "--window-id", str(TARGET), "--from-x", "10", "--from-y", "10",
                               "--to-x", "100", "--to-y", "100"])

    # ---- paste
    def stub_paste(self):
        m = self.m
        self.clip = []
        self.keys = []
        m._native_clip_has_other = lambda: False
        m._clip_text = lambda: "user text"
        m._native_clip_set = lambda text: self.clip.append(text)
        m._native_clip_seq = lambda: 1
        m._native_key = lambda key, hwnd=None, wait=True: self.keys.append(key)

    def test_paste_is_aborted_before_ctrl_v_when_the_foreground_changed_and_the_clipboard_is_restored(self):
        self.stub_paste()
        self.front = [OTHER]                                     # the user switched windows after the clipboard write
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m._native_paste("secret reply", TARGET)
        self.assertIn("no longer in front", str(ctx.exception))
        self.assertEqual(self.keys, [])                          # Ctrl+V never went out
        self.assertEqual(self.clip, ["secret reply", "user text"])    # written, then the user's text put back

    def test_paste_is_aborted_when_the_user_became_active(self):
        self.stub_paste()
        self.human = [True]
        with self.assertRaises(self.m.Measurement):
            self.m._native_paste("secret reply", TARGET)
        self.assertEqual(self.keys, [])
        self.assertEqual(self.clip[-1], "user text")

    def test_paste_goes_ahead_when_everything_is_in_order(self):
        self.stub_paste()
        self.m._native_paste("hello", TARGET)
        self.assertEqual(self.keys, ["ctrl+v"])
        self.assertGreaterEqual(self.checks, 1)

    def test_the_paste_verb_passes_the_target_window(self):
        self.stub_paste()
        seen = []
        self.m._native_paste = lambda text, hwnd=None: seen.append((text, hwnd))
        self.m._native(["computer", "paste-text", "--window-id", str(TARGET), "--text", "hi"])
        self.assertEqual(seen, [("hi", TARGET)])

    # ---- drag
    def test_a_drag_releases_the_button_when_the_foreground_changes_midway(self):
        self.front = [TARGET, TARGET, TARGET, OTHER]             # ok before the press, ok for two steps, then another window
        with self.assertRaises(self.m.Measurement):
            self.drag()
        flags = self.mouse_flags()
        self.assertEqual(flags[0], DOWN)
        self.assertEqual(flags[-1], UP)                          # always released
        self.assertEqual(flags.count(DOWN), flags.count(UP))
        self.assertLess(len(self.cursor), 11)                    # it did not finish the whole path

    def test_a_drag_releases_the_button_when_the_user_touches_the_mouse_midway(self):
        self.human = [False, False, False, True]                 # active from the third check on
        with self.assertRaises(self.m.Measurement):
            self.drag()
        flags = self.mouse_flags()
        self.assertEqual((flags[0], flags[-1]), (DOWN, UP))

    def test_a_drag_that_is_refused_before_the_press_never_presses(self):
        self.front = [OTHER]
        with self.assertRaises(self.m.Measurement):
            self.drag()
        self.assertEqual(self.mouse_flags(), [])

    def test_a_complete_drag_presses_moves_and_releases(self):
        self.drag()
        flags = self.mouse_flags()
        self.assertEqual(flags, [DOWN, UP])
        self.assertEqual(len(self.cursor), 11)                   # start point plus ten steps

    # ---- scroll, keys, typing, click
    def test_scroll_checks_before_every_notch(self):
        self.front = [TARGET, TARGET, OTHER]
        with self.assertRaises(self.m.Measurement):
            self.m._native(["computer", "scroll", "--window-id", str(TARGET), "--x", "50", "--y", "50", "--pages", "1"])
        self.assertEqual(self.mouse_flags(), [WHEEL, WHEEL][:len(self.mouse_flags())])
        self.assertLess(len(self.mouse_flags()), self.m.WHEEL_NOTCHES_PER_PAGE)

    def test_typing_checks_before_every_character(self):
        self.front = [TARGET, OTHER]
        with self.assertRaises(self.m.Measurement):
            self.m._native(["computer", "type-text", "--window-id", str(TARGET), "--text", "abc"])
        self.assertEqual(len(self.sent), 1)                      # only the first character went out

    def test_keys_are_checked_against_the_target_window(self):
        m = self.m
        m._split_key = lambda key: ([], key)
        m._w32 = lambda: {"user32": types.SimpleNamespace(VkKeyScanW=lambda ch: 0x41)}
        self.front = [OTHER]
        with self.assertRaises(m.Measurement):
            m._native(["computer", "press-key", "--window-id", str(TARGET), "--key", "a"])
        self.assertEqual(self.sent, [])
        self.front = [TARGET]
        m._native(["computer", "press-key", "--window-id", str(TARGET), "--key", "a"])
        self.assertEqual(len(self.sent), 1)

    def test_a_click_is_checked_after_the_cursor_moved(self):
        self.front = [OTHER]
        with self.assertRaises(self.m.Measurement):
            self.m._native(["computer", "click", "--window-id", str(TARGET), "--x", "5", "--y", "5"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.cursor, [(5, 5)])                  # the cursor moved, the button was never pressed

    def test_the_helper_is_the_only_pattern_used_in_the_native_verbs(self):
        text = SERVER.read_text(encoding="utf-8")
        body = text[text.index("def _native(args"):text.index("# ---- Direct control of LINE on Mac")]
        self.assertGreaterEqual(body.count("_guard_before_input("), 5)
        self.assertNotIn("_check_human()", body)                 # no more ad-hoc checks in the verbs themselves


if __name__ == "__main__":
    unittest.main()
