"""Sixth round: owner-only private files (POSIX modes; Windows relies on the per-user profile), read_chat_text
restore around the whole body, scans that never clear a search whose text could not be read.
"""
import importlib.util
import os
import stat
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "line_desktop_mcp.py"
FAST_TIME = types.SimpleNamespace(sleep=lambda s: None, time=time.time, monotonic=time.monotonic)
POSIX = unittest.skipIf(os.name == "nt", "POSIX file modes (Windows uses the per-user profile ACL)")


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_MAX_PER_MINUTE", "LINE_SEND_MAX_PER_DAY", "LINE_ACTION_LOG",
                "LINE_ACTION_LOG_BODY", "LINE_MANAGE_MAX_PER_MINUTE", "LINE_MANAGE_MAX_PER_DAY", "LINE_SEND_KEY",
                "LINE_MCP_LOG_DIR"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round6", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name, "state")
        self.m = load_server(str(self.state))
        self.old_umask = os.umask(0o022) if os.name != "nt" else None

    def tearDown(self):
        if self.old_umask is not None:
            os.umask(self.old_umask)
        os.environ.pop("LINE_MCP_LOG_DIR", None)
        self.tmp.cleanup()


class PrivateStorageTests(Base):
    @POSIX
    def test_state_files_are_owner_only_even_with_a_permissive_umask(self):
        m = self.m
        m.reserve_action("send", "send", "Alice", "hi")
        m.save_layout_value("GROUP_TAB", [205, 25])
        with m._ui_lock():
            pass
        self.assertEqual(mode(self.state), 0o700)
        for name in ("actions.jsonl", "rate.jsonl", "config.json", "ui.lock", "actions.lock"):
            self.assertEqual(mode(self.state / name), 0o600, name)

    @POSIX
    def test_chat_logs_and_their_folder_are_owner_only(self):
        m = self.m
        folder = Path(self.tmp.name, "logs", "chats")
        os.environ["LINE_MCP_LOG_DIR"] = str(folder)
        path = m.save_chat_log("Alice", "text", now=1_700_000_000.0)
        self.assertEqual(mode(folder), 0o700)
        self.assertEqual(mode(path), 0o600)

    @POSIX
    def test_existing_wide_files_and_folders_are_tightened(self):
        self.state.mkdir(parents=True)
        os.chmod(self.state, 0o755)
        (self.state / "actions.jsonl").write_text("", encoding="utf-8")
        os.chmod(self.state / "actions.jsonl", 0o644)
        self.m.reserve_action("send", "send", "Alice", "hi")
        self.assertEqual(mode(self.state), 0o700)
        self.assertEqual(mode(self.state / "actions.jsonl"), 0o600)

    @POSIX
    def test_a_folder_that_cannot_be_made_private_is_refused(self):
        m = self.m
        self.state.mkdir(parents=True)
        os.chmod(self.state, 0o755)
        real = os.chmod
        os.chmod = lambda *a, **k: None                                       # the fix does not work
        try:
            with self.assertRaises(m.Measurement) as ctx:
                m._private_dir(self.state, "state folder")
        finally:
            os.chmod = real
        self.assertIn("can be read by other users", str(ctx.exception))

    def test_private_writes_go_through_the_owner_only_helpers(self):
        text = SERVER.read_text(encoding="utf-8")
        self.assertNotIn('path.open("a"', text)
        self.assertNotIn('path.open("x"', text)
        self.assertIn("0o600", text)
        self.assertIn("0o700", text)
        for readme in ("README.md", "README.ja.md"):
            body = (ROOT / readme).read_text(encoding="utf-8")
            self.assertIn("0700", body)

    def test_windows_keeps_the_profile_acl_and_the_docs_say_so(self):
        m = self.m
        if os.name == "nt":
            m._ensure_owner_only(Path(self.tmp.name), 0o700, "folder")        # a no-op, never raises
        self.assertIn("ACL", (ROOT / "README.md").read_text(encoding="utf-8"))


