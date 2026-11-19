"""Fourth round: count refusal, scan restore after a failure, unknown search state, dry-run invalidation,
restore read-back, doc wording. No LINE window needed. (Names and tokens are dummies.)
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
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_MAX_PER_MINUTE", "LINE_SEND_MAX_PER_DAY", "LINE_ACTION_LOG",
                "LINE_ACTION_LOG_BODY", "LINE_MANAGE_MAX_PER_MINUTE", "LINE_MANAGE_MAX_PER_DAY", "LINE_SEND_KEY"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round4", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class CountTests(Base):
    def stub(self):
        m = self.m
        self.reserved = []
        m._base_tail = lambda *a, **k: []
        m.ensure_window = lambda *a, **k: None
        m._remember_origin = lambda *a, **k: None
        m._bg_ok = lambda: False
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        m.check_in_window = lambda *a, **k: None
        m.reserve_action = lambda *a, **k: self.reserved.append(a)
        self.clicks = []
        m._orca = lambda args, *a, **k: self.clicks.append(args)
        m._shot = lambda note, *a, **k: [{"type": "text", "text": note}]
        m.time = FAST_TIME

    def test_popup_clicks_and_management_clicks_must_be_single(self):
        self.stub()
        m = self.m
        m._arm_menu()
        m._popup_size = lambda: (150, 120)
        for args in ({"x": 5, "y": 5, "window": "popup", "action": "menu", "count": 2},
                     {"x": 5, "y": 5, "window": "popup", "action": "leave", "expected_target": "g", "count": 3},
                     {"x": 400, "y": 300, "action": "leave", "expected_target": "g", "count": 2}):
            with self.assertRaises(m.Measurement, msg=str(args)):
                m.tool_click(args)
        self.assertEqual(self.clicks, [])                       # nothing was clicked
        self.assertEqual(self.reserved, [])                     # nothing was counted
        self.assertEqual(m._MENU["uses"], 1)                    # and the menu token was not spent

    def test_single_clicks_still_work(self):
        self.stub()
        m = self.m
        m._arm_menu()
        m._popup_size = lambda: (150, 120)
        m.tool_click({"x": 5, "y": 5, "window": "popup", "action": "menu"})
        m.tool_click({"x": 400, "y": 300, "action": "leave", "expected_target": "g", "count": 1})
        self.assertEqual(len(self.clicks), 2)
        self.assertEqual(len(self.reserved), 1)
        m.tool_click({"x": 210, "y": 300, "count": 2})          # a plain double click in the chat list is navigation
        self.assertEqual(len(self.clicks), 3)


class ScanRestoreTests(Base):
    def test_foreground_scan_restores_the_view_when_it_fails_midway(self):
        m = self.m
        m.time = FAST_TIME
        m._bg_ok = lambda: False
        m.ensure_window = lambda *a, **k: None
        m._ready = lambda *a, **k: {}
        m._base_tail = lambda *a, **k: []
        m._state = lambda *a, **k: {}
        m._png_bytes = lambda state: b"png"
        m._active_tab = lambda png: 1
        m._search_active = lambda png: False
        m._list_hash = lambda png: "hash"
        m._require_front = lambda *a, **k: None
        calls = []

        def orca(args, *a, **k):
            if "scroll" in args and "down" in args:
                raise m.Measurement("the user took over")
            calls.append(args)
        m._orca = orca
        restored = []
        m._restore_scan_view = lambda *a, **k: restored.append(a) or "Put back after the scan: x. "
        with self.assertRaises(m.Measurement) as ctx:
            m.tool_scan_list({})
        self.assertEqual(len(restored), 1)
        self.assertIn("stopped midway", str(ctx.exception))
        self.assertIn("Put back after the scan", str(ctx.exception))

    def test_background_scan_restores_the_view_when_it_fails_midway(self):
        m = self.m
        m.time = FAST_TIME
        m._state = lambda *a, **k: {}
        m._window_id = lambda *a, **k: 1
        m._png_bytes = lambda state: b"png"
        m._active_tab = lambda png: 0
        m._search_active = lambda png: False
        m._list_hash = lambda png: "hash"
        restored = []
        m._restore_scan_view = lambda *a, **k: restored.append(k) or "Put back. "

        def orca(args, *a, **k):
            if "bg-scroll" in args:
                raise m.Measurement("window gone")
        m._orca = orca
        with self.assertRaises(m.Measurement):
            m._scan_list_background(3)
        self.assertEqual(len(restored), 1)

    def test_unknown_search_state_is_not_treated_as_empty(self):
        m = self.m
        m.time = FAST_TIME
        m._view_png = lambda: b"png"
        m._active_tab = lambda png: None
        note = m._restore_scan_view(None, None, None, None, search_was_active=None)
        self.assertIn("could not be measured", note)
        self.assertIn("Could not put back", note)
        self.assertNotIn("could not be measured", m._restore_scan_view(None, None, None, None, search_was_active=False))

    def test_origin_with_unknown_search_state_fails_the_restore_and_keeps_the_view(self):
        m = self.m
        m._view_png = lambda: b"png"
        m.header_fingerprint = lambda png: "fp"
        m._chat_open = lambda png: False
        m._active_tab = lambda png: 0
        m._search_active = lambda png: None
        m._list_hash = lambda png: "should not be taken"
        o = m._capture_origin(allow_fg=False)
        self.assertIsNone(o["search_active"])
        self.assertIsNone(o["list_hash"])
        self.assertTrue(o["notes"])
        m._restore_supported = lambda: True
        m.time = FAST_TIME
        m._ORIGIN["view"] = o
        self.assertIn("could not be measured", m._restore_and_keep(o))
        self.assertIs(m._ORIGIN.get("view"), o)

    def test_restored_search_text_is_read_back_and_compared(self):
        m = self.m
        m.time = FAST_TIME
        m._view_png = lambda: b"png"
        m._active_tab = lambda png: 0
        m._set_search_text = lambda text: None
        m._read_search_text = lambda: "abc"
        self.assertIn("Put back after the scan", m._restore_scan_view(0, "abc", None, None))
        m._read_search_text = lambda: "something else"                 # some text is not the same text
        self.assertIn("Could not put back", m._restore_scan_view(0, "abc", None, None))
        m._read_search_text = lambda: None
        self.assertIn("Could not put back", m._restore_scan_view(0, "abc", None, None))


class DryRunInvalidationTests(Base):
    def stub_send(self, draft):
        m = self.m
        m.BACKEND, m.IS_MAC = "native", False
        m.time = FAST_TIME
        m.ensure_window = lambda *a, **k: None
        m.require_same_chat = lambda ref=None: "chat-fp"
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        m._base_tail = lambda *a, **k: []
        m._require_front = lambda *a, **k: None
        m._header_now = lambda: "chat-fp"
        m._composer_draft = lambda tail: draft
        m._shot = lambda note, *a, **k: [{"type": "text", "text": note}]
        m._orca = lambda *a, **k: None
        m.reserve_action = lambda *a, **k: None
        m.check_limit = lambda kind: None

    def record(self, body):
        self.m._LAST_DRY.update(fp="chat-fp", hash=self.m._body_hash(body), at=time.time())

    def test_a_real_send_invalidates_the_record(self):
        self.stub_send(draft=None)
        self.record("hello")
        self.m.tool_send({"text": "hello", "expected_chat": "x"})
        self.assertIsNone(self.m._LAST_DRY["fp"])

    def test_replace_draft_invalidates_the_record(self):
        self.stub_send(draft=None)
        self.record("hello")
        self.m.tool_send({"text": "other", "expected_chat": "x", "dry_run": True, "replace_draft": True})
        self.assertNotEqual(self.m._LAST_DRY["hash"], self.m._body_hash("hello"))     # replaced by the new dry_run
        self.record("hello")
        self.m.tool_send({"text": "hello", "expected_chat": "x", "replace_draft": True})
        self.assertIsNone(self.m._LAST_DRY["fp"])

    def test_an_old_identical_text_in_the_box_is_then_a_user_draft(self):
        self.stub_send(draft="hello")
        self.record("hello")
        self.m.tool_send({"text": "hello", "expected_chat": "x"})                    # the first send used the record up
        self.assertIsNone(self.m._LAST_DRY["fp"])
        self.assertFalse(self.m._is_own_dry_run("hello", "hello", "chat-fp"))


class DocsTests(unittest.TestCase):
    def read(self, *parts):
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def test_docs_match_the_code(self):
        skill = self.read("skills", "line-desktop", "SKILL.md")
        self.assertNotIn("3 clicks", skill)
        self.assertIn("count=1", skill)
        helpers = self.read("skills", "line-desktop", "references", "windows-helpers.md")
        self.assertNotIn("screenshots can include other apps", helpers)
        self.assertIn("PrintWindow", helpers)
        header = self.read("server", "line_desktop_mcp.py")[:3000]
        self.assertNotIn("Input and sending are not restricted by the tool", header)
        mac = self.read("skills", "line-desktop", "references", "mac.md")
        self.assertIn("dry_run=true replace_draft=true", mac)
        code = self.read("server", "line_desktop_mcp.py")
        self.assertNotIn("nothing can be put on the clipboard between them", code)
        for readme in ("README.md", "README.ja.md"):
            self.assertIn("line_open", self.read(readme))


if __name__ == "__main__":
    unittest.main()
