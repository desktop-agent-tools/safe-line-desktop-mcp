"""Fifth round: OS-held locks across real processes, owner-only screenshot files, unknown search state,
scan restore around the search clearing, read_chat_text restore on failure. No LINE window needed.
"""
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "line_desktop_mcp.py"
FAST_TIME = types.SimpleNamespace(sleep=lambda s: None, time=time.time, monotonic=time.monotonic)

HOLDER = """
import importlib.util, os, sys
os.environ["LINE_MCP_HOME"] = sys.argv[2]
spec = importlib.util.spec_from_file_location("holder", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
lock = m._ui_lock() if sys.argv[3] == "ui" else m._action_lock(5)
with lock:
    print("locked", flush=True)
    sys.stdin.readline()
"""


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_MAX_PER_MINUTE", "LINE_SEND_MAX_PER_DAY", "LINE_ACTION_LOG",
                "LINE_ACTION_LOG_BODY", "LINE_MANAGE_MAX_PER_MINUTE", "LINE_MANAGE_MAX_PER_DAY", "LINE_SEND_KEY"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_round5", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class TwoProcessLockTests(Base):
    def start_holder(self, which):
        script = Path(self.tmp.name, "holder.py")
        script.write_text(HOLDER, encoding="utf-8")
        state = Path(self.tmp.name, "state")
        proc = subprocess.Popen([sys.executable, str(script), str(SERVER), str(state), which],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            proc.stdin.close()
            proc.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        os.environ["LINE_MCP_HOME"] = str(state)
        return proc, load_server(str(state))

    def contend(self, which):
        proc, m = self.start_holder(which)
        m.UI_LOCK_WAIT = 0.3
        take = (lambda: m._ui_lock()) if which == "ui" else (lambda: m._action_lock(0.3))
        with self.assertRaises(m.Measurement) as ctx:                          # the other process holds it
            with take():
                pass
        if which == "ui":
            self.assertIn("another agent is operating LINE", str(ctx.exception))
        proc.kill()                                                            # the holder dies without cleaning up
        proc.wait(timeout=10)
        for _ in range(40):                                                    # the OS frees the lock at once
            try:
                with take():
                    return
            except m.Measurement:
                time.sleep(0.1)
        self.fail("the lock was not released after the holder process was killed")

    def test_ui_lock_contends_across_processes_and_dies_with_the_holder(self):
        self.contend("ui")

    def test_action_lock_contends_across_processes_and_dies_with_the_holder(self):
        self.contend("actions")

    def test_a_clean_exit_also_releases_it(self):
        proc, m = self.start_holder("ui")
        proc.stdin.write("done" + chr(10))
        proc.stdin.flush()
        proc.wait(timeout=10)
        with m._ui_lock():
            pass

    def test_the_lock_code_has_no_stale_file_cleanup(self):
        text = SERVER.read_text(encoding="utf-8")
        self.assertNotIn("_lock_is_stale", text)
        self.assertNotIn("_pid_alive", text)
        self.assertIn("msvcrt.locking", text)
        self.assertIn("fcntl.flock", text)


class PrivateFilesTests(Base):
    def test_screenshot_folder_and_files_are_owner_only(self):
        m = self.m
        folder = m._shot_dir()
        self.assertEqual(folder, m._shot_dir())                                # one folder per process
        self.assertTrue(folder.name.startswith("line-desktop-mcp-shots-"))
        path = folder / "one.png"
        m._write_private(path, b"png")
        self.assertEqual(path.read_bytes(), b"png")
        with self.assertRaises(FileExistsError):                               # exclusive create: never reuses a file
            m._write_private(path, b"again")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_mac_capture_output_stays_in_the_private_folder_and_is_chmodded(self):
        m = self.m
        seen = {}

        def run(cmd, **kw):
            if cmd[0] == "screencapture":
                out = Path(cmd[-1])
                seen["dir"] = out.parent
                out.write_bytes(b"png")
            return types.SimpleNamespace(returncode=0, stdout=b"")
        m.subprocess = types.SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError,
                                             TimeoutExpired=subprocess.TimeoutExpired)
        self.assertEqual(m._mac_capture(0, 0, 10, 10), b"png")
        self.assertEqual(seen["dir"], m._shot_dir())


