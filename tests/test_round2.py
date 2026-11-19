"""Second round of hardening tests: click regions, Space, drafts, clipboard formats, counter file, links,
restore-view verification. No LINE window needed.
"""
import base64
import importlib.util
import json
import os
import tempfile
import time
import subprocess
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "line_desktop_mcp.py"


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_MAX_PER_MINUTE", "LINE_SEND_MAX_PER_DAY", "LINE_ACTION_LOG",
                "LINE_ACTION_LOG_BODY", "LINE_MANAGE_MAX_PER_MINUTE", "LINE_MANAGE_MAX_PER_DAY", "LINE_SEND_KEY"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round2", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAST_TIME = types.SimpleNamespace(sleep=lambda s: None, time=time.time, monotonic=time.monotonic)


def fake_subprocess(run):
    """A stand-in for the server module's subprocess (the real module is not touched)."""
    return types.SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError, PIPE=subprocess.PIPE,
                                 TimeoutExpired=subprocess.TimeoutExpired)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class ClickRegionTests(Base):
    def test_bottom_strip_of_the_chat_pane_is_refused(self):
        m = self.m
        w, h = 767, 722
        with self.assertRaises(m.Measurement):
            m._navigation_guard(560, h - m.COMPOSER_FROM_BOTTOM, w, h)      # input row
        with self.assertRaises(m.Measurement):
            m._navigation_guard(740, h - 40, w, h)                          # send button corner
        m._navigation_guard(210, h - 40, w, h)                              # chat list column (any height)
        m._navigation_guard(560, 350, w, h)                                 # chat area above the composer
        m._navigation_guard(90, 24, w, h)                                   # tab bar
        m._navigation_guard(m.CHAT_MENU_BUTTON[0], m.CHAT_MENU_BUTTON[1], w, h)

    def test_drag_into_the_strip_is_refused(self):
        m = self.m
        m.ensure_window = lambda *a, **k: None
        m._remember_origin = lambda *a, **k: None
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        with self.assertRaises(m.Measurement):
            m.tool_drag({"x1": 500, "y1": 300, "x2": 560, "y2": 630})


class SpaceKeyTests(Base):
    def test_space_is_treated_like_return(self):
        m = self.m
        self.assertTrue(m.is_return_family("space"))
        self.assertTrue(m.is_return_family("ctrl+Space"))
        with self.assertRaises(m.Measurement):
            m.tool_key({"key": "space"})
        with self.assertRaises(m.Measurement):
            m.tool_key({"key": "space", "window": "popup"})                 # no management action
        m._popup_size = lambda: (300, 200)
        self.assertEqual(m._return_key_guard("space", {"window": "popup", "action": "invite",
                                                       "expected_target": "g"}), "invite")


