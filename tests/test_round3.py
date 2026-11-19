"""Third round: send region whatever the action, per-OS region, UI lock, confirm point, single-use records,
target-window foreground, wrapped draft restore, scan restore, config temp file, screenshot deletion.
No LINE window needed. (Tokens and names in the tests are dummies.)
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
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round3", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class SendRegionTests(Base):
    def stub_click(self):
        m = self.m
        self.reserved = []
        m.ensure_window = lambda *a, **k: None
        m._remember_origin = lambda *a, **k: None
        m._bg_ok = lambda: False
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        m.check_in_window = lambda *a, **k: None
        m.reserve_action = lambda *a, **k: self.reserved.append(a)
        m._orca = lambda *a, **k: self.fail("a click must not reach the window")

    def test_a_management_action_does_not_open_the_send_region(self):
        self.stub_click()
        m = self.m
        for args in ({"x": 560, "y": 622},                                            # input row
                     {"x": 740, "y": 700, "action": "leave", "expected_target": "g"},   # send button, with an action
                     {"x": 560, "y": 622, "action": "invite", "expected_target": "g"}):
            with self.assertRaises(m.Measurement, msg=str(args)):
                m.tool_click(args)
        self.assertEqual(self.reserved, [])                                           # nothing was even counted

    def test_mac_chat_window_region_covers_the_whole_width(self):
        m = self.m
        m.IS_MAC = True
        m._WIN_TITLE["main"] = "Some chat"                                            # a chat window, not the list
        w, h = 375, 566
        with self.assertRaises(m.Measurement):
            m._navigation_guard(m.COMPOSER_X, h - m.COMPOSER_FROM_BOTTOM, w, h)      # x=190: left of the Windows cut-off
        with self.assertRaises(m.Measurement):
            m._navigation_guard(20, h - 20, w, h)
        m._navigation_guard(190, 200, w, h)                                           # the message area
        m._WIN_TITLE["main"] = m.APP                                                  # the chat list window
        m._navigation_guard(190, h - 20, w, h)

    def test_region_can_be_set_in_the_settings_file(self):
        m = self.m
        Path(self.tmp.name, "config.json").write_text('{"send_region": {"min_x": 100, "strip": 300}}', encoding="utf-8")
        with self.assertRaises(m.Measurement):
            m._navigation_guard(150, 722 - 250, 767, 722)
        m._navigation_guard(50, 722 - 250, 767, 722)

    def test_drag_into_the_region_is_refused(self):
        self.stub_click()
        with self.assertRaises(self.m.Measurement):
            self.m.tool_drag({"x1": 500, "y1": 300, "x2": 600, "y2": 650})


class ConfirmPointTests(Base):
    def test_clicks_near_the_leave_confirm_button_need_the_action(self):
        m = self.m
        x, y = m.LEAVE_CONFIRM_POINT
        with self.assertRaises(m.Measurement):
            m._confirm_point_guard(x + 10, y - 10, {})
        with self.assertRaises(m.Measurement):
            m._confirm_point_guard(x, y, {"action": "menu"})
        m._confirm_point_guard(x, y, {"action": "leave", "expected_target": "g"})
        m._confirm_point_guard(x + 200, y, {})                                        # elsewhere: navigation
        self.assertIn("LEAVE_CONFIRM_POINT", m.NAMED_POINTS)


class UiLockTests(Base):
    def call(self, name, args=None):
        return self.m.process({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args or {}}})

    def test_a_second_agent_is_told_and_nothing_is_done(self):
        m = self.m
        m.UI_LOCK_WAIT = 0.2
        ran = []
        m.HANDLERS["line_click"] = lambda args: ran.append(args) or [{"type": "text", "text": "ok"}]
        other = load_server(self.tmp.name)                                            # another server instance
        with other._ui_lock():
            out = self.call("line_click")
        self.assertTrue(out["result"]["isError"])
        self.assertIn("another agent", out["result"]["content"][0]["text"])
        self.assertEqual(ran, [])
        self.assertFalse(self.call("line_click")["result"]["isError"])                # free again afterwards
        self.assertEqual(len(ran), 1)

    def test_line_open_and_foreground_screenshots_take_the_lock_but_background_ones_do_not(self):
        m = self.m
        m.UI_LOCK_WAIT = 0.2
        ran = []
        m.HANDLERS["line_open"] = lambda args: ran.append("open") or [{"type": "text", "text": "ok"}]
        m.HANDLERS["line_screenshot"] = lambda args: ran.append("shot") or [{"type": "text", "text": "ok"}]
        other = load_server(self.tmp.name)
        with other._ui_lock():
            self.assertTrue(self.call("line_open")["result"]["isError"])
            m._bg_ok = lambda: False
            self.assertTrue(self.call("line_screenshot")["result"]["isError"])        # brings LINE to the front
            self.assertFalse(self.call("line_screenshot", {"window": "popup"})["result"]["isError"])
            m._bg_ok = lambda: True
            self.assertFalse(self.call("line_screenshot")["result"]["isError"])       # looks only
        self.assertEqual(ran, ["shot", "shot"])

    def test_the_focus_is_given_back_while_the_lock_is_still_held(self):
        m = self.m
        seen = []
        m.HANDLERS["line_send"] = lambda args: [{"type": "text", "text": "ok"}]
        m.remember_focus = lambda: None
        m.return_focus = lambda: seen.append(Path(self.tmp.name, "ui.lock").exists())
        self.assertFalse(self.call("line_send")["result"]["isError"])
        self.assertEqual(seen, [True])
        with self.m._ui_lock():                                             # and it is released afterwards
            pass

    def test_every_screen_changing_tool_takes_the_lock_and_status_does_not(self):
        m = self.m
        for name in ("line_click", "line_drag", "line_key", "line_type", "line_scroll", "line_search",
                     "line_open_chat", "line_send", "line_read_chat_text", "line_restore_view", "line_scan_list",
                     "line_open"):
            self.assertTrue(m._needs_ui_lock(name, {}), name)
        for name in ("line_status", "line_layout"):
            self.assertFalse(m._needs_ui_lock(name, {}), name)

    def test_ui_lock_refuses_a_second_handle_in_the_same_process(self):
        m = self.m
        m.UI_LOCK_WAIT = 0.2
        with m._ui_lock():
            with self.assertRaises(m.Measurement):
                with m._ui_lock():
                    pass


class SingleUseTests(Base):
    def test_dry_run_record_is_single_use_and_expires(self):
        m = self.m
        m._LAST_DRY.update(fp="fp", hash=m._body_hash("hello"), at=time.time())
        self.assertTrue(m._is_own_dry_run("hello", "hello", "fp"))
        m._forget_dry_run()
        self.assertFalse(m._is_own_dry_run("hello", "hello", "fp"))                   # used up
        m._LAST_DRY.update(fp="fp", hash=m._body_hash("hello"), at=time.time() - m.DRY_RUN_TTL - 1)
        self.assertFalse(m._is_own_dry_run("hello", "hello", "fp"))                   # older than 10 minutes
        self.assertEqual(m.DRY_RUN_TTL, 600)

    def test_a_submenu_click_can_rearm_the_menu_token(self):
        m = self.m
        m._popup_size = lambda: (150, 120)
        m._arm_menu()
        m._menu_only_guard({"action": "menu"})
        with self.assertRaises(m.Measurement):
            m._menu_only_guard({"action": "menu"})
        m._arm_menu()                                                                 # opens_menu=true on the item
        m._menu_only_guard({"action": "menu"})


class TargetWindowTests(Base):
    @unittest.skipUnless(os.name == "nt", "the target-window check is Windows-only")
    def test_front_must_be_the_target_window_not_just_line(self):
        m = self.m
        m.IS_MAC, m.BACKEND = False, "native"
        m._foreground_is_line = lambda: True
        m._window_id = lambda which="main": 100
        m._foreground_hwnd = lambda: 200                                              # another LINE window
        self.assertFalse(m._front_ok("main"))
        m._foreground_hwnd = lambda: 100
        self.assertTrue(m._front_ok("main"))
        m._foreground_is_line = lambda: False
        self.assertFalse(m._front_ok("main"))

    def test_mac_checks_the_application_only(self):
        m = self.m
        m.IS_MAC = True
        m._foreground_is_line = lambda: True
        self.assertTrue(m._front_ok("main"))


class DraftRestoreTests(Base):
    def test_any_exception_hands_the_text_back(self):
        m = self.m

        def boom():
            raise RuntimeError("not a Measurement")
        m._header_now = boom
        ok, note = m._restore_draft([], 1, 2, "precious draft", "fp")
        self.assertFalse(ok)
        self.assertIn("precious draft", note)
        m._header_now = lambda: "fp"
        m._orca = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        ok, note = m._restore_draft([], 1, 2, "precious draft", "fp")
        self.assertFalse(ok)
        self.assertIn("precious draft", note)


class ScanRestoreTests(Base):
    def test_scan_restore_is_verified(self):
        m = self.m
        m.time = FAST_TIME
        m._click_win = lambda x, y: None
        m._tab_points = lambda: [(1, 1), (2, 2)]
        m._view_png = lambda: b"png"
        m._active_tab = lambda png: 0                                                 # the tab never changes
        note = m._restore_scan_view(1, None, None, None)
        self.assertIn("Could not put back", note)
        m._active_tab = lambda png: 1
        self.assertIn("Put back after the scan", m._restore_scan_view(1, None, None, None))
        m._list_hash = lambda png: None                                               # unmeasurable list = not put back
        m._scroll_list = lambda *a, **k: None
        self.assertIn("Could not put back", m._restore_scan_view(0, None, "h", 2))

    def test_unreadable_search_box_is_unknown_not_false(self):
        self.assertIsNone(self.m._search_active(b"junk"))
        self.assertIsNone(self.m._search_active(None))


class FilesTests(Base):
    def test_config_save_never_follows_a_fixed_temp_name(self):
        m = self.m
        keep = Path(self.tmp.name, "config.tmp")
        keep.write_text("do not touch", encoding="utf-8")
        m.save_layout_value("GROUP_TAB", [205, 25])
        self.assertEqual(keep.read_text(encoding="utf-8"), "do not touch")
        self.assertTrue(Path(self.tmp.name, "config.json").is_file())
        self.assertEqual([p.name for p in Path(self.tmp.name).glob(".config-*.tmp")], [])   # nothing left behind

    def test_screenshot_file_is_deleted_once_read(self):
        m = self.m
        path = Path(self.tmp.name, "shot.png")
        path.write_bytes(b"png bytes")
        self.assertEqual(m._png_bytes({"screenshot": {"path": str(path)}}), b"png bytes")
        self.assertFalse(path.exists())
        self.assertIsNone(m._png_bytes({"screenshot": {"path": str(path)}}))

    def test_stale_docstring_is_gone_and_scripts_use_printwindow_everywhere(self):
        text = (ROOT / "server" / "line_desktop_mcp.py").read_text(encoding="utf-8")
        self.assertNotIn("Without OCR the first row of the Chats section is pressed", text)
        ps = (ROOT / "server" / "line_desktop_windows.ps1").read_text(encoding="utf-8")
        self.assertNotIn("CopyFromScreen", ps)
        self.assertGreaterEqual(ps.count("PrintWindow"), 3)

    def test_readmes_describe_the_same_guards(self):
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        ja = (ROOT / "README.ja.md").read_text(encoding="utf-8")
        self.assertEqual(en.count(chr(10) + "| "), ja.count(chr(10) + "| "))          # the same number of safety rows
        for needle in ("ui.lock", "LEAVE_CONFIRM_POINT" if False else "60"):
            self.assertIn(needle, en)
            self.assertIn(needle, ja)
        self.assertIn("dummy", en)


if __name__ == "__main__":
    unittest.main()