class ReadChatWholeBodyTests(Base):
    def stub(self):
        m = self.m
        m.time = FAST_TIME
        m.require_same_chat = lambda *a, **k: None
        m.ensure_window = lambda *a, **k: None
        m._remember_origin = lambda *a, **k: None
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        m._base_tail = lambda *a, **k: []
        m._require_front = lambda *a, **k: None
        m._clip_set = lambda text: None
        m._orca = lambda *a, **k: None
        self.restored = []
        m._ORIGIN["view"] = {"at": time.time()}
        m._restore_and_keep = lambda o, names=None: self.restored.append(names) or "View put back."

    def test_a_clipboard_refusal_before_the_copy_restores_the_view(self):
        self.stub()
        m = self.m
        m._clip_get = lambda: {"text": None, "has_other": True, "formats": []}
        with self.assertRaises(m.Measurement) as ctx:
            m.tool_read_chat_text({"expected_chat": "x"})
        self.assertEqual(len(self.restored), 1)
        self.assertIn("View put back.", str(ctx.exception))

    def test_a_log_save_failure_after_the_copy_restores_the_view(self):
        self.stub()
        m = self.m
        clip = {"t": "mine"}
        m._clip_get = lambda: {"text": clip["t"], "has_other": False, "formats": []}
        m._clip_set = lambda text: clip.__setitem__("t", "copied text" if (text or "").startswith("__LINE_MCP") else text)
        m._popup_size = lambda: (100, 100)
        m._clip_restore_if_unchanged = lambda *a, **k: True

        def broken_log(*a, **k):
            raise m.Measurement("disk full")
        m.save_chat_log = broken_log
        with self.assertRaises(m.Measurement) as ctx:
            m.tool_read_chat_text({"expected_chat": "x", "save_log": True})
        self.assertIn("disk full", str(ctx.exception))
        self.assertEqual(len(self.restored), 1)
        self.assertIn("View put back.", str(ctx.exception))

    def test_refusals_before_anything_is_done_do_not_touch_the_view(self):
        self.stub()
        with self.assertRaises(self.m.Measurement):
            self.m.tool_read_chat_text({"expected_chat": "Keep memo"})
        self.assertEqual(self.restored, [])


class UnreadableSearchTests(Base):
    def stub_common(self):
        m = self.m
        m.time = FAST_TIME
        m.ensure_window = lambda *a, **k: None
        m._ready = lambda *a, **k: {}
        m._base_tail = lambda *a, **k: []
        m._state = lambda *a, **k: {}
        m._window_id = lambda *a, **k: 1
        m._png_bytes = lambda state: None                                       # no pages: the loops end at once
        m._active_tab = lambda png: 0
        m._search_active = lambda png: True                                     # a search is in the box
        m._list_hash = lambda png: "hash"
        m._require_front = lambda *a, **k: None
        m._orca = lambda *a, **k: None
        self.cleared, self.restored = [], []
        m._set_search_text = lambda text: self.cleared.append(text)
        m._restore_scan_view = lambda *a, **k: self.restored.append(k) or "Put back. "

    def test_foreground_scan_leaves_an_unreadable_search_alone_and_says_so(self):
        self.stub_common()
        m = self.m
        m._bg_ok = lambda: False
        m._read_search_text = lambda: None
        out = m.tool_scan_list({})
        self.assertEqual(self.cleared, [])
        self.assertIn("NOT cleared", out[0]["text"])
        self.assertIn("filtered list", out[0]["text"])
        self.assertFalse(self.restored[0]["search_was_active"])                 # nothing to restore: the box was untouched

    def test_background_scan_leaves_an_unreadable_search_alone_and_says_so(self):
        self.stub_common()
        m = self.m
        m._bg_ok = lambda: True
        m._read_search_text = lambda: None
        out = m._scan_list_background(2)
        self.assertEqual(self.cleared, [])
        self.assertIn("NOT cleared", out[0]["text"])

    def test_a_readable_search_is_still_cleared_and_put_back(self):
        self.stub_common()
        m = self.m
        m._bg_ok = lambda: False
        m._read_search_text = lambda: "my search"
        m.tool_scan_list({})
        self.assertEqual(self.cleared, [""])
        self.assertTrue(self.restored[0]["search_was_active"] is True)


if __name__ == "__main__":
    unittest.main()