class UnknownSearchTests(Base):
    def origin(self, **kw):
        base = {"at": time.time(), "chat_open": None, "fp": None, "tab": None, "search_active": False,
                "search": None, "name": None, "list_hash": None, "notes": []}
        base.update(kw)
        return base

    def test_an_unknown_original_search_state_never_clears_the_box(self):
        m = self.m
        m.time = FAST_TIME
        m._restore_supported = lambda: True
        m._view_png = lambda: b"png"
        m._chat_open = lambda png: None
        m.header_fingerprint = lambda png: None
        m._active_tab = lambda png: None
        m._list_hash = lambda png: None
        m._search_active = lambda png: True                                   # a search is in the box now
        cleared = []
        m._set_search_text = lambda text: cleared.append(text)
        note = m._restore_origin(self.origin(search_active=None, notes=["search state unknown"]))
        self.assertEqual(cleared, [])                                          # unknown is not "there was no search"
        self.assertIn("Could not put back", note)
        m._restore_origin(self.origin(search_active=False))                    # known: there was none, so clear it
        self.assertEqual(cleared, [""])


class ScanClearInsideProtectionTests(Base):
    def stub(self, fail_on):
        m = self.m
        m.time = FAST_TIME
        m._bg_ok = lambda: False
        m.ensure_window = lambda *a, **k: None
        m._ready = lambda *a, **k: {}
        m._base_tail = lambda *a, **k: []
        m._state = lambda *a, **k: {}
        m._png_bytes = lambda state: b"png"
        m._active_tab = lambda png: 1
        m._search_active = lambda png: True                                    # a search is in the box
        m._list_hash = lambda png: "hash"
        m._require_front = lambda *a, **k: None
        m._read_search_text = lambda: "my search"
        self.restored = []
        m._restore_scan_view = lambda *a, **k: self.restored.append((a, k)) or "Put back. "

        def clear(text):
            if fail_on == "clear":
                raise m.Measurement("could not clear the box")
        m._set_search_text = clear
        m._orca = lambda *a, **k: None

    def test_a_failure_while_clearing_the_search_still_restores(self):
        self.stub("clear")
        with self.assertRaises(self.m.Measurement) as ctx:
            self.m.tool_scan_list({})
        self.assertEqual(len(self.restored), 1)
        self.assertEqual(self.restored[0][0][1], "my search")                  # the text that had been read is passed on
        self.assertIn("Put back", str(ctx.exception))

    def test_the_restore_also_runs_on_a_normal_scan(self):
        self.stub(None)
        self.m._png_bytes = lambda state: None                                 # nothing to capture: the loop ends at once
        self.m.tool_scan_list({})
        self.assertEqual(len(self.restored), 1)


class ReadChatRestoreTests(Base):
    def test_a_failed_copy_still_puts_the_view_back(self):
        m = self.m
        m.time = FAST_TIME
        m.require_same_chat = lambda *a, **k: None
        m.ensure_window = lambda *a, **k: None
        m._remember_origin = lambda *a, **k: None
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        m._base_tail = lambda *a, **k: []
        m._require_front = lambda *a, **k: None
        m._popup_size = lambda: None                                            # no menu ever appears: nothing is copied
        m._clip_get = lambda: {"text": "mine", "has_other": False, "formats": []}
        m._clip_set = lambda text: None
        m._orca = lambda *a, **k: None
        calls = []
        m._ORIGIN["view"] = {"at": time.time()}
        m._restore_and_keep = lambda o, names=None: calls.append(names) or "View put back."
        with self.assertRaises(m.Measurement) as ctx:
            m.tool_read_chat_text({"expected_chat": "x"})
        self.assertEqual(calls, [["x"]])
        self.assertIn("View put back.", str(ctx.exception))

    def test_an_exception_while_reading_still_puts_the_view_back(self):
        m = self.m
        m.time = FAST_TIME
        m.require_same_chat = lambda *a, **k: None
        m.ensure_window = lambda *a, **k: None
        m._remember_origin = lambda *a, **k: None
        m._ready = lambda *a, **k: {}
        m._geometry = lambda *a, **k: (767, 722)
        m._base_tail = lambda *a, **k: []
        m._require_front = lambda *a, **k: None
        m._clip_get = lambda: {"text": "mine", "has_other": False, "formats": []}
        m._clip_set = lambda text: None

        def boom(*a, **k):
            raise m.Measurement("the user took over")
        m._orca = boom
        calls = []
        m._ORIGIN["view"] = {"at": time.time()}
        m._restore_and_keep = lambda o, names=None: calls.append(names) or "View put back."
        with self.assertRaises(m.Measurement) as ctx:
            m.tool_read_chat_text({"expected_chat": "x"})
        self.assertEqual(len(calls), 1)
        self.assertIn("the user took over", str(ctx.exception))
        self.assertIn("View put back.", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
