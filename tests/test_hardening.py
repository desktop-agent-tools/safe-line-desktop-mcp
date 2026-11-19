"""Tests for the send path, clipboard, action log, HTTP guards and restore-view memory. No LINE window needed.

Run: python -m unittest discover -s tests
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server" / "line_desktop_mcp.py"
CR = chr(13)


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_MAX_PER_MINUTE", "LINE_SEND_MAX_PER_DAY", "LINE_ACTION_LOG",
                "LINE_ACTION_LOG_BODY", "LINE_MANAGE_MAX_PER_MINUTE", "LINE_MANAGE_MAX_PER_DAY"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_hardening", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class SendPathTests(Base):
    """Return-family keys, mentions, drafts, header check: all refused before anything is touched."""

    def test_line_key_refuses_return_family_in_main_window(self):
        for key in ("Return", "enter", "alt+Return", "ctrl+Return", "shift+Return", "ctrl+enter"):
            with self.assertRaises(self.m.Measurement, msg=key):
                self.m.tool_key({"key": key})
        with self.assertRaises(self.m.Measurement):
            self.m.tool_key({"key": "Return", "action": "leave", "expected_target": "Old trip"})   # main window

    def test_line_key_refuses_configured_send_key(self):
        self.m.SEND_KEY = "ctrl+shift+k"
        self.assertTrue(self.m.is_return_family("ctrl+shift+k"))
        with self.assertRaises(self.m.Measurement):
            self.m.tool_key({"key": "ctrl+shift+k"})

    def test_line_key_popup_return_needs_management_action_and_popup(self):
        with self.assertRaises(self.m.Measurement):                      # no action
            self.m.tool_key({"key": "Return", "window": "popup"})
        with self.assertRaises(self.m.Measurement):                      # action=menu is not a confirm action
            self.m.tool_key({"key": "Return", "window": "popup", "action": "menu"})
        with self.assertRaises(self.m.Measurement):                      # modified Return is a send key
            self.m.tool_key({"key": "alt+Return", "window": "popup", "action": "leave", "expected_target": "g"})
        self.m._popup_size = lambda: None                                  # no popup is open
        with self.assertRaises(self.m.Measurement):
            self.m.tool_key({"key": "Return", "window": "popup", "action": "leave", "expected_target": "g"})
        self.m._popup_size = lambda: (300, 200)
        self.assertEqual(self.m._return_key_guard("Return", {"window": "popup", "action": "leave",
                                                            "expected_target": "g"}), "leave")

    def test_line_key_other_keys_not_blocked(self):
        self.assertFalse(self.m.is_return_family("Escape"))
        self.assertFalse(self.m.is_return_family("ctrl+a"))
        self.assertEqual(self.m._return_key_guard("Escape", {}), "")

    def test_line_type_refuses_control_characters(self):
        with self.assertRaises(self.m.Measurement):
            self.m.tool_type({"text": "hi" + CR})

    def test_menu_action_needs_a_fresh_menu_and_a_small_popup(self):
        self.m._popup_size = lambda: (150, 120)
        with self.assertRaises(self.m.Measurement):                       # nobody opened a menu
            self.m._menu_only_guard({"action": "menu"})
        self.m._arm_menu()
        self.m._popup_size = lambda: (400, 300)                           # a dialog-sized popup
        with self.assertRaises(self.m.Measurement):
            self.m._menu_only_guard({"action": "menu"})
        self.m._popup_size = lambda: None
        with self.assertRaises(self.m.Measurement):
            self.m._menu_only_guard({"action": "menu"})
        self.m._popup_size = lambda: (150, 120)
        self.m._menu_only_guard({"action": "menu"})                       # exactly one click ...
        with self.assertRaises(self.m.Measurement):                       # ... then the token is gone
            self.m._menu_only_guard({"action": "menu"})
        self.m._arm_menu()
        self.m._MENU["at"] -= 100                                         # and it expires
        with self.assertRaises(self.m.Measurement):
            self.m._menu_only_guard({"action": "menu"})

    def test_manage_guard_records_before_acting(self):
        self.m._manage_guard({"action": "leave", "expected_target": "Old trip"})
        rows = self.m.action_log_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(rows[0])["target"], "Old trip")

    def test_mentions_need_a_user_declared_modified_send_key(self):
        for key in ("Return", "alt+Return"):                              # a per-call send_key never counts
            with self.assertRaises(self.m.Measurement) as ctx:
                self.m.tool_send({"text": "hi @[Ann]", "expected_chat": "x", "dry_run": True, "send_key": key})
            self.assertIn("declared", str(ctx.exception))
        self.assertIsNone(self.m.declared_send_key())
        self.m.save_layout_value("SEND_KEY", "Return")                    # declared, but a plain Return
        self.assertIsNone(self.m.declared_send_key())
        self.m.save_layout_value("SEND_KEY", "alt+Return")
        self.assertEqual(self.m.declared_send_key().lower(), "alt+return")

    def test_unreadable_draft_backend_refuses_without_replace_draft(self):
        self.m.IS_MAC = True
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m.tool_send({"text": "hi", "expected_chat": "x"})
        self.assertIn("replace_draft", str(ctx.exception))
        with self.assertRaises(self.m.Measurement):                       # dry_run overwrites the box too
            self.m.tool_send({"text": "hi", "expected_chat": "x", "dry_run": True})

    def test_header_check_fails_closed_when_fingerprint_unknown(self):
        m = self.m
        m._bg_ok = lambda: True
        m._state = lambda *a, **k: {}
        m._png_bytes = lambda state: b"not a png"
        m._LAST_HEADER["fp"] = "abc"
        with self.assertRaises(m.Measurement) as ctx:
            m.require_same_chat()
        self.assertIn("could not be read", str(ctx.exception))
        m.header_fingerprint = lambda png: "other"
        with self.assertRaises(m.Measurement):
            m.require_same_chat("abc")                                    # second check against the start value
        m.header_fingerprint = lambda png: "abc"
        self.assertEqual(m.require_same_chat("abc"), "abc")


class ClipboardTests(Base):
    def test_native_paste_refuses_non_text_clipboard_before_writing(self):
        m, calls = self.m, []
        m._native_clip_has_other = lambda: True
        m._native_clip_set = lambda text: calls.append(text)
        with self.assertRaises(m.Measurement):
            m._native_paste("hello")
        self.assertEqual(calls, [])

    def test_native_paste_restores_text_even_when_the_paste_fails(self):
        m, calls = self.m, []
        m._native_clip_has_other = lambda: False
        m._clip_text = lambda: "user text"
        m._native_clip_set = lambda text: calls.append(text)
        m._native_clip_seq = lambda: 7                                    # nobody touched the clipboard

        def boom(_key, *a, **k):
            raise m.Measurement("no window")
        m._native_key = boom
        with self.assertRaises(m.Measurement):
            m._native_paste("hello")
        self.assertEqual(calls, ["hello", "user text"])

    def test_native_paste_leaves_a_clipboard_the_user_changed_and_clears_an_empty_one(self):
        m, calls = self.m, []
        m._native_clip_has_other = lambda: False
        m._clip_text = lambda: "user text"
        m._native_clip_set = lambda text: calls.append(text)
        m._native_key = lambda key, *a, **k: None
        seqs = iter([7, 8])                                               # the number moved: the user copied something
        m._native_clip_seq = lambda: next(seqs)
        m._native_paste("hello")
        self.assertEqual(calls, ["hello"])
        calls.clear()
        cleared = []
        m._clip_text = lambda: None                                       # it was empty before
        m._native_clip_clear = lambda: cleared.append(True)
        m._native_clip_seq = lambda: 3
        m._native_paste("hello")
        self.assertEqual((calls, cleared), (["hello"], [True]))

    def test_mac_paste_refuses_non_text_and_restores_only_if_unchanged(self):
        m, calls, clip = self.m, [], {"t": "mine"}
        m._mac_clip_has_other = lambda: True
        m._mac_clip_set = lambda text: calls.append(text)
        with self.assertRaises(m.Measurement):
            m._mac_paste("hello")
        self.assertEqual(calls, [])
        m._mac_clip_has_other = lambda: False
        m._mac_clip_text = lambda: clip["t"]
        m._mac_guard_before_input = lambda wait=True: None

        def setter(text):
            calls.append(text)
            clip["t"] = text
        m._mac_clip_set = setter

        def boom(_key, *a, **k):
            raise m.Measurement("no key")
        m._mac_key = boom
        with self.assertRaises(m.Measurement):
            m._mac_paste("hello")
        self.assertEqual(calls, ["hello", "mine"])
        calls.clear()
        clip["t"] = "mine"

        def user_copies(_key, *a, **k):
            clip["t"] = "something new the user copied"
            raise m.Measurement("no key")
        m._mac_key = user_copies
        with self.assertRaises(m.Measurement):
            m._mac_paste("hello")
        self.assertEqual(calls, ["hello"])                                # the user's newer copy is left alone


class ActionLogTests(Base):
    def test_unwritable_log_refuses(self):
        Path(self.tmp.name, "actions.jsonl").mkdir()                       # a directory where the file should be
        with self.assertRaises(self.m.Measurement):
            self.m.record_action("send", "send", "Alice", "hi")
        with self.assertRaises(self.m.Measurement):
            self.m.reserve_action("send", "send", "Alice", "hi")

    def test_log_off_still_counts_in_memory(self):
        os.environ["LINE_ACTION_LOG"] = "0"
        try:
            self.m.reserve_action("send", "send", "Alice", "hi")
            self.assertFalse(self.m.action_log_path().exists())
            self.assertEqual(len(self.m._recent("send", time.time())), 1)
        finally:
            os.environ.pop("LINE_ACTION_LOG", None)

    def test_reserve_checks_limit_then_records(self):
        for _ in range(5):
            self.m.reserve_action("send", "send", "Alice", "hi")
        with self.assertRaises(self.m.Measurement):
            self.m.reserve_action("send", "send", "Alice", "hi")
        self.assertEqual(len(self.m.action_log_path().read_text(encoding="utf-8").splitlines()), 5)

    def test_lock_blocks_a_second_holder_and_is_released(self):
        with self.m._action_lock(timeout=1):
            with self.assertRaises(self.m.Measurement):
                with self.m._action_lock(timeout=0.2):
                    pass
        with self.m._action_lock(timeout=1):                              # free again; the lock file is kept, not deleted
            pass
        self.assertTrue(Path(self.tmp.name, "actions.lock").exists())

    def test_an_old_leftover_lock_file_does_not_block(self):
        lock = Path(self.tmp.name, "actions.lock")
        lock.write_text("x")                                              # a file alone is not a lock
        with self.m._action_lock(timeout=0.5):
            pass

    def test_nothing_is_written_inside_a_git_working_tree(self):
        repo = Path(self.tmp.name, "repo")
        (repo / ".git").mkdir(parents=True)
        m = load_server(str(repo / "state"))
        with self.assertRaises(m.Measurement):
            m.record_action("send", "send", "Alice", "hi")
        with self.assertRaises(m.Measurement):
            m.save_layout_value("GROUP_TAB", [205, 25])
        self.assertFalse((repo / "state").exists())

    def test_gitignore_covers_outputs(self):
        text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
        for name in ("actions.jsonl", "actions.lock", "chat-logs/"):
            self.assertIn(name, text)


class HttpGuardTests(Base):
    TOKEN = "x7Kq2mP9vR4tW8yB3nL6jH5fD1sG0aZc"

    def start(self):
        import threading
        server = self.m.make_http_server("127.0.0.1", 0, self.TOKEN)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def post(self, port, headers):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        conn.request("POST", "/mcp", body, headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_any_origin_header_is_refused_even_from_localhost(self):
        port = self.start()
        good = {"Authorization": "Bearer " + self.TOKEN, "Content-Type": "application/json"}
        self.assertEqual(self.post(port, good), 200)
        for origin in ("http://localhost:3000", "http://127.0.0.1", "https://example.com", "null"):
            self.assertEqual(self.post(port, {**good, "Origin": origin}), 403, origin)

    def test_connection_cap_and_timeout(self):
        import socket
        self.m.HTTP_MAX_CONNECTIONS = 2
        self.m.HTTP_SOCKET_TIMEOUT = 0.5
        port = self.start()
        held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.assertIn(b"503", extra.recv(100))                           # over the cap
        extra.close()
        # idle connections are dropped after the socket timeout, which frees their slots
        self.assertEqual(held[0].recv(10), b"")
        for sock in held:
            sock.close()
        time.sleep(0.3)
        good = {"Authorization": "Bearer " + self.TOKEN, "Content-Type": "application/json"}
        self.assertEqual(self.post(port, good), 200)


class RestoreViewKeepTests(Base):
    def test_failed_restore_keeps_the_remembered_view(self):
        m = self.m
        view = {"at": time.time()}
        m._ORIGIN["view"] = view
        m._restore_origin = lambda o, extra=None: "Could not put back: the tab."
        note = m._restore_and_keep(view)
        self.assertIs(m._ORIGIN.get("view"), view)
        self.assertIn("kept", note)
        m._restore_origin = lambda o, extra=None: "View put back: the tab."
        m._restore_and_keep(view)
        self.assertNotIn("view", m._ORIGIN)


if __name__ == "__main__":
    unittest.main()