class DraftTests(Base):
    def test_only_our_recorded_dry_run_counts_as_ours(self):
        m = self.m
        self.assertFalse(m._is_own_dry_run("hello", "hello", "fp1"))          # no dry_run was ever made
        m._LAST_DRY.update(fp="fp1", hash=m._body_hash("hello"), at=time.time())
        self.assertTrue(m._is_own_dry_run("hello", "hello", "fp1"))
        self.assertFalse(m._is_own_dry_run("hello", "hello", "fp2"))          # another chat
        self.assertFalse(m._is_own_dry_run("hel lo!", "hello", "fp1"))        # different text
        self.assertFalse(m._is_own_dry_run("hello", "hello", None))           # chat unknown

    def test_draft_is_not_pasted_into_another_chat(self):
        m, calls = self.m, []
        m._orca = lambda args, *a, **k: calls.append(args)
        m._header_now = lambda: "other-chat"
        ok, note = m._restore_draft([], 1, 2, "my draft", "original-chat")
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertIn("my draft", note)
        m._header_now = lambda: None
        self.assertFalse(m._restore_draft([], 1, 2, "my draft", "original-chat")[0])
        self.assertFalse(m._restore_draft([], 1, 2, "my draft", None)[0])
        m._header_now = lambda: "original-chat"
        self.assertTrue(m._restore_draft([], 1, 2, "my draft", "original-chat")[0])
        self.assertTrue(any("paste-text" in c for c in calls))

    def stub_send(self, press_fails=False, paste_fails=False):
        m = self.m
        self.calls = []
        m.BACKEND, m.IS_MAC = "native", False
        m.time = FAST_TIME
        m.ensure_window = lambda *a, **k: None
        m.require_same_chat = lambda ref=None: "chat-fp"
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        m._base_tail = lambda *a, **k: []
        m._require_front = lambda *a, **k: None
        m._header_now = lambda: "chat-fp"
        m._composer_draft = lambda tail: "the user draft"
        m._shot = lambda note, *a, **k: [{"type": "text", "text": note}]
        m.reserve_action = lambda *a, **k: self.calls.append("reserve")

        def orca(args, *a, **k):
            self.calls.append(args)
            if paste_fails and "paste-text" in args and "my message" in args:
                raise m.Measurement("paste failed")
            if press_fails and "hotkey" in args and "alt+Return" in args:
                raise m.Measurement("key failed")
        m._orca = orca

    def pasted_drafts(self):
        return [c for c in self.calls if isinstance(c, list) and "paste-text" in c and "the user draft" in c]

    def test_failure_before_the_key_restores_the_draft_and_returns_its_text(self):
        self.stub_send(paste_fails=True)
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m.tool_send({"text": "my message", "expected_chat": "x"})
        self.assertIn("the user draft", str(ctx.exception))
        self.assertEqual(len(self.pasted_drafts()), 1)

    def test_failure_of_the_send_key_is_unknown_and_never_pastes_the_draft_back(self):
        self.stub_send(press_fails=True)
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m.tool_send({"text": "my message", "expected_chat": "x"})
        self.assertIn("UNKNOWN", str(ctx.exception))
        self.assertIn("the user draft", str(ctx.exception))
        self.assertEqual(self.pasted_drafts(), [])

    def test_successful_send_puts_the_draft_back_only_in_the_same_chat(self):
        self.stub_send()
        self.m.tool_send({"text": "my message", "expected_chat": "x"})
        self.assertEqual(len(self.pasted_drafts()), 1)
        self.stub_send()
        self.m._header_now = lambda: "different"
        out = self.m.tool_send({"text": "my message", "expected_chat": "x"})
        self.assertEqual(self.pasted_drafts(), [])
        self.assertIn("the user draft", out[0]["text"])


