"""Seventh round: macOS chat windows are never popups; the Windows helper script never saves pictures inside
a Git working tree and defaults to a private temp folder.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "line_desktop_mcp.py"
PS1 = ROOT / "server" / "line_desktop_windows.ps1"


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_KEY", "LINE_ACTION_LOG"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round7", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def win(wid, title, width, height):
    return {"id": wid, "title": title, "width": width, "height": height, "x": 0, "y": 0}


LIST = win(1, "LINE", 382, 687)
CHAT = win(2, "Some chat", 375, 566)              # the measured chat window size on a Mac
MENU = win(3, "", 200, 160)
DIALOG = win(4, "", 260, 180)
GRAY = win(5, "", 320, 300)                        # wide enough to be a chat window, too short to be sure


class MacClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)
        self.m.IS_MAC = True

    def tearDown(self):
        self.tmp.cleanup()

    def windows(self, *wins):
        self.m._windows = lambda: list(wins)

    def test_kinds(self):
        m = self.m
        self.assertEqual(m._mac_kind(LIST), "list")
        self.assertEqual(m._mac_kind(CHAT), "chat")
        self.assertEqual(m._mac_kind(MENU), "dialog")
        self.assertEqual(m._mac_kind(DIALOG), "dialog")
        self.assertEqual(m._mac_kind(GRAY), "uncertain")

    def test_a_chat_window_is_not_a_popup(self):
        m = self.m
        self.windows(CHAT, LIST)
        self.assertIsNone(m._popup_size())
        self.assertIsNone(m._confirm_popup_size())
        with self.assertRaises(m.Measurement):
            m._window_id("popup")

    def test_confirm_keys_are_refused_when_only_a_chat_window_is_open(self):
        m = self.m
        self.windows(CHAT, LIST)
        for key in ("Return", "space"):
            with self.assertRaises(m.Measurement, msg=key):
                m.tool_key({"key": key, "window": "popup", "action": "leave", "expected_target": "g"})
        with self.assertRaises(m.Measurement):
            m._return_key_guard("Return", {"window": "popup", "action": "leave", "expected_target": "g"})

    def test_a_real_dialog_is_found_and_accepts_the_confirm_key_guard(self):
        m = self.m
        self.windows(CHAT, LIST, DIALOG)
        self.assertEqual(m._popup_size(), (260, 180))
        self.assertEqual(m._window_id("popup"), 4)
        self.assertEqual(m._return_key_guard("Return", {"window": "popup", "action": "leave", "expected_target": "g"}),
                         "leave")

    def test_an_uncertain_window_refuses_confirm_keys_but_is_still_a_popup_for_clicks(self):
        m = self.m
        self.windows(CHAT, LIST, DIALOG, GRAY)
        self.assertIsNone(m._confirm_popup_size())
        with self.assertRaises(m.Measurement):
            m._return_key_guard("Return", {"window": "popup", "action": "leave", "expected_target": "g"})
        self.assertIn(m._window_id("popup"), (4, 5))                         # dialogs and uncertain windows only, never the chat

    def test_a_popup_click_on_a_chat_window_has_no_target(self):
        m = self.m
        self.windows(CHAT, LIST)
        m.ensure_window = lambda *a, **k: None
        m._remember_origin = lambda *a, **k: None
        m._bg_ok = lambda: False
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (375, 566)
        m.check_in_window = lambda *a, **k: None
        m.reserve_action = lambda *a, **k: self.fail("a refused click must not be counted")
        m._orca = lambda *a, **k: self.fail("nothing may be clicked")
        with self.assertRaises(m.Measurement):
            m.tool_click({"x": 100, "y": 450, "window": "popup", "action": "leave", "expected_target": "g"})

    def test_the_send_region_applies_to_chat_windows_not_to_the_list(self):
        m = self.m
        m._WIN_TITLE["main"] = CHAT["title"]
        with self.assertRaises(m.Measurement):
            m._navigation_guard(190, 566 - m.COMPOSER_FROM_BOTTOM, 375, 566)
        m._WIN_TITLE["main"] = m.APP
        m._navigation_guard(190, 687 - 20, 382, 687)


class HelperOutputTests(unittest.TestCase):
    def function_text(self):
        text = PS1.read_text(encoding="utf-8-sig").replace(chr(13) + chr(10), chr(10))
        a = text.index("function Resolve-Out")
        b = text.index(chr(10) + chr(10) + "$linePid", a)
        return text[a:b]

    def run_ps(self, body):
        exe = shutil.which("powershell")
        if exe is None:
            self.skipTest("PowerShell is not available")
        script = self.function_text() + chr(10) + body
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                              text=True, timeout=60)
        return proc.returncode, (proc.stdout or "").strip()

    @unittest.skipUnless(os.name == "nt", "the helper script is Windows-only")
    def test_a_path_inside_a_git_working_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            (repo / ".git").mkdir(parents=True)
            (repo / "sub").mkdir()
            for target in (repo / "x.png", repo / "sub" / "x.png"):
                code, out = self.run_ps("Resolve-Out '%s' 'note-shot'" % target)
                self.assertEqual(code, 1, out)
                self.assertIn("OUT_INSIDE_GIT_TREE", out)
            code, out = self.run_ps("Resolve-Out '%s' 'note-shot'" % Path(tmp, "elsewhere.png"))
            self.assertEqual(code, 0, out)
            self.assertTrue(out.endswith("elsewhere.png"))

    @unittest.skipUnless(os.name == "nt", "the helper script is Windows-only")
    def test_without_out_it_defaults_to_a_private_temp_folder(self):
        code, out = self.run_ps("Resolve-Out '' 'viewer-shot'")
        self.assertEqual(code, 0, out)
        self.assertIn("line-desktop-mcp-helper", out)
        self.assertTrue(re.search(r"viewer-shot-[0-9a-f]{32}\.png$", out), out)
        shutil.rmtree(Path(out).parent, ignore_errors=True)

    def test_script_and_docs_wire_it_in(self):
        text = PS1.read_text(encoding="utf-8-sig")
        self.assertIn("if ($Cmd -in @('note-shot', 'viewer-shot', 'screen-shot')) { $Out = Resolve-Out $Out $Cmd }", text)
        self.assertIn("line-desktop-mcp-helper", text.split("'cleanup' {")[1])           # cleanup deletes the default folder
        for doc in ("skills/line-desktop/references/windows-helpers.md", "skills/line-desktop/SKILL.md", "README.md",
                    "README.ja.md"):
            body = (ROOT / doc).read_text(encoding="utf-8")
            for stale in ("-Out x.png", "-Out v.png", "-Out p.png"):
                self.assertNotIn(stale, body, doc)
        self.assertIn("OUT_INSIDE_GIT_TREE", (ROOT / "skills/line-desktop/references/windows-helpers.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
