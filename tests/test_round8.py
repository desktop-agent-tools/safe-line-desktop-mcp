"""Eighth round: macOS human-input detection without time-proximity excuses (fail closed), and the Windows helper
refusing output paths that reach a Git working tree through a junction or symbolic link.
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "line_desktop_mcp.py"
PS1 = ROOT / "server" / "line_desktop_windows.ps1"


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_KEY", "LINE_ACTION_LOG"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round8", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacHumanInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)
        self.m.IS_MAC = True
        self.m._MAC_OWN_POSTS.clear()
        self.m._CALL["start"] = None

    def tearDown(self):
        self.tmp.cleanup()

    def idle(self, seconds):
        self.m._mac_idle_seconds = lambda: seconds

    def test_unreadable_idle_time_fails_closed_for_foreground_steps(self):
        self.idle(None)
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m._mac_human_active()
        self.assertIn("cannot read", str(ctx.exception))
        with self.assertRaises(self.m.Measurement):
            self.m._human_active()                       # the generic check goes through the same function
        with self.assertRaises(self.m.Measurement):
            self.m._check_human()                        # so a step is refused, not continued

    def test_recent_input_is_the_user_when_the_server_sent_nothing(self):
        self.idle(0.5)
        self.assertTrue(self.m._mac_human_active())
        self.idle(60)
        self.assertFalse(self.m._mac_human_active())

    def test_input_right_after_our_event_is_still_the_user(self):
        m = self.m
        now = time.time()
        m._MAC_OWN_POSTS.append((now - 1.0, now - 0.9))          # our event ended 0.9 s ago
        self.idle(0.3)                                            # input 0.3 s ago: 0.6 s after ours, not inside it
        self.assertTrue(m._mac_human_active())
        self.idle(0.05)
        self.assertTrue(m._mac_human_active())

    def test_the_old_point_four_second_excuse_is_gone(self):
        m = self.m
        now = time.time()
        m._MAC_OWN_POSTS.append((now - 0.5, now - 0.35))          # our event ended 0.35 s ago
        self.idle(0.1)                                            # input 0.1 s ago: 0.25 s after our event
        self.assertTrue(m._mac_human_active())                    # (it used to be excused within 0.4 s)

    def test_the_input_that_is_our_own_event_is_not_the_user(self):
        m = self.m
        now = time.time()
        m._MAC_OWN_POSTS.append((now - 0.3, now - 0.1))
        self.idle(0.12)                                           # the newest input fell inside our event window
        self.assertFalse(m._mac_human_active())
        self.idle(5)                                              # older than our event
        self.assertFalse(m._mac_human_active())

    def test_input_after_the_call_started_counts_even_when_it_is_old(self):
        m = self.m
        m._CALL["start"] = time.monotonic() - 10
        self.idle(5)                                              # 5 s ago: after the call started, before nothing of ours
        self.assertTrue(m._mac_human_active())

    def test_events_are_recorded_with_their_start_and_end(self):
        m = self.m
        calls = []

        class Proc:
            returncode = 0
            stdout = b'{"ok": true}'
            stderr = b""
        m.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: calls.append(1) or Proc()),
                                      "TimeoutExpired": subprocess.TimeoutExpired,
                                      "SubprocessError": subprocess.SubprocessError})
        m._mac_env = lambda: {}
        m._mac_js("key", "36", "0")
        self.assertEqual(len(m._MAC_OWN_POSTS), 1)
        start, end = m._MAC_OWN_POSTS[0]
        self.assertLessEqual(start, end)

    def test_docs_call_macos_untested_on_a_real_mac(self):
        for doc in ("README.md", "README.ja.md", "skills/line-desktop/SKILL.md",
                    "skills/line-desktop/references/mac.md"):
            body = (ROOT / doc).read_text(encoding="utf-8")
            self.assertTrue("tested on a real Mac" in body or "実機での動作確認はまだしていません" in body, doc)
        text = SERVER.read_text(encoding="utf-8")
        self.assertNotIn("abs(last - own) < 0.4", text)


class LinkedOutputPathTests(unittest.TestCase):
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

    @unittest.skipUnless(os.name == "nt", "junctions and the helper script are Windows-only")
    def test_a_junction_into_a_git_tree_is_refused_before_the_git_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            (repo / ".git").mkdir(parents=True)
            outside = Path(tmp, "outside")
            outside.mkdir()
            link = outside / "link"
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(repo)], capture_output=True)
            if made.returncode != 0:
                self.skipTest("could not create a junction here")
            try:
                code, out = self.run_ps("Resolve-Out '%s' 'note-shot'" % (link / "x.png"))
                self.assertEqual(code, 1, out)
                self.assertIn("OUT_THROUGH_LINK", out)
                code, out = self.run_ps("Resolve-Out '%s' 'note-shot'" % (link / "deep" / "x.png"))   # not yet existing below it
                self.assertEqual(code, 1, out)
                self.assertIn("OUT_THROUGH_LINK", out)
                code, out = self.run_ps("Resolve-Out '%s' 'note-shot'" % (outside / "plain.png"))
                self.assertEqual(code, 0, out)
            finally:
                os.rmdir(link)                                       # removes the junction only, never the target

    @unittest.skipUnless(os.name == "nt", "the helper script is Windows-only")
    def test_a_symbolic_link_file_is_refused_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "target.png")
            target.write_bytes(b"x")
            link = Path(tmp, "link.png")
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links need extra privileges here")
            code, out = self.run_ps("Resolve-Out '%s' 'note-shot'" % link)
            self.assertEqual(code, 1, out)
            self.assertIn("OUT_THROUGH_LINK", out)

    def test_script_and_docs_describe_it(self):
        text = PS1.read_text(encoding="utf-8-sig")
        self.assertIn("ReparsePoint", text)
        self.assertIn("OUT_THROUGH_LINK", text)
        self.assertLess(text.index("OUT_THROUGH_LINK"), text.index("OUT_INSIDE_GIT_TREE"))   # links first, then the .git walk
        self.assertIn("junction", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