class ClipboardFormatTests(Base):
    def test_rich_or_unknown_formats_count_as_non_text(self):
        m = self.m
        m.IS_MAC = False

        def fake_ps(formats):
            b64 = base64.b64encode(b"hi").decode()
            return lambda script, stdin="", timeout=20: formats + "|1|" + b64
        m._ps = fake_ps("UnicodeText,Text,OEMText,Locale")
        self.assertFalse(m._clip_get()["has_other"])
        m._ps = fake_ps("UnicodeText,Text,HTML Format")
        self.assertTrue(m._clip_get()["has_other"])
        m._ps = fake_ps("UnicodeText,SomeVendorFormat")
        self.assertTrue(m._clip_get()["has_other"])

    @unittest.skipUnless(os.name == "nt", "the PowerShell clipboard path is Windows-only")
    def test_clip_set_check_and_write_are_one_powershell_step(self):
        m, scripts = self.m, []
        m.IS_MAC = False

        def run(cmd, **kw):
            scripts.append(cmd[-1])
            return types.SimpleNamespace(returncode=3, stdout=b"")           # the guard in the script said "not text"
        m.subprocess = fake_subprocess(run)
        with self.assertRaises(m.Measurement) as ctx:
            m._clip_set("x")
        self.assertIn("plain text", str(ctx.exception))
        self.assertEqual(len(scripts), 1)                                     # no separate check beforehand
        self.assertIn("exit 3", scripts[0])
        self.assertIn("SetText", scripts[0])

    def test_mac_inspection_failure_and_unknown_types_refuse(self):
        m = self.m
        left, right = chr(0xab), chr(0xbb)

        def run(stdout, code=0):
            return lambda *a, **k: types.SimpleNamespace(returncode=code, stdout=stdout.encode("utf-8"))
        m._mac_env = lambda: {}
        m.subprocess = fake_subprocess(run(left + "class utf8" + right + ", 5, " + left + "class utxt" + right + ", 10"))
        self.assertFalse(m._mac_clip_has_other())
        m.subprocess = fake_subprocess(run(left + "class utf8" + right + ", 5, " + left + "class HTML" + right + ", 99"))
        self.assertTrue(m._mac_clip_has_other())
        m.subprocess = fake_subprocess(run("", code=1))
        self.assertTrue(m._mac_clip_has_other())

        def boom(*a, **k):
            raise OSError("no osascript")
        m.subprocess = fake_subprocess(boom)
        self.assertTrue(m._mac_clip_has_other())

    def reader(self, user_copies):
        m, sets, clip = self.m, [], {"t": "user clip"}
        m.IS_MAC, m.BACKEND = False, "native"
        m.time = FAST_TIME
        m._clip_get = lambda: {"text": clip["t"] or None, "has_other": False, "formats": []}

        def setter(text):
            sets.append(text)
            clip["t"] = text
        m._clip_set = setter

        def boom(*a, **k):
            if user_copies:
                clip["t"] = "newer text from the user"
            raise m.Measurement("copy failed")
        m._orca = boom
        with self.assertRaises(m.Measurement):
            m._composer_draft([])
        return sets

    def test_draft_reader_restores_the_clipboard_even_when_copy_fails(self):
        self.assertEqual(self.reader(user_copies=False), ["", "user clip"])

    def test_draft_reader_leaves_a_clipboard_the_user_changed(self):
        self.assertEqual(self.reader(user_copies=True), [""])

    def test_restore_if_unchanged_clears_what_was_empty(self):
        m, sets, clip = self.m, [], {"t": "marker"}
        m._clip_get = lambda: {"text": clip["t"], "has_other": False, "formats": []}
        m._clip_set = lambda text: sets.append(text)
        self.assertTrue(m._clip_restore_if_unchanged(None, "marker"))        # None = it was empty: cleared again
        self.assertEqual(sets, [None])
        self.assertFalse(m._clip_restore_if_unchanged("old", "other"))
        m._clip_get = lambda: {"text": None, "has_other": True, "formats": []}
        self.assertFalse(m._clip_restore_if_unchanged("old", None))


