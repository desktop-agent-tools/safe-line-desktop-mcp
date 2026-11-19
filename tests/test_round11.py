"""Eleventh round: every input event the macOS backend injects is preceded by a user/front-app check, and the checks
after the first event of a step do not wait. Stubs only: no Mac, no LINE window, no real input.
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


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_KEY", "LINE_ACTION_LOG"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round11", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacInputGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        m = self.m = load_server(self.tmp.name)
        m.IS_MAC = True
        m.time = FAST_TIME
        self.js, self.waited, self.typed, self.clip = [], [], [], []
        self.front = [True]                      # is LINE frontmost at each check (the last value repeats)
        self.human = [False]                     # is the user active at each check (the last value repeats)

        def pop(seq):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        m._foreground_is_line = lambda: pop(self.front)
        m._human_active = lambda: pop(self.human)

        def check_human():
            self.waited.append(True)
            if pop(self.human):
                raise m.Measurement("user active")
        m._check_human = check_human
        m._mac_js = lambda *argv, **k: self.js.append(argv) or {}
        m._mac_key_parts = lambda key: (36, 0)
        m._mac_env = lambda: {}
        m.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: self.typed.append(a) or types.SimpleNamespace(returncode=0, stdout=b""),
            SubprocessError=Exception, TimeoutExpired=TimeoutError)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self):
        return [call[0] for call in self.js]

    def stub_paste(self):
        m = self.m
        self.store = {"t": "user text"}
        m._mac_clip_has_other = lambda: False
        m._mac_clip_text = lambda: self.store["t"]

        def set_text(text):
            self.clip.append(text)
            self.store["t"] = text
        m._mac_clip_set = set_text

    # ---- paste
    def test_paste_is_aborted_before_cmd_v_when_the_front_app_changes_and_the_clipboard_is_restored(self):
        self.stub_paste()
        self.front = [True, False]               # the outer check sees LINE, the inner one (right before Cmd+V) does not
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m._mac_paste("a reply")
        self.assertIn("no longer the frontmost", str(ctx.exception))
        self.assertEqual(self.events(), [])      # no key event reached the Mac
        self.assertEqual(self.clip, ["a reply", "user text"])      # written, then the user's text put back
        self.assertEqual(self.store["t"], "user text")

    def test_paste_is_aborted_when_the_user_becomes_active_between_the_two_checks(self):
        self.stub_paste()
        self.human = [False, True]               # the outer (waiting) check passes, the inner (non-waiting) one sees the user
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m._mac_paste("a reply")
        self.assertIn("Stopped midway", str(ctx.exception))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.waited, [True])    # only the outer check could wait
        self.assertEqual(self.store["t"], "user text")             # the clipboard is back

    def test_a_paste_in_good_order_sends_cmd_v(self):
        self.stub_paste()
        self.m._mac_paste("a reply")
        self.assertEqual(self.events(), ["key"])
        self.assertEqual(self.waited, [True])

    def test_a_key_press_checks_first_and_waits_only_when_asked(self):
        self.front = [False]
        with self.assertRaises(self.m.Measurement):
            self.m._mac_key("Return")
        self.assertEqual(self.events(), [])
        self.front = [True]
        self.m._mac_key("Return")
        self.m._mac_key("Return", wait=False)
        self.assertEqual(self.events(), ["key", "key"])
        self.assertEqual(len(self.waited), 2)    # the two waiting calls used _check_human; the wait=False call did not

    # ---- type, click, drag, scroll
    def test_typing_is_refused_when_the_front_app_is_not_line(self):
        self.front = [False]
        with self.assertRaises(self.m.Measurement):
            self.m._mac_type("@")
        self.assertEqual(self.typed, [])         # osascript never ran
        self.front = [True]
        self.m._mac_type("@")
        self.assertEqual(len(self.typed), 1)

    def mac_args(self, verb, *extra):
        return ["computer", verb, "--window-id", "7", *extra]

    def with_window(self):
        self.m._mac_windows = lambda: [{"id": 7, "x": 0, "y": 0, "width": 375, "height": 566, "title": "chat"}]

    def test_a_click_checks_before_the_move_and_again_before_the_button(self):
        self.with_window()
        self.front = [True, False]               # LINE is front before the move, another app is front after it
        with self.assertRaises(self.m.Measurement):
            self.m._mac(self.mac_args("click", "--x", "5", "--y", "5"))
        self.assertEqual(self.events(), ["move"])        # the cursor moved, the button was never pressed
        self.js.clear()
        self.front = [True]
        self.human = [False, True]               # the user touches the mouse after the move
        with self.assertRaises(self.m.Measurement):
            self.m._mac(self.mac_args("click", "--x", "5", "--y", "5"))
        self.assertEqual(self.events(), ["move"])
        self.js.clear()
        self.human = [False]
        self.m._mac(self.mac_args("click", "--x", "5", "--y", "5"))
        self.assertEqual(self.events(), ["move", "click"])

    def test_drag_and_scroll_check_before_the_event(self):
        self.with_window()
        self.front = [False]
        with self.assertRaises(self.m.Measurement):
            self.m._mac(self.mac_args("drag", "--from-x", "1", "--from-y", "1", "--to-x", "9", "--to-y", "9"))
        with self.assertRaises(self.m.Measurement):
            self.m._mac(self.mac_args("scroll", "--x", "5", "--y", "5"))
        self.assertEqual(self.events(), [])
        self.front = [True]
        self.m._mac(self.mac_args("drag", "--from-x", "1", "--from-y", "1", "--to-x", "9", "--to-y", "9"))
        self.m._mac(self.mac_args("scroll", "--x", "5", "--y", "5"))
        self.assertEqual(self.events(), ["drag", "scroll"])

    def test_every_injecting_call_in_the_mac_backend_is_guarded(self):
        text = SERVER.read_text(encoding="utf-8")
        body = text[text.index("def _mac_guard_before_input"):text.index("def _mac_capture")]
        self.assertIn("_mac_guard_before_input(wait)", body)                       # _mac_key
        self.assertIn('_mac_key("cmd+v", wait=False)', body)                       # the paste, non-waiting inner check
        mac_verbs = text[text.index("def _mac(args"):text.index("MAC_LAYOUT_KEYS = ")]
        for event in ('_mac_js("move"', '_mac_js("drag"', '_mac_js("scroll"'):
            at = mac_verbs.index(event)
            self.assertIn("_mac_guard_before_input(", mac_verbs[max(0, at - 200):at], event)
        click = mac_verbs.index('_mac_js("click"')
        self.assertIn("_mac_guard_before_input(wait=False)", mac_verbs[click - 300:click])
        self.assertNotIn("_check_human()", mac_verbs)                              # no ad-hoc checks left in the verbs

    def test_docs_state_the_remaining_limit_of_multi_clicks_and_drags(self):
        mac = (ROOT / "skills" / "line-desktop" / "references" / "mac.md").read_text(encoding="utf-8")
        self.assertIn("before every injected event", mac)
        self.assertIn("one helper call", mac)


if __name__ == "__main__":
    unittest.main()
