"""Tests that need no LINE window: limits, action log, layout settings, guards, MCP framing.

Run: python -m unittest discover -s tests
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server" / "line_desktop_mcp.py"


def load_server(home: str):
    os.environ["LINE_MCP_HOME"] = home
    for key in ("LINE_MCP_CONFIG", "LINE_SEND_MAX_PER_MINUTE", "LINE_SEND_MAX_PER_DAY", "LINE_ACTION_LOG",
                "LINE_ACTION_LOG_BODY", "LINE_MANAGE_MAX_PER_MINUTE", "LINE_MANAGE_MAX_PER_DAY"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("line_desktop_mcp_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, data):
        Path(self.tmp.name, "config.json").write_text(json.dumps(data), encoding="utf-8")

    def test_default_limits_are_on(self):
        self.assertEqual(self.m.limits()["send_per_minute"], 5)
        self.assertEqual(self.m.limits()["send_per_day"], 100)

    def test_send_limit_per_minute(self):
        now = 1_000_000.0
        for i in range(5):
            self.m.check_limit("send", now + i)
            self.m.record_action("send", "send", "Alice", "hello", now + i)
        with self.assertRaises(self.m.Measurement):
            self.m.check_limit("send", now + 10)
        self.m.check_limit("send", now + 70)          # a minute later it is allowed again

    def test_limit_survives_restart_through_the_log(self):
        now = 2_000_000.0
        for i in range(5):
            self.m.record_action("send", "send", "Alice", "hi", now + i)
        fresh = load_server(self.tmp.name)
        with self.assertRaises(fresh.Measurement):
            fresh.check_limit("send", now + 20)

    def test_limits_from_config_and_env(self):
        self.write_config({"limits": {"send_per_minute": 0, "send_per_day": 2}})
        now = 3_000_000.0
        for i in range(2):
            self.m.record_action("send", "send", "Bob", "x", now + i)
        with self.assertRaises(self.m.Measurement):
            self.m.check_limit("send", now + 5)
        os.environ["LINE_SEND_MAX_PER_DAY"] = "0"
        try:
            self.m.check_limit("send", now + 5)       # 0 = no limit
        finally:
            os.environ.pop("LINE_SEND_MAX_PER_DAY")

    def test_log_has_no_message_text_by_default(self):
        self.m.record_action("send", "send", "Carol", "secret words", 4_000_000.0)
        row = json.loads(self.m.action_log_path().read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["target"], "Carol")
        self.assertEqual(row["chars"], len("secret words"))
        self.assertNotIn("text", row)
        self.assertNotIn("secret", json.dumps(row))

    def test_log_body_opt_in_and_log_off(self):
        self.write_config({"log": {"include_body": True}})
        self.m.record_action("send", "send", "Dan", "body here", 5_000_000.0)
        self.assertIn("body here", self.m.action_log_path().read_text(encoding="utf-8"))
        self.write_config({"log": {"enabled": False}})
        before = self.m.action_log_path().read_text(encoding="utf-8")
        self.m.record_action("send", "send", "Dan", "again", 5_000_010.0)
        self.assertEqual(before, self.m.action_log_path().read_text(encoding="utf-8"))

    def test_manage_guard_needs_target_and_counts(self):
        with self.assertRaises(self.m.Measurement):
            self.m._manage_guard({"action": "leave"})
        with self.assertRaises(self.m.Measurement):
            self.m._manage_guard({"action": "format_disk", "expected_target": "x"})
        self.assertIsNone(self.m._manage_guard({"action": "menu"}))
        self.assertEqual(self.m._manage_guard({"action": "leave", "expected_target": "Old trip"}), "leave")
        now = 6_000_000.0
        for i in range(6):
            self.m.record_action("manage", "leave", f"g{i}", now=now + i)
        with self.assertRaises(self.m.Measurement):
            self.m.check_limit("manage", now + 10)

    def test_layout_set_get_and_reload(self):
        path = self.m.save_layout_value("GROUP_TAB", [205, 25])
        self.assertTrue(path.is_file())
        self.assertEqual(self.m.GROUP_TAB, (205, 25))
        fresh = load_server(self.tmp.name)
        self.assertIn("GROUP_TAB", fresh.apply_layout())
        self.assertEqual(fresh.GROUP_TAB, (205, 25))
        with self.assertRaises(self.m.Measurement):
            self.m.save_layout_value("NOT_A_POINT", [1, 2])
        with self.assertRaises(self.m.Measurement):
            self.m.save_layout_value("GROUP_TAB", [1, 99999])
        self.m.save_layout_value("SEND_KEY", "Return")
        self.assertEqual(self.m.SEND_KEY, "Return")

    def test_named_point_argument(self):
        self.assertEqual(self.m._point_arg({"point": "SEARCH_BOX"}), tuple(self.m.SEARCH_BOX))
        self.assertEqual(self.m._point_arg({"x": 3, "y": 4}), (3, 4))
        with self.assertRaises(self.m.Measurement):
            self.m._point_arg({"point": "NOPE"})

    def test_send_requires_expected_chat_before_touching_anything(self):
        out = self.m.process({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "line_send", "arguments": {"text": "hi"}}})
        self.assertTrue(out["result"]["isError"])
        self.assertIn("expected_chat", out["result"]["content"][0]["text"])

    def test_tools_list(self):
        out = self.m.process({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in out["result"]["tools"]}
        self.assertIn("line_layout", names)
        self.assertIn("line_send", names)

    def test_http_token_rules(self):
        self.assertIsNotNone(self.m.http_token_problem(None))
        self.assertIsNotNone(self.m.http_token_problem("short"))
        self.assertIsNone(self.m.http_token_problem("x7Kq2mP9vR4tW8yB3nL6jH5fD1sG0aZc"))


if __name__ == "__main__":
    unittest.main()


class HeaderFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, header_value: int, body_value: int) -> bytes:
        w, h = 767, 722
        rows = []
        for y in range(h):
            v = header_value if 58 <= y < 102 else body_value
            rows.append(bytes([v, v, v, 255]) * w)
        return self.m._png_encode(b"".join(rows), w, h)

    def test_same_header_same_fingerprint(self):
        a = self.m.header_fingerprint(self.image(10, 20))
        b = self.m.header_fingerprint(self.image(10, 99))       # the conversation changed, the header did not
        self.assertIsNotNone(a)
        self.assertEqual(a, b)

    def test_other_chat_other_fingerprint(self):
        self.assertNotEqual(self.m.header_fingerprint(self.image(10, 20)),
                            self.m.header_fingerprint(self.image(11, 20)))

    def test_unknown_png_is_none(self):
        self.assertIsNone(self.m.header_fingerprint(b"not a png"))
        self.assertIsNone(self.m.header_fingerprint(None))


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_own_dry_run_text_is_not_a_draft(self):
        self.assertEqual(self.m._plain("Hi @[Ann]\n\nsee you"), self.m._plain("Hi @Ann\r\nsee you"))
        self.assertNotEqual(self.m._plain("draft memo"), self.m._plain("Hi"))


class RestoreViewTests(unittest.TestCase):
    """The pixel checks behind line_restore_view, on synthetic screenshots (no LINE window needed)."""

    W, H = 767, 722

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, paint=None, background: int = 255) -> bytes:
        """A W x H screenshot made by the server's own PNG encoder. `paint` is a list of (x, y, grey) pixels."""
        data = bytearray(bytes([background, background, background, 255]) * (self.W * self.H))
        for x, y, v in (paint or []):
            i = (y * self.W + x) * 4
            data[i:i + 4] = bytes([v, v, v, 255])
        return self.m._png_encode(bytes(data), self.W, self.H)

    def clear_button(self, grey: int = 180):
        cx, cy = self.m.SEARCH_CLEAR
        return [(cx + dx, cy, grey) for dx in (-7, -6, -5, 5, 6)]

    def test_search_active_sees_the_grey_x_circle(self):
        self.assertFalse(self.m._search_active(self.image()))
        self.assertTrue(self.m._search_active(self.image(self.clear_button())))

    def test_search_active_ignores_a_light_grey_box(self):
        self.assertFalse(self.m._search_active(self.image(self.clear_button(230))))

    def test_search_active_follows_the_layout(self):
        old = self.clear_button()
        self.m.save_layout_value("SEARCH_CLEAR", [300, 90])
        moved = [(300 + dx, 90, 180) for dx in (-7, -6, -5, 5, 6)]
        self.assertTrue(self.m._search_active(self.image(moved)))
        self.assertFalse(self.m._search_active(self.image(old)))

    def test_unreadable_pictures_are_never_active(self):
        self.assertIsNone(self.m._search_active(None))
        self.assertIsNone(self.m._search_active(b"not a png"))
        self.assertIsNone(self.m._active_tab(None))
        self.assertIsNone(self.m._list_hash(b"not a png"))
        self.assertIsNone(self.m._chat_open(None))

    def test_filtered_png_is_not_decoded(self):
        import struct
        import zlib
        w, h = 4, 3
        raw = b"".join(b"\x01" + bytes(w * 3) for _ in range(h))      # filter type 1 (Sub)

        def chunk(tag, body):
            return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        self.assertIsNone(self.m._decode_png(png))
        self.assertIsNone(self.m._search_active(png))

    def test_active_tab_from_the_underline(self):
        points = self.m._tab_points()
        self.assertEqual(len(points), 5)
        for i, (x, y) in enumerate(points):
            dark = [(x + d, y + self.m.TAB_UNDERLINE_DY, 40) for d in (-3, 0, 3)]
            self.assertEqual(self.m._active_tab(self.image(dark)), i)
        self.assertIsNone(self.m._active_tab(self.image()))

    def test_chat_open_needs_dark_text_in_the_header(self):
        left, top, right, bottom = self.m.HEADER_NAME_BOX
        text = [(x, top + 10 + (x % 3), 20) for x in range(left + 5, left + 105)]
        self.assertFalse(self.m._chat_open(self.image()))
        self.assertTrue(self.m._chat_open(self.image(text)))

    def test_list_hash_follows_the_list_only(self):
        left, top, right, bottom = self.m.LIST_HASH_BOX
        base = self.m._list_hash(self.image())
        self.assertIsNotNone(base)
        self.assertEqual(base, self.m._list_hash(self.image([(10, 300, 0), (500, 400, 0)])))     # outside the list body
        self.assertNotEqual(base, self.m._list_hash(self.image([(left + 20, top + 50, 0)])))      # a row of the list changed

    def test_name_helpers(self):
        self.assertEqual(self.m._norm_name("Spring BBQ (18)"), "springbbq")
        self.assertGreaterEqual(self.m._name_score("Spring BBQ (18)", "Spring BBQ"), 0.95)
        self.assertLess(self.m._name_score("Spring BBQ", "Book club"), 0.75)
        self.assertEqual(self.m._search_query("Spring BBQ 2023"), "Spring")
        self.assertLessEqual(len(self.m._search_query("x" * 100)), 20)

    def test_nothing_remembered_means_nothing_restored(self):
        self.assertEqual(self.m._restore_origin(None), "")

    def test_tools_and_schema(self):
        out = self.m.process({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools = {t["name"]: t for t in out["result"]["tools"]}
        self.assertIn("line_restore_view", tools)
        self.assertIn("line_restore_view", self.m.HANDLERS)
        self.assertEqual(tools["line_restore_view"]["inputSchema"]["properties"], {})
        self.assertEqual(tools["line_read_chat_text"]["inputSchema"]["properties"]["restore_view"]["type"], "boolean")
        self.assertNotIn("restore_view", tools["line_read_chat_text"]["inputSchema"].get("required", []))

    def test_tab_points_are_named_and_recalibratable(self):
        for name in ("TAB_FRIENDS", "TAB_OPENCHAT", "TAB_OFFICIAL"):
            self.assertIn(name, self.m.NAMED_POINTS)
            self.assertIn(name, self.m.LAYOUT_KEYS)
        self.m.save_layout_value("TAB_FRIENDS", [150, 25])
        self.assertEqual(self.m._tab_points()[1], (150, 25))
        self.assertEqual(self.m._point_arg({"point": "TAB_OPENCHAT"}), tuple(self.m.TAB_OPENCHAT))