class CounterAndLinkTests(Base):
    def test_limits_hold_across_processes_even_with_the_log_off(self):
        os.environ["LINE_ACTION_LOG"] = "0"
        try:
            for _ in range(5):
                self.m.reserve_action("send", "send", "Alice", "hi")
            fresh = load_server(self.tmp.name)
            os.environ["LINE_ACTION_LOG"] = "0"
            with self.assertRaises(fresh.Measurement):
                fresh.reserve_action("send", "send", "Alice", "hi")
            self.assertFalse(fresh.action_log_path().exists())
            row = json.loads(fresh.counter_path().read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(set(row), {"ts", "kind"})                       # times and kinds only
        finally:
            os.environ.pop("LINE_ACTION_LOG", None)

    def test_unwritable_counter_refuses(self):
        Path(self.tmp.name, "rate.jsonl").mkdir()
        with self.assertRaises(self.m.Measurement):
            self.m.reserve_action("send", "send", "Alice", "hi")

    def try_symlink(self, link: Path, target: Path) -> bool:
        try:
            os.symlink(target, link)
            return True
        except (OSError, NotImplementedError):
            return False

    def test_a_linked_log_into_a_git_tree_is_refused(self):
        repo = Path(self.tmp.name, "repo")
        (repo / ".git").mkdir(parents=True)
        target = repo / "leak.jsonl"
        target.write_text("", encoding="utf-8")
        state = Path(self.tmp.name, "state")
        state.mkdir()
        if not self.try_symlink(state / "actions.jsonl", target):
            self.skipTest("symlinks are not available here")
        m = load_server(str(state))
        with self.assertRaises(m.Measurement):
            m.record_action("send", "send", "Alice", "hi")
        self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_a_linked_counter_or_config_is_refused(self):
        state = Path(self.tmp.name, "state")
        state.mkdir()
        other = Path(self.tmp.name, "elsewhere.json")
        other.write_text("{}", encoding="utf-8")
        if not self.try_symlink(state / "config.json", other):
            self.skipTest("symlinks are not available here")
        m = load_server(str(state))
        with self.assertRaises(m.Measurement):
            m.load_config()
        with self.assertRaises(m.Measurement):
            m.save_layout_value("GROUP_TAB", [205, 25])

    def test_chat_log_is_created_exclusively(self):
        m = self.m
        os.environ["LINE_MCP_LOG_DIR"] = str(Path(self.tmp.name, "logs"))
        try:
            first = m.save_chat_log("Alice", "one", now=1_700_000_000.0)
            second = m.save_chat_log("Alice", "two", now=1_700_000_000.0)
            self.assertNotEqual(first, second)
        finally:
            os.environ.pop("LINE_MCP_LOG_DIR", None)


class RestoreVerifyTests(Base):
    def stub(self):
        m = self.m
        m.time = FAST_TIME
        m._restore_supported = lambda: True
        m._view_png = lambda: b"png"
        m._ready = lambda *a, **k: {}
        m._orca = lambda *a, **k: None
        m._base_tail = lambda *a, **k: []
        m._chat_open = lambda png: True
        m._header_fp = None
        m.header_fingerprint = lambda png: "fp"
        m._search_active = lambda png: False
        m._active_tab = lambda png: None
        m._list_hash = lambda png: None
        return m

    def origin(self, **kw):
        base = {"at": time.time(), "chat_open": None, "fp": None, "tab": None, "search_active": False,
                "search": None, "name": None, "list_hash": None, "notes": []}
        base.update(kw)
        return base

    def test_closing_a_chat_is_verified(self):
        m = self.stub()
        out = m._restore_origin(self.origin(chat_open=False))
        self.assertIn("Could not put back", out)
        calls = {"n": 0}

        def chat_open(png):
            calls["n"] += 1
            return calls["n"] == 1                                            # open at first, closed after Escape
        m._chat_open = chat_open
        out = m._restore_origin(self.origin(chat_open=False))
        self.assertNotIn("Could not put back", out)

    def test_an_unmeasurable_list_is_a_failure_and_the_view_is_kept(self):
        m = self.stub()
        view = self.origin(list_hash="abc")
        m._ORIGIN["view"] = view
        note = m._restore_and_keep(view)
        self.assertIn("unknown", note)
        self.assertIs(m._ORIGIN.get("view"), view)

    def test_the_tab_is_verified_after_clicking(self):
        m = self.stub()
        m._click_win = lambda x, y: None
        m._tab_points = lambda: [(1, 1), (2, 2)]
        out = m._restore_origin(self.origin(tab=1))                          # the tab never becomes active
        self.assertIn("could not be confirmed", out)
        seen = iter([0, 1])
        m._active_tab = lambda png: next(seen)
        out = m._restore_origin(self.origin(tab=1))
        self.assertIn("View put back", out)


class DocsAndScriptTests(unittest.TestCase):
    def test_no_advisory_ids_in_public_text(self):
        for path in (ROOT / "server" / "line_desktop_mcp.py", ROOT / "SECURITY.md", ROOT / "README.md"):
            self.assertNotIn("CVE-", path.read_text(encoding="utf-8"))

    def test_helper_script_rechecks_before_clicking_and_uses_printwindow(self):
        text = (ROOT / "server" / "line_desktop_windows.ps1").read_text(encoding="utf-8")
        for needle in ("ABORTED_USER_INPUT", "ABORTED_CURSOR_MOVED", "ABORTED_LINE_NOT_IN_FRONT", "PrintWindow",
                       "IsLineProcess"):
            self.assertIn(needle, text)
        self.assertNotIn("CopyFromScreen", text)                              # every capture goes through PrintWindow

    def test_recalibrate_skill_does_not_delete_a_restored_draft(self):
        text = (ROOT / "skills" / "line-desktop-recalibrate" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("only if the dry_run result did not say it put an unsent draft back", text)


if __name__ == "__main__":
    unittest.main()
