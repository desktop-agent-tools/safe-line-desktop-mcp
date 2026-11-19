# -*- coding: utf-8 -*-
"""MCP server (stdio) that drives the LINE desktop app on Windows and Mac.

Why it exists: driving LINE by hand means chaining "show the window -> bring it to the front -> take a screenshot ->
click". If the window is not brought to the front first, the click lands in a different window (this happened in
practice). This server prevents that on the tool side.

What it promises:
  - Looking, scrolling and a single left click (line_screenshot, line_scan_list, line_scroll, line_click) run in the
    background on Windows, without bringing LINE to the front, so the tool does not fight the user for the cursor
    (screenshots come straight from the window via PrintWindow, clicks go straight to it via PostMessage).
  - Every action that presses, types, sends or opens a chat first brings LINE to the front (`--restore-window`),
    because otherwise screenshots and clicks go to whatever window happens to be in front. Coordinates outside the
    window are refused.
  - Something that could not be verified is never reported as "nothing there". Failures are returned as
    MEASUREMENT_FAILED.
  - Sending goes through line_send only (it checks the chat, counts and logs the send); line_key refuses Return/Enter/Space
    except to confirm a dialog with a management action, and clicks in the input row / send area are refused. The server
    cannot read button labels or names on the screen: what to send to whom, and whether a dialog says what the agent
    thinks it says, is checked by the calling agent on the returned screenshots. Limits, the action log and one-agent-at-
    a-time locking are enforced here (see the README).
  - Screens are returned as images. Conversation text is not extracted or saved unless explicitly requested. Images go
    to a temporary folder and are deleted as soon as they are read (an unread one after an hour).

Requirements: LINE installed on a Windows PC. Orca is not needed: the server drives Windows directly (SendInput, screen
  copy). Set the environment variable LINE_BACKEND=orca only to go back to the old Orca-based path.
Run:
  python.exe line_desktop_mcp.py
  (do not go through cmd.exe / .bat / npx)
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "line-desktop", "version": "2.5.0"}

APP = "LINE"
SESSION = "line-desktop-mcp-" + uuid.uuid4().hex[:8]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED_PROCESS = 0x00000008 if os.name == "nt" else 0

# Coordinates for LINE's standard layout (window 767x722), with the window's top-left as the origin. Measured 2026-10-21.
ALL_TAB = (90, 24)
SEARCH_BOX = (190, 82)
LIST_POINT = (210, 400)       # chat list on the left
CHAT_POINT = (560, 350)       # conversation on the right
COMPOSER_X = 560              # x of the input box in the conversation pane
COMPOSER_FROM_BOTTOM = 100    # y of the input text (height above the bottom edge of the window)
# In this LINE setup Enter inserts a newline and sending is Alt+Enter (measured 2026-10-22).
# Enter, Shift+Enter and Ctrl+Enter did not send. If a PC differs, change it with send_key or the LINE_SEND_KEY environment variable.
SEND_KEY = "alt+Return"

KEY_ALIASES = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
               "del": "Delete", "delete": "Delete", "tab": "Tab", "backspace": "Backspace",
               "up": "Up", "down": "Down", "left": "Left", "right": "Right",
               "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End"}


class Measurement(Exception):
    """Report things that could not be verified, or that were refused, separately from ordinary failures."""


def _orca_exe() -> str:
    for name in ("orca.exe", "orca"):
        found = shutil.which(name)
        if found and found.lower().endswith(".exe"):
            return found
    fallback = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "orca" / "resources" / "bin" / "orca.exe"
    if fallback.is_file():
        return str(fallback)
    raise Measurement("orca.exe not found (Orca is not installed)")


def _launcher() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "LINE" / "bin" / "LineLauncher.exe"


HUMAN_IDLE_MS = 2500                 # input within this many ms of the user's last key or mouse input counts as "user is active"
INPUT_VERBS = {"click", "scroll", "drag", "type-text", "press-key", "hotkey", "paste-text", "set-value"}
_OWN_INPUT: dict[str, int | None] = {"tick": None}   # time of the last input this server produced (to tell it apart from the user's input)
_ID_WHICH: dict[str, str] = {}   # window id -> main / popup (to re-pick a window with the same role if one is lost)
# The tool call in progress: its start time and the inputs it has already sent (so an abort can report exactly what was pressed).
_CALL: dict = {"start": None, "done": []}


def _describe_input(args: list[str]) -> str:
    verb, opts = args[1], _parse_opts(args[2:])
    if verb == "click":
        return f"click({opts.get('--x')},{opts.get('--y')})"
    if verb in ("hotkey", "press-key"):
        return f"key [{opts.get('--key', '')}]"
    if verb in ("paste-text", "type-text", "set-value"):
        return "pasting or typing text"
    if verb == "drag":
        return "drag"
    return {"scroll": "scroll"}.get(verb, verb)


def _orca(args: list[str], timeout: int = 40, _retry: int = 2) -> dict:
    """Call orca without a window and return its JSON. Raises Measurement unless ok.

    If the window id has vanished (LINE's window list briefly includes an unrelated full-screen window, measured 2026-10-22),
    pick a window with the same role (main / popup) again and retry a few times.
    Before sending input (press / type / move) it checks **just before sending** that the user is not active. If it stops, nothing was sent.
    """
    is_input = len(args) > 1 and args[1] in INPUT_VERBS
    if is_input:
        _check_human()
    if BACKEND in ("native", "mac"):
        before = _input_tick() if is_input else None
        result = _mac(args) if BACKEND == "mac" else _native(args)
        if is_input:
            _CALL["done"].append(_describe_input(args))
            _settle_own_input(before)
        return result
    try:
        proc = subprocess.run(
            [_orca_exe(), *args, "--json"],
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise Measurement(f"orca did not return within {timeout} seconds ({args[1] if len(args) > 1 else ''})") from exc
    text = proc.stdout.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Measurement("could not read orca's reply") from exc
    if not data.get("ok"):
        err = data.get("error") or {}
        message = str(err.get("message", ""))
        if _retry > 0 and "windowNotFound" in message and "--window-id" in args:
            idx = args.index("--window-id") + 1
            which = _ID_WHICH.get(args[idx], "main")
            time.sleep(0.5)
            args = [*args[:idx], str(_window_id(which)), *args[idx + 1:]]
            return _orca(args, timeout, _retry - 1)
        raise Measurement(f"orca: {err.get('code', '?')} {message[:160]}")
    if is_input:
        _CALL["done"].append(_describe_input(args))
        _settle_own_input(None)
    return data.get("result") or {}


# ---- Direct control through the Windows API (no Orca) ----------------------
# Takes the same arguments as `_orca` (computer <verb> --window-id ... --x ...) and returns the same shape, so callers do not change.
BACKEND = os.environ.get("LINE_BACKEND", "native" if os.name == "nt" else
                         ("mac" if sys.platform == "darwin" else "orca")).lower()
FLAG_ONLY = {"--restore-window", "--no-screenshot", "--json"}
VK_NAMES = {"return": 0x0D, "enter": 0x0D, "escape": 0x1B, "esc": 0x1B, "delete": 0x2E, "del": 0x2E, "tab": 0x09,
            "backspace": 0x08, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
            "pageup": 0x21, "pagedown": 0x22, "home": 0x24, "end": 0x23}
VK_MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "meta": 0x5B, "cmd": 0x5B}
VK_EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x21, 0x22, 0x24, 0x23, 0x2E}
WHEEL_NOTCHES_PER_PAGE = 6
_W32: dict = {}


def _parse_opts(rest: list[str]) -> dict[str, str]:
    """['--x','10','--restore-window'] → {'--x': '10', '--restore-window': '1'}."""
    opts: dict[str, str] = {}
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag in FLAG_ONLY or i + 1 >= len(rest):
            opts[flag] = "1"
            i += 1
        else:
            opts[flag] = rest[i + 1]
            i += 2
    return opts


def _split_key(key: str) -> tuple[list[int], str]:
    """'alt+Return' → ([0x12], 'Return'). Modifier key names become numbers."""
    key = key.strip()
    if "+" in key and len(key) > 1:
        *mods, main = key.split("+")
    else:
        mods, main = [], key
    codes = []
    for m in mods:
        if m.lower() not in VK_MODS:
            raise Measurement(f"unknown modifier key: {m}")
        codes.append(VK_MODS[m.lower()])
    return codes, main


def _png_encode(bgra: bytes, width: int, height: int) -> bytes:
    """Turn a screen copy (BGRA) into a PNG (standard library only)."""
    import struct
    import zlib
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[i * stride:(i + 1) * stride]) for i in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))


def _w32() -> dict:
    """Set up the Windows API once (not called in Linux tests)."""
    if _W32:
        return _W32
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    try:
        user32.SetProcessDPIAware()          # use real pixel coordinates (no drift with display scaling)
    except OSError:
        pass
    ptr = wintypes.WPARAM

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ptr)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ptr)]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.UINT]
    _W32.update(ctypes=ctypes, wintypes=wintypes, user32=user32, kernel32=kernel32, gdi32=gdi32, dwmapi=dwmapi,
                INPUT=INPUT, BITMAPINFOHEADER=BITMAPINFOHEADER)
    return _W32


def _hwnd_exe(hwnd: int) -> str:
    w = _w32()
    ctypes, wintypes = w["ctypes"], w["wintypes"]
    pid = wintypes.DWORD(0)
    w["user32"].GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = w["kernel32"].OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not w["kernel32"].QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name.lower()
    finally:
        w["kernel32"].CloseHandle(handle)


def _hwnd_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """Top-left and size of the visible frame (shadow excluded)."""
    w = _w32()
    ctypes, wintypes = w["ctypes"], w["wintypes"]
    rect = wintypes.RECT()
    if w["dwmapi"].DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        w["user32"].GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _native_windows(include_iconic: bool = False) -> list[dict]:
    """Visible windows of LINE.exe (main window, menus, etc.)."""
    w = _w32()
    ctypes, wintypes, user32 = w["ctypes"], w["wintypes"], w["user32"]
    found: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        iconic = bool(user32.IsIconic(hwnd))
        if iconic and not include_iconic:
            return True
        x, y, width, height = _hwnd_bounds(hwnd)
        if not iconic and (width < 30 or height < 30):
            return True
        if _hwnd_exe(hwnd) != "line.exe":
            return True
        title = ctypes.create_unicode_buffer(128)
        user32.GetWindowTextW(hwnd, title, 128)
        found.append({"id": int(hwnd), "title": title.value, "x": x, "y": y, "width": width, "height": height,
                      "iconic": iconic})
        return True

    user32.EnumWindows(visit, 0)
    return found


def _native_focus(hwnd: int) -> None:
    """Bring a window to the front (sends no input; briefly attaches to the foreground window's thread to get permission)."""
    w = _w32()
    user32, kernel32 = w["user32"], w["kernel32"]
    ctypes, wintypes = w["ctypes"], w["wintypes"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.5)
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return
    cur = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_thread and fg_thread != cur and user32.AttachThreadInput(cur, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_thread, False)
    time.sleep(0.3)


def _native_capture(x: int, y: int, width: int, height: int) -> bytes:
    w = _w32()
    ctypes, user32, gdi32 = w["ctypes"], w["user32"], w["gdi32"]
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, width, height, hdc, x, y, 0x00CC0020 | 0x40000000)   # SRCCOPY | CAPTUREBLT
        info = w["BITMAPINFOHEADER"]()
        info.biSize, info.biWidth, info.biHeight = ctypes.sizeof(info), width, -height
        info.biPlanes, info.biBitCount = 1, 32
        buf = (ctypes.c_ubyte * (width * height * 4))()
        if gdi32.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(info), 0) == 0:
            raise Measurement("could not capture the screen (GetDIBits)")
        return _png_encode(bytes(buf), width, height)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)


def _native_capture_window(hwnd: int, x: int, y: int, width: int, height: int) -> bytes | None:
    """Capture from the window itself (PrintWindow). Works without bringing it to the front, even when covered by another window (measured 2026-11-04).
    Returns None when capture failed (minimized, all black, ...); the caller falls back to a screen copy or refuses."""
    w = _w32()
    ctypes, wintypes, user32, gdi32 = w["ctypes"], w["wintypes"], w["user32"], w["gdi32"]
    if user32.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    full_w, full_h = rect.right - rect.left, rect.bottom - rect.top
    ox, oy = x - rect.left, y - rect.top            # position of the visible frame (shadow excluded) within the whole window
    if full_w <= 0 or full_h <= 0 or ox < 0 or oy < 0 or ox + width > full_w or oy + height > full_h:
        return None
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, full_w, full_h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, 2):    # PW_RENDERFULLCONTENT
            return None
        info = w["BITMAPINFOHEADER"]()
        info.biSize, info.biWidth, info.biHeight = ctypes.sizeof(info), full_w, -full_h
        info.biPlanes, info.biBitCount = 1, 32
        buf = (ctypes.c_ubyte * (full_w * full_h * 4))()
        if gdi32.GetDIBits(mem, bmp, 0, full_h, buf, ctypes.byref(info), 0) == 0:
            return None
        raw = bytes(buf)
        stride = full_w * 4
        rows = b"".join(raw[(oy + r) * stride + ox * 4:(oy + r) * stride + (ox + width) * 4] for r in range(height))
        if rows.count(rows[:4]) * 4 == len(rows):   # all one color = nothing was captured
            return None
        return _png_encode(rows, width, height)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)


def _bg_restore_minimized() -> None:
    """Restore a minimized LINE to its normal size without bringing it to the front, and put it at the bottom of the stacking order (so it does not appear over the user's windows)."""
    w = _w32()
    user32 = w["user32"]
    for hwnd in _line_minimized_hwnds():
        user32.ShowWindow(hwnd, 4)                  # SW_SHOWNOACTIVATE
        user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # HWND_BOTTOM, NOSIZE|NOMOVE|NOACTIVATE
        time.sleep(0.8)


def _bg_post_click(hwnd: int, sx: int, sy: int) -> None:
    """Deliver a click straight to the window (PostMessage). Does not move the cursor or bring the window to the front (accepted by LINE, measured 2026-11-04)."""
    w = _w32()
    ctypes, wintypes, user32 = w["ctypes"], w["wintypes"], w["user32"]
    pt = wintypes.POINT(sx, sy)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    for msg, wp in ((0x0200, 0), (0x0201, 1), (0x0202, 0)):    # MOUSEMOVE, LBUTTONDOWN, LBUTTONUP
        user32.PostMessageW(hwnd, msg, wp, lp)
        time.sleep(0.05)


def _bg_post_wheel(hwnd: int, sx: int, sy: int, notches: int, up: bool) -> None:
    """Deliver a mouse wheel event straight to the window (PostMessage). Coordinates are screen coordinates."""
    user32 = _w32()["user32"]
    delta = 120 if up else -120
    lp = ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)
    for _ in range(notches):
        user32.PostMessageW(hwnd, 0x020A, (delta & 0xFFFF) << 16, lp)  # WM_MOUSEWHEEL
        time.sleep(0.03)


# Keys that work when posted straight to the LINE window (measured 2026-11-15 on LINE for Windows):
# plain keys only. Qt reads Ctrl/Alt/Shift from the real keyboard state, so combinations need the foreground path.
# Return is deliberately excluded: with "Enter to send" it would send a message.
BG_KEYS = {"escape", "esc", "tab", "backspace", "delete", "del", "up", "down", "left", "right",
           "pageup", "pagedown", "home", "end"}


def _bg_post_key(hwnd: int, key: str, repeat: int = 1) -> None:
    """Post a plain (modifier-free) key to the window without focusing it or moving the cursor."""
    user32 = _w32()["user32"]
    vk = VK_NAMES[key.lower()]
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    ext = 0x01000000 if vk in VK_EXTENDED else 0
    down = 0x00000001 | (scan << 16) | ext
    up = down | 0xC0000000
    for i in range(repeat):
        user32.PostMessageW(hwnd, 0x0100, vk, down)     # WM_KEYDOWN
        user32.PostMessageW(hwnd, 0x0101, vk, up)       # WM_KEYUP
        if i % 50 == 49:
            time.sleep(0.05)                            # let Qt drain the queue (the queue holds ~10,000 messages)


def _bg_post_chars(hwnd: int, text: str) -> None:
    """Post text as WM_CHAR (UTF-16 units, so emoji and Japanese work). Single-line text only."""
    user32 = _w32()["user32"]
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        user32.PostMessageW(hwnd, 0x0102, int.from_bytes(data[i:i + 2], "little"), 1)   # WM_CHAR
        if i % 200 == 198:
            time.sleep(0.05)


_SHOT_DIR: Path | None = None


def _shot_dir() -> Path:
    """A private folder for screenshots: made with tempfile.mkdtemp (mode 0o700, owner only), one per server process,
    removed when the process ends. Pictures older than an hour that nobody read are deleted."""
    global _SHOT_DIR
    if _SHOT_DIR is None or not _SHOT_DIR.is_dir():
        import atexit
        import tempfile
        _SHOT_DIR = Path(tempfile.mkdtemp(prefix="line-desktop-mcp-shots-"))
        try:
            os.chmod(_SHOT_DIR, 0o700)
        except OSError:
            pass
        atexit.register(shutil.rmtree, str(_SHOT_DIR), True)
    cutoff = time.time() - 3600
    for old in _SHOT_DIR.glob("*.png"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return _SHOT_DIR


def _write_private(path: Path, data: bytes) -> None:
    """Create a new file readable by the owner only (mode 0o600, exclusive create) and write the bytes."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _guard_before_input(hwnd: int | None = None, check_human: bool = True, wait: bool = True) -> None:
    """The one check made before every input event this server injects on Windows: the user is not on the keyboard
    or mouse, and (when hwnd is given) the foreground window is exactly the target window. `wait=True` is for the first
    event of a step (it may wait for the user to pause, like _check_human); `wait=False` is for the events that follow
    inside one step (a pressed button, a run of wheel notches, typed characters): there is no waiting then, the step
    stops at once. Callers that hold something down release it in a finally."""
    if check_human:
        if wait:
            _check_human()
        elif _human_active():
            raise Measurement("Stopped midway because user activity was detected (anything pressed was released). "
                              "Check the screen with line_screenshot and do not repeat the action until the user says to continue.")
    if hwnd is not None and _foreground_hwnd() != hwnd:
        raise Measurement("the target LINE window is no longer in front, so the input was not sent (the user may be working in "
                          "another window). Nothing further was pressed.")


def _send(inputs: list) -> None:
    w = _w32()
    ctypes = w["ctypes"]
    array = (w["INPUT"] * len(inputs))(*inputs)
    if w["user32"].SendInput(len(inputs), array, ctypes.sizeof(w["INPUT"])) != len(inputs):
        raise Measurement("could not send input (SendInput rejected it)")


def _key_input(vk: int, up: bool, unicode_char: str | None = None):
    w = _w32()
    item = w["INPUT"]()
    item.type = 1
    if unicode_char is not None:
        item.u.ki.wScan = ord(unicode_char)
        item.u.ki.dwFlags = 0x0004 | (0x0002 if up else 0)     # KEYEVENTF_UNICODE
    else:
        item.u.ki.wVk = vk
        item.u.ki.dwFlags = (0x0001 if vk in VK_EXTENDED else 0) | (0x0002 if up else 0)
    return item


def _mouse_input(flags: int, data: int = 0):
    item = _w32()["INPUT"]()
    item.type = 0
    item.u.mi.dwFlags = flags
    item.u.mi.mouseData = data & 0xFFFFFFFF
    return item


def _native_key(key: str, hwnd: int | None = None, wait: bool = True) -> None:
    mods, main = _split_key(key)
    lowered = main.lower()
    if lowered in VK_NAMES:
        vk = VK_NAMES[lowered]
    elif re.fullmatch(r"f([1-9]|1[0-2])", lowered):
        vk = 0x70 + int(lowered[1:]) - 1
    elif len(main) == 1:
        vk = _w32()["user32"].VkKeyScanW(main) & 0xFF
        if vk == 0xFF:
            raise Measurement(f"unknown key: {main}")
    else:
        raise Measurement(f"unknown key: {main}")
    _guard_before_input(hwnd, wait=wait)
    _send([_key_input(m, False) for m in mods] + [_key_input(vk, False), _key_input(vk, True)]
          + [_key_input(m, True) for m in reversed(mods)])


def _clip_text() -> str | None:
    w = _w32()
    user32, kernel32, ctypes = w["user32"], w["kernel32"], w["ctypes"]
    if not user32.IsClipboardFormatAvailable(13):          # CF_UNICODETEXT
        return None
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = user32.GetClipboardData(13)
        if not handle:
            return None
        locked = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(locked) if locked else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


CF_TEXT_FORMATS = {1, 7, 13, 16}      # CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE: the only formats a text paste may replace


def _native_open_clipboard():
    w = _w32()
    user32 = w["user32"]
    for _ in range(10):
        if user32.OpenClipboard(None):
            return user32
        time.sleep(0.05)
    return None


def _native_formats_open(user32) -> set:
    """Formats on the clipboard (the clipboard must be open)."""
    found, fmt = set(), 0
    for _ in range(500):
        fmt = user32.EnumClipboardFormats(fmt)
        if not fmt:
            break
        found.add(int(fmt))
    return found


def _native_clip_has_other() -> bool:
    """True when the clipboard holds anything but plain text (images, files, HTML, RTF, private formats ...), and
    also when it cannot be inspected: a text paste would destroy it, so the caller must refuse."""
    try:
        user32 = _native_open_clipboard()
        if user32 is None:
            return True
        try:
            return bool(_native_formats_open(user32) - CF_TEXT_FORMATS)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - an inspection failure is a refusal
        return True


def _native_clip_seq() -> int:
    return int(_w32()["user32"].GetClipboardSequenceNumber())


def _native_clip_clear() -> None:
    user32 = _native_open_clipboard()
    if user32 is None:
        raise Measurement("cannot open the clipboard (another app is using it)")
    try:
        user32.EmptyClipboard()
    finally:
        user32.CloseClipboard()


def _native_clip_set(text: str) -> None:
    w = _w32()
    user32, kernel32, ctypes = w["user32"], w["kernel32"], w["ctypes"]
    user32 = _native_open_clipboard()
    if user32 is None:
        raise Measurement("cannot open the clipboard (another app is using it)")
    try:
        if _native_formats_open(user32) - CF_TEXT_FORMATS:       # checked inside the same open: no gap
            raise Measurement("the clipboard holds something other than text (image, files, rich text ...), which a "
                              "write would destroy. Nothing was written.")
        user32.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(0x0002, len(data))     # GMEM_MOVEABLE
        locked = kernel32.GlobalLock(handle)
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(13, handle)
    finally:
        user32.CloseClipboard()


def _native_paste(text: str, hwnd: int | None = None) -> None:
    """Paste. Refuses (before writing) when the clipboard holds a non-text item; the text is restored in finally."""
    if _native_clip_has_other():
        raise Measurement("the clipboard holds something other than plain text (image, files, rich text) or cannot be "
                          "inspected, which a text paste would destroy. Nothing was pasted. Ask the user to copy "
                          "plain text (or clear the clipboard) and retry.")
    before = _clip_text()
    seq = None
    try:
        _native_clip_set(text)
        seq = _native_clip_seq()
        _guard_before_input(hwnd)           # after the write, right before Ctrl+V: the user or the foreground may have changed
        _native_key("ctrl+v", hwnd, wait=False)     # the outer check just ran: this one must not wait, only stop
        time.sleep(0.3)
    finally:
        # Put the user's clipboard back only if nobody has changed it since we wrote ours (sequence number);
        # an originally empty clipboard is cleared again.
        try:
            if seq is not None and _native_clip_seq() == seq:
                if before is not None:
                    _native_clip_set(before)
                else:
                    _native_clip_clear()
        except Measurement:
            pass


def _native(args: list[str]) -> dict:
    """Use the Windows API directly, with the same calling style as `orca computer <verb> ...`."""
    if os.name != "nt":
        raise Measurement("direct control works only on Windows (use Orca with LINE_BACKEND=orca)")
    if len(args) < 2 or args[0] != "computer":
        raise Measurement(f"unknown operation: {' '.join(args[:2])}")
    verb, opts = args[1], _parse_opts(args[2:])
    if verb == "list-apps":
        return {"apps": [{"name": APP}] if _native_windows(include_iconic=True) else []}
    if verb == "list-windows":
        return {"windows": _native_windows()}
    win = None
    if "--window-id" in opts:
        win = next((x for x in _native_windows() if x["id"] == int(opts["--window-id"])), None)
        if win is None:
            raise Measurement("LINE window not found (the id is gone)")
    if verb == "get-app-state":
        if win is None:
            raise Measurement("--window-id is required")
        if "--restore-window" in opts:
            _native_focus(win["id"])
            win = next((x for x in _native_windows() if x["id"] == win["id"]), win)
        result: dict = {"snapshot": {"window": {"width": win["width"], "height": win["height"]}}}
        if "--no-screenshot" not in opts:
            path = _shot_dir() / f"{uuid.uuid4().hex}.png"
            png = _native_capture_window(win["id"], win["x"], win["y"], win["width"], win["height"])
            if png is None:
                if "--restore-window" not in opts:
                    raise Measurement("could not capture the LINE window in the background (minimized or not rendered)")
                png = _native_capture(win["x"], win["y"], win["width"], win["height"])   # the window is in front by now, so a screen copy is fine
            _write_private(path, png)
            result["screenshot"] = {"path": str(path)}
        return result
    if win is None:
        raise Measurement("--window-id is required")
    w = _w32()
    user32 = w["user32"]

    def point(xk: str, yk: str) -> tuple[int, int]:
        return win["x"] + int(opts[xk]), win["y"] + int(opts[yk])

    # Background actions (no cursor movement, no bringing to the front). Used only for actions that do not mark chats as read, such as list tabs and scrolling.
    if verb == "bg-click":
        _bg_post_click(win["id"], *point("--x", "--y"))
        return {}
    if verb == "bg-scroll":
        x, y = point("--x", "--y")
        notches = WHEEL_NOTCHES_PER_PAGE * int(opts.get("--pages", "1"))
        _bg_post_wheel(win["id"], x, y, notches, opts.get("--direction", "down") == "up")
        return {}
    if verb == "bg-key":
        _bg_post_key(win["id"], opts.get("--key", ""), int(opts.get("--repeat", "1")))
        return {}
    if verb == "bg-chars":
        _bg_post_chars(win["id"], opts.get("--text", ""))
        return {}

    if verb == "click":
        x, y = point("--x", "--y")
        button = opts.get("--mouse-button", "left")
        down, up = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}[button]
        user32.SetCursorPos(x, y)
        time.sleep(0.08)
        # if the user touches anything (or the front window changes) during the 0.08 s since the cursor was moved, stop without pressing the button
        _guard_before_input(win["id"], check_human=_real_input() is not None)
        for index in range(int(opts.get("--click-count", "1"))):
            if index:                                               # the clicks after the first are checked again each time
                _guard_before_input(win["id"], check_human=_real_input() is not None, wait=False)
            _send([_mouse_input(down), _mouse_input(up)])
            time.sleep(0.08)
        return {}
    if verb == "drag":
        x1, y1 = point("--from-x", "--from-y")
        x2, y2 = point("--to-x", "--to-y")
        user32.SetCursorPos(x1, y1)
        time.sleep(0.08)
        _guard_before_input(win["id"])
        pressed = False
        try:
            _send([_mouse_input(0x0002)])
            pressed = True
            for step in range(1, 11):
                _guard_before_input(win["id"], wait=False)          # every step: user input or another front window stops the drag
                user32.SetCursorPos(x1 + (x2 - x1) * step // 10, y1 + (y2 - y1) * step // 10)
                time.sleep(0.03)
        finally:
            if pressed:
                _send([_mouse_input(0x0004)])                         # the button is always released, whatever happened
        return {}
    if verb == "scroll":
        x, y = point("--x", "--y")
        user32.SetCursorPos(x, y)
        time.sleep(0.08)
        notches = WHEEL_NOTCHES_PER_PAGE * int(opts.get("--pages", "1"))
        delta = 120 if opts.get("--direction", "down") == "up" else -120
        for index in range(notches):
            _guard_before_input(win["id"], wait=(index == 0))       # before every notch
            _send([_mouse_input(0x0800, delta)])               # MOUSEEVENTF_WHEEL
            time.sleep(0.02)
        return {}
    if verb in ("hotkey", "press-key"):
        _native_key(opts.get("--key", ""), win["id"])
        return {}
    if verb == "paste-text":
        _native_paste(opts.get("--text", ""), win["id"])
        return {}
    if verb == "type-text":
        for index, ch in enumerate(opts.get("--text", "")):
            _guard_before_input(win["id"], wait=(index == 0))       # before every character
            _send([_key_input(0, False, ch), _key_input(0, True, ch)])
        return {}
    raise Measurement(f"unknown operation: {verb}")


# ---- Direct control of LINE on Mac (2026-11-02) ----------------------------
# Takes the same arguments and returns the same shape as `_native`. Hands: line_desktop_mac.js (JXA); capture: screencapture; clipboard: pbcopy.
# LINE for Mac does not respond to accessibility queries (measured on a Mac), so mouse and key events are sent straight to screen coordinates.
IS_MAC = sys.platform == "darwin"
MAC_HELPER = Path(__file__).resolve().parent / "line_desktop_mac.js"
MAC_LAYOUT_FILE = Path(os.environ.get("LINE_MCP_LAYOUT") or (Path(__file__).resolve().parent / "mac_layout.json"))
# Mac key codes (same positions on US and JIS keyboards)
MAC_KEYCODES = {"return": 36, "enter": 36, "escape": 53, "esc": 53, "delete": 117, "del": 117, "tab": 48,
                "backspace": 51, "space": 49, "up": 126, "down": 125, "left": 123, "right": 124,
                "pageup": 116, "pagedown": 121, "home": 115, "end": 119,
                "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
                "f10": 109, "f11": 103, "f12": 111}
MAC_CHAR_KEYCODES = dict(zip("asdfhgzxcv", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
MAC_CHAR_KEYCODES.update({"b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20,
                          "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30,
                          "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
                          "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50})
# Windows Ctrl actions (select all, paste) are Cmd on Mac. Alt is Option. The real Control key is realctrl.
MAC_FLAGS = {"cmd": 0x100000, "command": 0x100000, "meta": 0x100000, "win": 0x100000, "ctrl": 0x100000,
             "control": 0x100000, "realctrl": 0x40000, "alt": 0x80000, "option": 0x80000, "opt": 0x80000,
             "shift": 0x20000}
MAC_IMAGE_CLIP = ("PNGf", "TIFF", "JPEG", "GIFf", "furl", "8BPS", "PDF ")
MAC_TEXT_TYPES = {"utf8", "utxt", "ut16", "TEXT", "string", "Unicode text", "public.utf8-plain-text", "public.utf16-plain-text"}
_MAC_OWN_POSTS: list[tuple[float, float]] = []   # (start, end) of each event this server sent (time.time()), so only those are not taken for human input
MAC_OWN_SLOP = 0.05                    # seconds of timing slack around one of our own events


def _mac_env() -> dict:
    env = dict(os.environ)
    env.setdefault("LANG", "en_US.UTF-8")
    env["LC_ALL"] = env.get("LC_ALL") or "en_US.UTF-8"
    return env


def _mac_js(*argv: str, timeout: int = 15) -> dict:
    """Call the helper (JXA). Raises Measurement on failure, e.g. missing permission."""
    if not IS_MAC:
        raise Measurement("Mac operations are available only on a Mac")
    started = time.time()
    try:
        proc = subprocess.run(["osascript", "-l", "JavaScript", str(MAC_HELPER), *argv],
                              capture_output=True, timeout=timeout, env=_mac_env())
    except subprocess.TimeoutExpired as exc:
        raise Measurement(f"Mac operation ({argv[0]}) did not return within {timeout} seconds") from exc
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(out.splitlines()[-1]) if out else {}
    except json.JSONDecodeError:
        data = {}
    if proc.returncode != 0 or not data.get("ok"):
        err = data.get("error") or proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise Measurement(f"Mac operation ({argv[0]}) failed: {err}. The Accessibility permission may be missing for the app that runs this "
                          "(e.g. the AI app or Terminal) (System Settings -> Privacy & Security)")
    if argv[0] in ("click", "move", "drag", "scroll", "key"):
        _MAC_OWN_POSTS.append((started, time.time()))
        del _MAC_OWN_POSTS[:-50]
    return data


def _mac_windows() -> list[dict]:
    return _mac_js("windows").get("windows") or []


def _mac_idle_seconds() -> float | None:
    try:
        return float(_mac_js("idle").get("seconds"))
    except (Measurement, TypeError, ValueError):
        return None


def _mac_human_active() -> bool:
    """True if there was real-hardware (HID) input that is NOT ours: input within HUMAN_IDLE_MS, or after this call
    started, and newer than the end of the last event this server sent. Only input inside the execution window of one
    of our own events (a fraction of a second) is attributed to us; being close in time to our input is no excuse
    for later input. If the idle time cannot be read the user cannot be watched, so foreground steps are refused
    (fail closed). Residual limit: a user input inside the same sub-second window as one of our events is not seen."""
    idle = _mac_idle_seconds()
    if idle is None:
        raise Measurement("the macOS user-activity watcher cannot read the time since the last input (HIDIdleTime), so the "
                          "user cannot be watched and no foreground step was done. Check the permissions of the app "
                          "that runs this server. Nothing was done.")
    now = time.time()
    last = now - idle
    if _MAC_OWN_POSTS and last <= _MAC_OWN_POSTS[-1][1] + MAC_OWN_SLOP:
        return False                      # the newest input is one of ours (or older than ours): nothing newer from the user
    if idle * 1000 < HUMAN_IDLE_MS:
        return True
    start = _CALL["start"]
    return start is not None and last >= now - (time.monotonic() - start)


def _mac_key_parts(key: str) -> tuple[int, int]:
    if "+" in key and len(key) > 1:
        *mods, main = key.split("+")
    else:
        mods, main = [], key
    flags = 0
    for m in mods:
        if m.lower() not in MAC_FLAGS:
            raise Measurement(f"unknown modifier key: {m}")
        flags |= MAC_FLAGS[m.lower()]
    lowered = main.lower()
    if lowered in MAC_KEYCODES:
        return MAC_KEYCODES[lowered], flags
    if len(main) == 1 and lowered in MAC_CHAR_KEYCODES:
        if main.isupper():
            flags |= MAC_FLAGS["shift"]
        return MAC_CHAR_KEYCODES[lowered], flags
    raise Measurement(f"unknown key: {main}")


def _mac_guard_before_input(wait: bool = True) -> None:
    """The one check made before every input event this server injects on macOS: the user is not on the keyboard or
    mouse, and LINE is the frontmost application (macOS can check the application, not the individual window).
    wait=True is for the first event of a step (it may wait for the user to pause); wait=False for the events that
    follow inside one step, which only stop. If the idle time cannot be read the step is refused (fail closed)."""
    if wait:
        _check_human()
    elif _human_active():
        raise Measurement("Stopped midway because user activity was detected. Check the screen with line_screenshot and do "
                          "not repeat the action until the user says to continue.")
    if not _foreground_is_line():
        raise Measurement("LINE is no longer the frontmost application, so the input was not sent (the user may be working "
                          "in another app). Nothing further was pressed.")


def _mac_key(key: str, wait: bool = True) -> None:
    code, flags = _mac_key_parts(key)
    _mac_guard_before_input(wait)
    _mac_js("key", str(code), str(flags))


def _mac_clip_text() -> str | None:
    proc = subprocess.run(["pbpaste"], capture_output=True, timeout=10, env=_mac_env())
    return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None


def _mac_clip_has_other() -> bool:
    """True when the clipboard holds anything but plain text, and also when it cannot be inspected."""
    try:
        proc = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, timeout=10, env=_mac_env())
    except (OSError, subprocess.SubprocessError):
        return True
    if proc.returncode != 0:
        return True
    info = proc.stdout.decode("utf-8", errors="replace")
    for token in info.replace("{", "").replace("}", "").split(","):
        token = token.strip()
        if token.isdigit() or not token:
            continue
        tag = token.replace("«" + "class ", "").replace("»", "").strip()
        if tag not in MAC_TEXT_TYPES:
            return True
    return False


def _mac_clip_set(text: str | None) -> None:
    if _mac_clip_has_other():
        raise Measurement("the clipboard holds something other than plain text (or cannot be inspected). Nothing was written.")
    proc = subprocess.run(["pbcopy"], input=(text or "").encode("utf-8"), capture_output=True, timeout=10,
                          env=_mac_env())
    if proc.returncode != 0:
        raise Measurement("could not write to the clipboard (pbcopy)")


def _mac_paste(text: str) -> None:
    """Paste. Refuses (before writing) when the clipboard holds an image or files; the text is restored in finally."""
    if _mac_clip_has_other():
        raise Measurement("the clipboard holds something other than plain text (image, files, rich text) or cannot be "
                          "inspected, which a text paste would destroy. Nothing was pasted. Ask the user to copy "
                          "plain text (or clear the clipboard) and retry.")
    before = _mac_clip_text()
    wrote = False
    try:
        _mac_clip_set(text)
        wrote = True
        time.sleep(0.1)
        _mac_guard_before_input()               # after the write, right before Cmd+V: the user or the front app may have changed
        _mac_key("cmd+v", wait=False)           # the outer check just ran: this one only stops, it does not wait
        time.sleep(0.3)
    finally:
        # Only if the clipboard still holds what we wrote (the user may have copied something since); an
        # originally empty clipboard is emptied again. macOS has no cheap change counter, so text is compared.
        if wrote:
            try:
                if _mac_clip_text() == text and not _mac_clip_has_other():
                    _mac_clip_set(before if before is not None else "")
            except Measurement:
                pass


def _mac_type(text: str) -> None:
    """Type short text (such as @). Uses System Events keystroke (only creates key events; does not use LINE's accessibility)."""
    script = 'on run argv\ntell application "System Events" to keystroke (item 1 of argv)\nend run'
    _mac_guard_before_input()
    started = time.time()
    proc = subprocess.run(["osascript", "-e", script, text], capture_output=True, timeout=15, env=_mac_env())
    if proc.returncode != 0:
        raise Measurement("could not type text (System Events; check the Accessibility permission)")
    _MAC_OWN_POSTS.append((started, time.time()))


def _mac_capture(x: int, y: int, width: int, height: int) -> bytes:
    """Capture a screen region. Even on Retina, the image is scaled down to the window's coordinate size (points) so click coordinates and image coordinates match."""
    path = _shot_dir() / f"cap-{uuid.uuid4().hex}.png"
    proc = subprocess.run(["screencapture", "-x", "-t", "png", f"-R{x},{y},{width},{height}", str(path)],
                          capture_output=True, timeout=20)
    try:
        os.chmod(path, 0o600)                     # screencapture wrote it with the default umask: owner only from now on
    except OSError:
        pass
    if proc.returncode != 0 or not path.is_file():
        raise Measurement("could not capture the screen (screencapture). The Screen Recording permission may be missing for the app that runs this server")
    subprocess.run(["sips", "-z", str(height), str(width), str(path)], capture_output=True, timeout=20)
    data = path.read_bytes()
    try:
        path.unlink()
    except OSError:
        pass
    return data


def _mac_launch() -> None:
    subprocess.Popen(["open", "-a", "LINE"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _mac(args: list[str]) -> dict:
    """Drive LINE on Mac directly, with the same calling style as `orca computer <verb> ...`."""
    if len(args) < 2 or args[0] != "computer":
        raise Measurement(f"unknown operation: {' '.join(args[:2])}")
    verb, opts = args[1], _parse_opts(args[2:])
    if verb == "list-apps":
        return {"apps": [{"name": APP}] if _mac_windows() else []}
    if verb == "list-windows":
        return {"windows": _mac_windows()}
    win = None
    if "--window-id" in opts:
        win = next((w for w in _mac_windows() if int(w["id"]) == int(opts["--window-id"])), None)
        if win is None:
            raise Measurement("LINE window not found (the id is gone)")
    if verb == "get-app-state":
        if win is None:
            raise Measurement("--window-id is required")
        if "--restore-window" in opts:
            _mac_js("activate")
            win = next((w for w in _mac_windows() if int(w["id"]) == int(win["id"])), win)
        result: dict = {"snapshot": {"window": {"width": win["width"], "height": win["height"]}}}
        if "--no-screenshot" not in opts:
            path = _shot_dir() / f"{uuid.uuid4().hex}.png"
            _write_private(path, _mac_capture(int(win["x"]), int(win["y"]), int(win["width"]), int(win["height"])))
            result["screenshot"] = {"path": str(path)}
        return result
    if win is None:
        raise Measurement("--window-id is required")

    def point(xk: str, yk: str) -> tuple[str, str]:
        return str(int(win["x"]) + int(opts[xk])), str(int(win["y"]) + int(opts[yk]))

    if verb == "click":
        x, y = point("--x", "--y")
        _mac_guard_before_input()
        _mac_js("move", x, y)
        time.sleep(0.08)
        _mac_guard_before_input(wait=False)   # if the owner touches anything (or the front app changes) after the move, stop without pressing the button
        _mac_js("click", x, y, opts.get("--mouse-button", "left"), opts.get("--click-count", "1"))
        return {}
    if verb == "drag":
        _mac_guard_before_input()
        _mac_js("drag", *point("--from-x", "--from-y"), *point("--to-x", "--to-y"))
        return {}
    if verb == "scroll":
        x, y = point("--x", "--y")
        lines = 5 * int(opts.get("--pages", "1"))
        _mac_guard_before_input()
        _mac_js("scroll", x, y, str(lines if opts.get("--direction", "down") == "up" else -lines))
        return {}
    if verb in ("hotkey", "press-key"):
        _mac_key(opts.get("--key", ""))
        return {}
    if verb == "paste-text":
        _mac_paste(opts.get("--text", ""))
        return {}
    if verb == "type-text":
        _mac_type(opts.get("--text", ""))
        return {}
    raise Measurement(f"unknown operation: {verb}")


MAC_LAYOUT_KEYS = {"ALL_TAB", "SEARCH_BOX", "LIST_POINT", "CHAT_POINT", "COMPOSER_X", "COMPOSER_FROM_BOTTOM",
                   "SEND_KEY", "STANDARD_SIZE", "LIST_SIZE", "CHAT_SIZE", "FIRST_RESULT_POINT", "COPY_RIGHT_CLICK_POINTS",
                   "COPY_MENU_FIRST_ROW", "COPY_MENU_MAX_SIZE", "FOCUS_CHAT_POINT"}


def load_mac_layout(path: Path | None = None) -> list[str]:
    """Replace the Windows coordinate table with the coordinates measured on Mac (mac_layout.json). Returns the names replaced.
    Windows values are not deleted (this only overrides the table at startup, on Mac)."""
    path = path or MAC_LAYOUT_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    done = []
    for name, value in data.items():
        if name not in MAC_LAYOUT_KEYS:
            continue
        if isinstance(value, list):
            value = [tuple(v) for v in value] if value and isinstance(value[0], list) else tuple(value)
        globals()[name] = value
        done.append(name)
    return done


_MAC_LAYOUT_LOADED: list[str] = []


def _line_visible() -> bool:
    apps = _orca(["computer", "list-apps"]).get("apps") or []
    return any(a.get("name") == APP for a in apps)


def ensure_window(wait_seconds: int = 15) -> None:
    """Make sure the LINE window is visible. If LINE is resident in the tray, relaunch it to show the window."""
    if _line_visible():
        return
    if IS_MAC:
        if not (Path("/Applications/LINE.app").is_dir() or (Path.home() / "Applications" / "LINE.app").is_dir()):
            raise Measurement("LINE is not installed (/Applications/LINE.app is missing)")
        _mac_launch()
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(1)
            if _line_visible():
                return
        raise Measurement(f"the LINE window did not appear within {wait_seconds} seconds (login screen, locked screen or minimized to the Dock?)")
    launcher = _launcher()
    if not launcher.is_file():
        raise Measurement("LINE is not installed (LineLauncher.exe is missing)")
    subprocess.Popen([str(launcher)], creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1)
        if _line_visible():
            return
    raise Measurement(f"the LINE window did not appear within {wait_seconds} seconds (login screen or locked screen?)")


STANDARD_SIZE = (767, 722)    # window size the coordinates were measured at; varies with PC, display scaling and LINE version
LIST_SIZE = STANDARD_SIZE      # On Mac the list and each chat are separate windows; measured separately in mac_layout.json
CHAT_SIZE = STANDARD_SIZE
_WIN_SIZE: dict[str, tuple[int, int]] = {}
_WIN_TITLE: dict[str, str] = {}


def _layout_note() -> str | None:
    """Warn that coordinates may be off when the main window is not the standard size (does not stop anything)."""
    size = _WIN_SIZE.get("main")
    if not size:
        return None
    if IS_MAC and not _MAC_LAYOUT_LOADED:
        return ("⚠ The LINE coordinates for Mac have not been measured yet (Windows values are still in use). Check the position on the image before pressing, and "
                "press buttons with line_click using image coordinates. When sending, pass composer_x and composer_y (the input box position) to line_send")
    standard = (LIST_SIZE if _WIN_TITLE.get("main") == APP else CHAT_SIZE) if IS_MAC else STANDARD_SIZE
    if abs(size[0] - standard[0]) <= 8 and abs(size[1] - standard[1]) <= 8:
        return None
    return (f"⚠ The LINE window is not the standard size ({standard[0]}x{standard[1]}); it is now {size[0]}x{size[1]}. "
            "The coordinates in this tool were measured at the standard size, so they may be off. "
            "Check the position on the image before pressing, or restore the window to the standard size")


def _windows() -> list[dict]:
    wins = _orca(["computer", "list-windows", "--app", APP]).get("windows") or []
    return [w for w in wins if int(w.get("width") or 0) > 0 and int(w.get("height") or 0) > 0]


def _line_minimized_hwnds() -> list[int]:
    """Handle of the minimized LINE main window. Orca reports a minimized window as a small window, so _window_id, which picks the main window by size,
    wrongly decides "the main window is not visible" (measured 2026-10-28: iconic=True, rect=-32000)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsIconic(hwnd):
            return True
        title = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, title, 64)
        if title.value.strip() != APP:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))                         and Path(buf.value).name.lower() == "line.exe":
                    found.append(int(hwnd))
            finally:
                kernel32.CloseHandle(handle)
        return True

    user32.EnumWindows(visit, 0)
    return found


def _restore_minimized() -> bool:
    """Restore a minimized LINE main window to its normal size (call only right before an action that brings it to the front). Returns True if restored."""
    if IS_MAC:
        if any((w.get("title") or "").strip() == APP for w in _windows()):
            return False
        _mac_launch()                 # a window minimized to the Dock or closed comes back when opened again
        time.sleep(1)
        return True
    hwnds = _line_minimized_hwnds()
    if not hwnds:
        return False
    import ctypes
    for hwnd in hwnds:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(1)
    return True


def _mac_kind(w: dict) -> str:
    """Classify a LINE window on macOS: "list" (the chat list, titled LINE), "chat" (a conversation window: at least
    about the size of CHAT_SIZE in the layout, 300x400 by default), "dialog" (clearly smaller than a chat window in both
    directions: a menu or a confirmation) or "uncertain" (in between). A chat window is NEVER a popup: Return in it
    would send the draft."""
    if (w.get("title") or "").strip() == APP:
        return "list"
    chat_w = min(300, int(CHAT_SIZE[0] * 0.8))
    chat_h = min(400, int(CHAT_SIZE[1] * 0.7))
    wd, ht = int(w["width"]), int(w["height"])
    if wd >= chat_w and ht >= chat_h:
        return "chat"
    if wd < chat_w and ht < chat_h:
        return "dialog"
    return "uncertain"


def _window_id(which: str = "main") -> int:
    """The id of the window to operate on.

    - The main window is the one titled "LINE" (measured: the main window's title is LINE. An unrelated full-screen window can briefly appear in the list,
      so choosing by size alone would pick it). If no title matches, the largest window that is at least 500 wide and 500 high.
    - A popup such as a menu is a separate small window. While it is open, Orca's list shows **only the menu**, not the main window.
    """
    wins = _windows()
    if not wins:
        raise Measurement("LINE window not found")
    if IS_MAC:
        list_windows = [w for w in wins if (w.get("title") or "").strip() == APP]
        if which == "list":
            pool = list_windows
        elif which == "popup":
            pool = [w for w in wins if _mac_kind(w) in ("dialog", "uncertain")]    # chat windows are not popups
        else:
            pool = wins[:1]  # first in CGWindowList = frontmost. An opened chat is a separate window from the list
        if not pool:
            name = "chat list" if which == "list" else ("popup (menu)" if which == "popup" else "LINE main window")
            raise Measurement(f"the {name} window is not visible")
        chosen = pool[0]
        _ID_WHICH[str(chosen["id"])] = which
        _WIN_SIZE[which] = (int(chosen["width"]), int(chosen["height"]))
        _WIN_TITLE[which] = (chosen.get("title") or "").strip()
        return int(chosen["id"])

    big = [w for w in wins if int(w["width"]) >= 500 and int(w["height"]) >= 500]
    small = [w for w in wins if w not in big]
    if which == "popup":
        if not small:
            raise Measurement("no popup (menu) window is open")
        chosen = small[-1]
    else:
        titled = [w for w in big if (w.get("title") or "").strip() == APP]
        pool = titled or big
        if not pool:
            if _line_minimized_hwnds():
                raise Measurement("LINE is minimized to the taskbar (line_open or line_screenshot can restore it)")
            raise Measurement("the LINE main window is not visible (a menu may be open; close it by pressing Escape with line_key)")
        pool.sort(key=lambda w: int(w["width"]) * int(w["height"]), reverse=True)
        chosen = pool[0]
    _ID_WHICH[str(chosen["id"])] = which
    _WIN_SIZE[which] = (int(chosen["width"]), int(chosen["height"]))
    _WIN_TITLE[which] = (chosen.get("title") or "").strip()
    return int(chosen["id"])


def _bg_ok() -> bool:
    """Whether background reading (no bringing to the front, no cursor movement) is available. Windows direct control only. Disabled with LINE_BACKGROUND=0."""
    return BACKEND == "native" and os.environ.get("LINE_BACKGROUND", "1") != "0"


def _state(with_shot: bool, which: str = "main", restore: bool = True, background: bool = False) -> dict:
    """Get the window state. restore=False does not bring it to the front (a menu closes if the window is brought forward, so use it only when capturing a menu).
    background=True captures without bringing it to the front, even while the user is active (it captures straight from the window, so it does not clash with the user's screen)."""
    if background and _bg_ok():
        _bg_restore_minimized()
        args = ["computer", "get-app-state", "--app", APP, "--session", SESSION,
                "--window-id", str(_window_id(which))]
        if not with_shot:
            args.append("--no-screenshot")
        return _orca(args)
    if restore:
        _check_human()                # bringing to the front takes over the user's screen, so do nothing if the user is active
        _restore_minimized()          # restore from the taskbar if minimized (otherwise the main window is not found)
    args = ["computer", "get-app-state", "--app", APP, "--session", SESSION,
            "--window-id", str(_window_id(which))]
    if restore:
        args.append("--restore-window")
    if not with_shot:
        args.append("--no-screenshot")
    return _orca(args)


def _foreground_is_line() -> bool:
    """Whether the foreground window now belongs to LINE.exe (checked with the Windows API).

    While the user is working in another app on the same PC (Chrome, etc.), Windows refuses to let other apps take the foreground,
    so LINE may not come to the front even with `--restore-window` (measured 2026-10-22). Pressing, pasting or sending keys then would go
    to the other app (a send key would send there), so nothing is operated unless LINE is in front.
    """
    if IS_MAC:
        try:
            return bool(_mac_js("front").get("is_line"))
        except Measurement:
            return False
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return Path(buf.value).name.lower() == "line.exe"
    finally:
        kernel32.CloseHandle(handle)


def _input_tick() -> int | None:
    """Time (millisecond tick) at which Windows last received key or mouse input. None if unavailable."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return int(info.dwTime)


def _now_tick() -> int:
    import ctypes
    return int(ctypes.windll.kernel32.GetTickCount())


def _mark_own_input() -> None:
    """Remember the input this server produced (any different input means the user touched something)."""
    _OWN_INPUT["tick"] = _input_tick()


def _settle_own_input(before: int | None) -> None:
    """Remember the time of this server's own input. When SendInput returns, Windows' "last input time" may not have changed yet
    (measured 2026-11-01: it can take 20 to 90 ms or more). Remembering before it changes made the server mistake the later-stamped
    click for the user's input and abort right after pressing with "stopped because the user is active (nothing was pressed)".
    So it waits briefly for the change before remembering (used only for the fallback check; the primary check is _RealInputWatch)."""
    if before is not None:
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline and _input_tick() == before:
            time.sleep(0.01)
    _mark_own_input()


class _RealInputWatch:
    """Watch only input coming from real keyboards, mice and touchpads (that is, human input).

    In Windows Raw Input (WM_INPUT), input created with SendInput has a device handle of 0. That keeps this tool, its helper script
    and other AIs' automated input from being mistaken for the user's input (confirmed 2026-11-01).
    Unlike a low-level hook, it never makes the user's input wait (it only reads messages afterwards). The window is a dedicated invisible one.
    """

    def __init__(self) -> None:
        self.last_real: float | None = None     # time.monotonic()
        self.kind = ""
        self.error: str | None = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="line-desktop-real-input", daemon=True).start()
        self._ready.wait(3)
        if not self._ready.is_set() and self.error is None:
            self.error = "did not start within 3 seconds"

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)

            class RAWINPUTDEVICE(ctypes.Structure):
                _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                            ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

            class RAWINPUTHEADER(ctypes.Structure):
                _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                            ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

            user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                               wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
            user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                               ctypes.POINTER(wintypes.UINT), wintypes.UINT]
            user32.GetRawInputData.restype = wintypes.UINT
            user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            hwnd_message = wintypes.HWND(-3 & ((1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1))   # HWND_MESSAGE
            hwnd = user32.CreateWindowExW(0, "STATIC", "line-desktop-real-input", 0, 0, 0, 0, 0,
                                          hwnd_message, None, None, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW {ctypes.get_last_error()}")
            # Mouse (1/2) and keyboard (1/6) are required. Touchpad (13/5) and touch screen (13/4) are registered as well if the device exists
            registered = []
            for page, usage in ((0x01, 0x02), (0x01, 0x06), (0x0D, 0x05), (0x0D, 0x04)):
                dev = RAWINPUTDEVICE(page, usage, 0x00000100, hwnd)       # RIDEV_INPUTSINK (receive input even in the background)
                if user32.RegisterRawInputDevices(ctypes.byref(dev), 1, ctypes.sizeof(dev)):
                    registered.append((page, usage))
            if (0x01, 0x02) not in registered or (0x01, 0x06) not in registered:
                raise OSError(f"RegisterRawInputDevices {ctypes.get_last_error()}")
            self._ready.set()
            msg, header = wintypes.MSG(), RAWINPUTHEADER()
            size = wintypes.UINT(0)
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x00FF:                               # WM_INPUT
                    size.value = ctypes.sizeof(header)
                    if user32.GetRawInputData(msg.lParam, 0x10000005, ctypes.byref(header), ctypes.byref(size),
                                              ctypes.sizeof(RAWINPUTHEADER)) != 0xFFFFFFFF and header.hDevice:
                        self.last_real = time.monotonic()
                        self.kind = {0: "mouse", 1: "keyboard", 2: "touchpad etc."}.get(header.dwType, "?")
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()


_WATCH: dict = {}


def _real_input() -> _RealInputWatch | None:
    """The watcher for human input (started on first use). None if unavailable (the GetLastInputInfo fallback check is used then)."""
    if os.name != "nt" or os.environ.get("LINE_REAL_INPUT", "1") == "0":
        return None
    if "watch" not in _WATCH:
        _WATCH["watch"] = _RealInputWatch()
    watch = _WATCH["watch"]
    return None if watch.error else watch


def _declared_tick_file() -> Path:
    base = os.environ.get("LINE_MCP_OWN_TICK_FILE")
    if base:
        return Path(base)
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "line-desktop-mcp" / "own-input-tick.txt"


def _declared_tick() -> int | None:
    """The time at which the helper script (line_desktop_windows.ps1) declared "the last input was mine". None if there is none.

    Helpers that move the real cursor or press Alt produce input outside this tool (in PowerShell). Left alone, the tool would mistake it
    for the user's input and stall for a few seconds (this happened repeatedly on 2026-10-28). Right after its input, the helper writes Windows'
    last input time, and the tool treats an input as its own **only when the time matches exactly**. If the user touches anything afterwards the time changes, so it stops immediately.
    """
    try:
        return int(_declared_tick_file().read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _human_active() -> bool:
    """Whether the user is on the keyboard or mouse (there was input that is not our own within the last HUMAN_IDLE_MS).

    So that this server never interrupts the user while they are doing something else or trying to stop it (clicks must be
    stoppable at any time). Pressing Esc or moving the mouse stops it either way.

    The primary check is _RealInputWatch (counts only real-hardware input). Input within HUMAN_IDLE_MS, or **after this call started**,
    counts as the user being active (so a touch during a long operation is not missed just because 2.5 s have passed).
    The old GetLastInputInfo-only check mistook our own late-stamped input for the user's (false positive) and let our own later
    input hide the user's (miss), so it is kept only as a fallback for when the watcher is unavailable.
    """
    if IS_MAC:
        return _mac_human_active()
    watch = _real_input()
    if watch is not None:
        last = watch.last_real
        if last is None:
            return False
        if time.monotonic() - last < HUMAN_IDLE_MS / 1000:
            return True
        return _CALL["start"] is not None and last >= _CALL["start"]
    tick = _input_tick()
    if tick is None:
        return False
    if _OWN_INPUT["tick"] is not None and tick == _OWN_INPUT["tick"]:
        return False                      # the last input was our own
    if _declared_tick() == tick:
        return False                      # the last input was our own, declared by the helper script
    idle = (_now_tick() - tick) & 0xFFFFFFFF
    return idle < HUMAN_IDLE_MS


def _check_human() -> None:
    if not _human_active():
        return
    done = _CALL["done"]
    if not done and wait_for_idle():
        return                        # nothing sent yet: we waited for a pause instead of fighting the user
    if not done:
        raise Measurement("Stopped the LINE action because the user is on the keyboard or mouse (this call has sent no input at all, "
                          "so nothing was pressed). Do not repeat the same action until the user says to continue")
    raise Measurement("⚠ Stopped midway because user activity was detected. **However, this call had already sent these inputs: "
                      + ", ".join(done) + "**. Treat their effects as having happened, and once the user is finished "
                      "check the screen with line_screenshot. Do not repeat the same action until the user says to continue")


def _foreground_hwnd() -> int:
    import ctypes
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _front_ok(which: str = "main") -> bool:
    """LINE is in front. On Windows the front window must also be THE target window (the main window or the popup
    that is about to receive the keys), not just any LINE window. On macOS only the application can be checked."""
    if not _foreground_is_line():
        return False
    if IS_MAC or os.name != "nt" or BACKEND != "native":
        return True
    try:
        return _foreground_hwnd() == _window_id(which)
    except Measurement:
        return False


def _require_front(which: str = "main", tries: int = 4) -> None:
    _check_human()
    for attempt in range(tries):
        if _front_ok(which):
            return
        _state(False, which)          # bring it to the front again
        time.sleep(0.5)
    if not _front_ok(which):
        raise Measurement("could not bring LINE to the front (the user may be working in another app). Stopped without operating anything. Wait a moment and retry")


def _ready(which: str = "main") -> dict:
    """Right before an action, bring LINE to the front and verify that it really is in front. Do not operate if that cannot be verified."""
    _check_human()                    # before bringing to the front (which takes over the user's screen), check that the user is not active
    state = _state(False, which)
    _require_front(which)
    return state


def _geometry(state: dict, minimum: int = 300) -> tuple[int, int]:
    win = (state.get("snapshot") or {}).get("window") or {}
    width, height = int(win.get("width") or 0), int(win.get("height") or 0)
    if width < minimum or height < minimum:
        raise Measurement(f"could not get the LINE window size ({width}x{height}). Check that it is not minimized")
    return width, height


def check_in_window(x: int, y: int, width: int, height: int) -> None:
    """Refuse only coordinates outside the window (so a click cannot land in another window)."""
    if not (0 <= x < width and 0 <= y < height):
        raise Measurement(f"coordinates ({x},{y}) are outside the window (the window is {width}x{height})")


def _png_bytes(state: dict) -> bytes | None:
    shot = state.get("screenshot") or {}
    path = shot.get("path")
    if path and Path(path).is_file():
        data = Path(path).read_bytes()
        try:
            Path(path).unlink()                 # the picture now lives in the response only
        except OSError:
            pass
        return data
    return None


def _image_block(png: bytes | None) -> dict:
    if png is None:
        return {"type": "text", "text": "(could not capture the screen. Treat as MEASUREMENT_FAILED)"}
    return {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"}


# The chat header (name at the top of the conversation) in the last screenshot the agent was given.
# Sending or reading is refused when the header changed since then: the user (or a new message) may have
# switched the open chat after the agent read the name. Measured 2026-11-15: the user opened another chat
# between a screenshot and a send, and only this check would have caught it.
HEADER_BOX = (365, 58, 640, 102)     # left, top, right, bottom in window coordinates (reference layout)
_LAST_HEADER: dict = {"fp": None}


def header_fingerprint(png: bytes | None) -> str | None:
    """Hash of the chat-header strip of a screenshot made by this server (filter-0 PNG). None if unknown."""
    import struct
    import zlib
    if not png or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, width, height, data = 8, 0, 0, b""
    while pos + 8 <= len(png):
        length, tag = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            if depth != 8 or color != 2:
                return None
        elif tag == b"IDAT":
            data += body
        pos += 12 + length
    try:
        raw = zlib.decompress(data)
    except zlib.error:
        return None
    stride = width * 3 + 1
    left, top, right, bottom = HEADER_BOX
    right = min(right, width - 1)
    bottom = min(bottom, height - 1)
    if right <= left or bottom <= top or len(raw) < stride * height:
        return None
    digest = hashlib.sha256()
    for row in range(top, bottom):
        line = raw[row * stride:(row + 1) * stride]
        if line[0] != 0:
            return None                      # not one of our own screenshots (e.g. macOS screencapture)
        digest.update(line[1 + left * 3:1 + right * 3])
    return digest.hexdigest()


def require_same_chat(reference: str | None = None) -> str | None:
    """Refuse when the open chat may have changed since the last screenshot the agent saw (Windows background mode).
    Fails closed: if the header cannot be fingerprinted now, nothing is done. `reference` (the fingerprint taken
    at the start of an action) is used for a second check right before the send key. Returns the reference."""
    if not _bg_ok() or os.environ.get("LINE_CHECK_HEADER", "1") == "0":
        return None
    seen = reference if reference is not None else _LAST_HEADER.get("fp")
    if seen is None:
        raise Measurement("take line_screenshot first and read the chat name at the top right; then call this again "
                          "with that name. Nothing was done.")
    now = header_fingerprint(_png_bytes(_state(True, background=True)))
    if now is None:
        raise Measurement("the chat header could not be read from the screen right now, so the open chat cannot be "
                          "confirmed. Nothing was done. Take line_screenshot and retry.")
    if now != seen:
        _LAST_HEADER["fp"] = None
        raise Measurement("the open chat (its header) changed since your last screenshot - the user or a new "
                          "message may have switched it. Nothing was done. Take line_screenshot, read the name "
                          "again, and only then retry.")
    return seen


def _shot(note: str, which: str = "main", restore: bool = True, background: bool = False) -> list[dict]:
    state = _state(True, which, restore, background)
    png = _png_bytes(state)
    if which == "main":
        _LAST_HEADER["fp"] = header_fingerprint(png)
    blocks = [{"type": "text", "text": note}, _image_block(png)]
    warn = _layout_note() if which == "main" else None
    if warn:
        blocks.insert(1, {"type": "text", "text": warn})
    if _PENDING_WARN:
        blocks[0]["text"] += " NOTE: " + "; ".join(_PENDING_WARN) + ". line_restore_view cannot put that part back."
        _PENDING_WARN.clear()
    return blocks


def _base_tail(which: str = "main") -> list[str]:
    return ["--app", APP, "--session", SESSION, "--restore-window", "--no-screenshot",
            "--window-id", str(_window_id(which))]


def _int(args: dict, key: str) -> int:
    try:
        return int(args[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise Measurement(f"{key} (an integer with the window's top-left as the origin) is required") from exc


def _norm_key(key: str) -> str:
    """Combinations such as 'ctrl+a' are kept as they are; single keys are normalized to Orca's names."""
    key = key.strip()
    if not key:
        raise Measurement("key is empty")
    if "+" in key and len(key) > 1:
        parts = [KEY_ALIASES.get(p.lower(), p) if i == key.count("+") else p.lower()
                 for i, p in enumerate(key.split("+"))]
        return "+".join(parts)
    return KEY_ALIASES.get(key.lower(), key)


# ---- user settings: layout overrides, limits, action log ---------------------------------
# One JSON file the user (or their AI agent) can edit. Environment variables win over the file.
#   {
#     "layout": {"windows": {"SEARCH_BOX": [190, 82]}, "mac": {...}},
#     "limits": {"send_per_minute": 5, "send_per_day": 100, "manage_per_minute": 6, "manage_per_day": 150},
#     "log": {"enabled": true, "include_body": false},
#     "wait_for_idle_seconds": 15,
#     "return_focus": true
#   }

# Named points that skills refer to by name (so a recalibration fixes every flow at once).
GROUP_TAB = (199, 23)            # "Groups" tab above the chat list
CHAT_MENU_BUTTON = (746, 81)     # the ⋮ button at the top right of an open chat
NEW_CHAT_BUTTON = (313, 677)     # the green button at the bottom right of the chat list
SEARCH_CLEAR = (322, 82)         # the × inside the search box (shown only while the box holds text; also used to detect that)
TAB_FRIENDS = (142, 24)          # "Friends" tab above the chat list (used to put the tab back after a scan)
TAB_OPENCHAT = (285, 24)         # "Open chats" tab
TAB_OFFICIAL = (385, 24)         # "Official accounts" tab
LEAVE_CONFIRM_POINT = (344, 397) # the confirm button of the "leave" notice in the main window (measured on Windows)
MANAGE_CONFIRM_POINTS = ("LEAVE_CONFIRM_POINT",)
CONFIRM_RADIUS = 60              # unannotated main-window clicks this close to a confirm point are refused
NAMED_POINTS = ("ALL_TAB", "TAB_FRIENDS", "GROUP_TAB", "TAB_OPENCHAT", "TAB_OFFICIAL", "SEARCH_BOX", "SEARCH_CLEAR",
                "LIST_POINT", "CHAT_POINT", "CHAT_MENU_BUTTON", "NEW_CHAT_BUTTON", "FIRST_RESULT_POINT",
                "FOCUS_CHAT_POINT", "LEAVE_CONFIRM_POINT")
LAYOUT_KEYS = set(MAC_LAYOUT_KEYS) | set(NAMED_POINTS)
DEFAULT_LIMITS = {"send_per_minute": 5, "send_per_day": 100, "manage_per_minute": 6, "manage_per_day": 150}
LIMIT_ENV = {"send_per_minute": "LINE_SEND_MAX_PER_MINUTE", "send_per_day": "LINE_SEND_MAX_PER_DAY",
             "manage_per_minute": "LINE_MANAGE_MAX_PER_MINUTE", "manage_per_day": "LINE_MANAGE_MAX_PER_DAY"}
# Actions that change who is in a chat or what other people see. They need a named target, count toward
# the "manage" limits and are written to the action log.
MANAGE_ACTIONS = {"leave", "remove_member", "invite", "create_group", "add_friend", "rename", "block",
                  "delete_chat", "unsend"}
_LOADED_LAYOUT: list[str] = []
_ACTIONS_MEM: list[tuple[float, str]] = []     # used when the log file is off


def state_dir() -> Path:
    base = os.environ.get("LINE_MCP_HOME")
    if base:
        return Path(base)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "line-desktop-mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "line-desktop-mcp"
    return Path.home() / ".local" / "state" / "line-desktop-mcp"


def config_path() -> Path:
    return Path(os.environ.get("LINE_MCP_CONFIG") or (state_dir() / "config.json"))


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    if _is_link(path):
        raise Measurement(f"refusing to read the settings file {path}: it is a symbolic link or reparse point")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Measurement(f"cannot read the settings file {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _platform_key() -> str:
    return "mac" if IS_MAC else "windows"


def _coerce_layout(name: str, value):
    if name == "SEND_KEY":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("SEND_KEY must be a key name such as alt+Return")
        return value.strip()
    if name in ("COMPOSER_X", "COMPOSER_FROM_BOTTOM"):
        number = int(value)
        if not 0 <= number <= 4000:
            raise ValueError(f"{name} must be 0..4000")
        return number
    if name == "COPY_RIGHT_CLICK_POINTS":
        return [tuple(_coerce_point(p)) for p in value]
    return _coerce_point(value)


def _coerce_point(value) -> tuple[int, int]:
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        raise ValueError("a point is [x, y]")
    x, y = int(value[0]), int(value[1])
    if not (0 <= x <= 4000 and 0 <= y <= 4000):
        raise ValueError("x and y must be 0..4000 (window coordinates, top-left origin)")
    return (x, y)


def apply_layout(config: dict | None = None) -> list[str]:
    """Apply coordinate overrides from the settings file (`layout.<windows|mac>` and plain `layout.<NAME>`)."""
    config = load_config() if config is None else config
    layout = config.get("layout") or {}
    merged = {k: v for k, v in layout.items() if k in LAYOUT_KEYS}
    merged.update({k: v for k, v in (layout.get(_platform_key()) or {}).items() if k in LAYOUT_KEYS})
    done = []
    for name, value in merged.items():
        try:
            globals()[name] = _coerce_layout(name, value)
        except (TypeError, ValueError) as exc:
            raise Measurement(f"settings file: layout {name}: {exc}") from exc
        done.append(name)
    _LOADED_LAYOUT[:] = done
    return done


def save_layout_value(name: str, value) -> Path:
    if name not in LAYOUT_KEYS:
        raise Measurement(f"unknown layout name {name}. Known: {', '.join(sorted(LAYOUT_KEYS))}")
    try:
        coerced = _coerce_layout(name, value)
    except (TypeError, ValueError) as exc:
        raise Measurement(str(exc)) from exc
    path = config_path()
    _check_private_file(path, "settings file")
    config = load_config()
    section = config.setdefault("layout", {}).setdefault(_platform_key(), {})
    section[name] = list(coerced) if isinstance(coerced, tuple) else (
        [list(p) for p in coerced] if isinstance(coerced, list) else coerced)
    _private_dir(path.parent, "settings folder")
    tmp = path.parent / (".config-" + uuid.uuid4().hex + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)     # never follows an existing link
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(config, ensure_ascii=False, indent=2))
        if _is_link(tmp):
            raise Measurement("the temporary settings file turned into a link; refusing to replace the settings file")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    globals()[name] = coerced
    if name not in _LOADED_LAYOUT:
        _LOADED_LAYOUT.append(name)
    return path


def limits() -> dict[str, int]:
    config = load_config()
    out = dict(DEFAULT_LIMITS)
    for key, value in (config.get("limits") or {}).items():
        if key in out:
            out[key] = int(value)
    for key, env in LIMIT_ENV.items():
        if os.environ.get(env, "").strip():
            out[key] = int(os.environ[env])
    return out


def _log_settings() -> tuple[bool, bool]:
    log = load_config().get("log") or {}
    enabled = bool(log.get("enabled", True))
    body = bool(log.get("include_body", False))
    if os.environ.get("LINE_ACTION_LOG", "").strip():
        enabled = os.environ["LINE_ACTION_LOG"].strip() != "0"
    if os.environ.get("LINE_ACTION_LOG_BODY", "").strip():
        body = os.environ["LINE_ACTION_LOG_BODY"].strip() == "1"
    return enabled, body


def action_log_path() -> Path:
    return state_dir() / "actions.jsonl"


def _refuse_in_git(path: Path, what: str) -> None:
    """Never write private output (action log, settings, chat logs) inside a Git working tree."""
    reason = unsafe_log_dir_reason(path)
    if reason:
        raise Measurement(f"refusing to write the {what} at {path}: it is {reason}. Point LINE_MCP_HOME (or the "
                          "matching setting) outside any Git repository. Nothing was done.")


def _try_lock(fd: int) -> bool:
    """Take an exclusive lock on byte 0 of an open file without waiting. The operating system owns the lock: it
    disappears when the process that holds it ends, however it ends, so no lock can go stale."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def _file_lock(name: str, timeout: float, busy: str):
    """Cross-process lock on an open file in the state folder: msvcrt.locking (Windows) or fcntl.flock (POSIX),
    retried until the timeout. The file itself is never deleted (deleting a lock file is what makes lock files
    racy), and a lock whose owner died is released by the operating system."""
    folder = state_dir()
    _refuse_in_git(folder, "lock file")
    _private_dir(folder, "state folder")
    lock = folder / name
    if _is_link(lock):
        raise Measurement(f"refusing to use the lock {lock}: it is a symbolic link or reparse point")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise Measurement(f"cannot open the lock {lock} ({exc}); nothing was done") from exc
    try:
        _ensure_owner_only(lock, 0o600, "lock file")
    except Measurement:
        os.close(fd)
        raise
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise Measurement(busy)
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _action_lock(timeout: float = 5.0):
    """Lock around check + record of the limits."""
    return _file_lock("actions.lock", timeout,
                      f"could not take the action lock {state_dir() / 'actions.lock'} (another process holds it); "
                      "nothing was done")


UI_LOCK_WAIT = 5.0                 # seconds to wait for another agent to finish before giving up
UI_TOOLS = ("line_click", "line_drag", "line_key", "line_type", "line_scroll", "line_search", "line_open_chat",
            "line_send", "line_read_chat_text", "line_restore_view", "line_scan_list", "line_open")


def _needs_ui_lock(name: str, args: dict) -> bool:
    """Tools that change the screen, bring LINE to the front (line_open, a foreground line_screenshot) or move the
    user's focus. A background screenshot, a popup screenshot, line_status and line_layout only look and do not take it."""
    if name in UI_TOOLS:
        return True
    if name == "line_screenshot":
        return str((args or {}).get("window") or "main") != "popup" and not _bg_ok()
    return False


def _ui_lock():
    """One lock for the whole LINE window: every tool call that changes the screen holds it, so two agents (two
    stdio servers, or stdio and HTTP) never paste, send or restore a draft at the same time."""
    return _file_lock("ui.lock", UI_LOCK_WAIT,
                      "another agent is operating LINE right now (the UI lock is held). Nothing was done; wait a moment "
                      "and retry.")


def counter_path() -> Path:
    """Timestamps and kinds only (no target, no text). Used for the limits whatever the log setting is."""
    return state_dir() / "rate.jsonl"


def _ensure_owner_only(path: Path, want: int, what: str) -> None:
    """POSIX: make sure nobody but the owner can reach this folder (0o700) or file (0o600): fix the mode when it is
    wider, and refuse when it cannot be fixed. On Windows the files live under the per-user profile (LOCALAPPDATA),
    which already carries a per-user ACL; this server does not change ACLs there."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            os.chmod(path, want)
            mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise Measurement(f"the {what} {path} can be read by other users (mode {oct(mode)}) and this cannot be "
                              "fixed; refusing to store private data there. Nothing was done.")
    except OSError as exc:
        raise Measurement(f"cannot make the {what} {path} private ({exc}); refusing to store private data there. "
                          "Nothing was done.") from exc


def _private_dir(folder: Path, what: str = "folder") -> None:
    """Create a folder for private data (owner only, 0o700 on POSIX) or tighten an existing one."""
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise Measurement(f"cannot create the {what} {folder} ({exc}); nothing was done") from exc
    _ensure_owner_only(folder, 0o700, what)


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows reparse point (junction, mount point ...)."""
    try:
        if os.path.islink(path):
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _check_private_file(path: Path, what: str) -> None:
    """Refuse a private output file (or its folder) that is a link, or whose real location is inside a Git tree."""
    for item in (path, path.parent):
        if _is_link(item):
            raise Measurement(f"refusing to use the {what} at {item}: it is a symbolic link or reparse point. "
                              "Nothing was done.")
    _refuse_in_git(path.parent, what)
    try:
        real = Path(os.path.realpath(path))
    except OSError as exc:
        raise Measurement(f"cannot resolve the {what} path {path} ({exc})") from exc
    _refuse_in_git(real.parent, what)


def _recent(kind: str, now: float) -> list[float]:
    """Times of past actions of this kind ("send" or "manage") within the last day (from the counter file)."""
    times = [t for t, k in _ACTIONS_MEM if k == kind and now - t < 86400]
    path = counter_path()
    if path.exists():
        _check_private_file(path, "rate counter")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-5000:]
        except (OSError, UnicodeDecodeError) as exc:
            raise Measurement(f"cannot read the rate counter {path} ({exc}), so the limits cannot be checked. "
                              "Nothing was done.") from exc
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = float(row.get("ts", 0))
            if row.get("kind") == kind and now - t < 86400 and t not in times:
                times.append(t)
    return times


def check_limit(kind: str, now: float | None = None) -> None:
    """Refuse when the per-minute or per-day limit for this kind of action is used up (0 = no limit)."""
    now = time.time() if now is None else now
    lim = limits()
    times = _recent(kind, now)
    per_minute, per_day = lim[f"{kind}_per_minute"], lim[f"{kind}_per_day"]
    in_minute = sum(1 for t in times if now - t < 60)
    if per_minute and in_minute >= per_minute:
        raise Measurement(f"limit reached: {in_minute} {kind} actions in the last minute (limit {per_minute}). "
                          "Nothing was done. Wait a minute, or raise limits."
                          f"{kind}_per_minute in {config_path()} if this pace is intended.")
    if per_day and len(times) >= per_day:
        raise Measurement(f"limit reached: {len(times)} {kind} actions in the last 24 hours (limit {per_day}). "
                          f"Nothing was done. Raise limits.{kind}_per_day in {config_path()} if this is intended.")


def _append_line(path: Path, line: str, what: str) -> None:
    _check_private_file(path, what)
    _private_dir(path.parent, "state folder")
    try:
        _check_private_file(path, what)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)      # owner only from the first byte
        with os.fdopen(fd, "a", encoding="utf-8", newline="") as fh:
            _ensure_owner_only(path, 0o600, what)
            fh.write(line + chr(10))
    except OSError as exc:
        raise Measurement(f"cannot write the {what} {path} ({exc}); refusing to act without a record. "
                          "Nothing was done.") from exc


def _prune_counter(now: float) -> None:
    path = counter_path()
    try:
        if path.stat().st_size < 262144:
            return
        keep = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if now - float(json.loads(line).get("ts", 0)) < 86400:
                    keep.append(line)
            except (ValueError, json.JSONDecodeError):
                continue
        tmp = path.parent / (".rate-" + uuid.uuid4().hex + ".tmp")
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(chr(10).join(keep) + chr(10))
        os.replace(tmp, path)
    except OSError:
        pass


def record_action(kind: str, action: str, target: str, text: str | None = None, now: float | None = None) -> None:
    """Remember an action locally. The counter file (time and kind only) is always written, because the limits
    depend on it; the action log (time, kind, action, target, length, hash) is written when it is on, and the text
    itself only if include_body is on. Fails closed: when either file cannot be written this raises (callers record
    BEFORE pressing)."""
    now = time.time() if now is None else now
    enabled, body = _log_settings()
    stamp = round(now, 3)
    _append_line(counter_path(), json.dumps({"ts": stamp, "kind": kind}), "rate counter")
    _prune_counter(now)
    if enabled:
        row = {"ts": stamp, "time": dt.datetime.fromtimestamp(now).isoformat(timespec="seconds"),
               "kind": kind, "action": action, "target": target}
        if text is not None:
            row["chars"] = len(text)
            row["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            if body:
                row["text"] = text
        _append_line(action_log_path(), json.dumps(row, ensure_ascii=False), "action log")
    _ACTIONS_MEM.append((stamp, kind))     # same rounding as the files, so one action is not counted twice


def reserve_action(kind: str, action: str, target: str, text: str | None = None) -> None:
    """Check the limit and record the action as one step under a cross-process lock. Call it right BEFORE the
    key or click that performs the action (a press that then fails still counts, which is the safe side)."""
    with _action_lock():
        check_limit(kind)
        record_action(kind, action, target, text)


def _wait_seconds() -> float:
    env = os.environ.get("LINE_WAIT_FOR_IDLE_SECONDS", "").strip()
    if env:
        return float(env)
    try:
        return float(load_config().get("wait_for_idle_seconds", 15))
    except (Measurement, TypeError, ValueError):
        return 15.0


def wait_for_idle() -> bool:
    """Before taking the foreground, wait (up to wait_for_idle_seconds) for a pause in the user's typing/mouse.
    Returns True when the user is idle. The user is never interrupted mid-gesture; they keep working."""
    deadline = time.monotonic() + _wait_seconds()
    while True:
        _CALL["start"] = time.monotonic()          # only input from now on counts
        if not _human_active():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


_FOCUS: dict = {"hwnd": None, "cursor": None}
# Tools after which the user's window gets the foreground back. Clicks, keys and drags keep LINE in front,
# because the agent may be in the middle of a menu or dialog that would close when LINE loses focus.
GIVE_BACK_TOOLS = {"line_send", "line_type", "line_search", "line_open_chat", "line_read_chat_text",
                   "line_scan_list", "line_scroll", "line_screenshot", "line_status", "line_restore_view"}


def remember_focus() -> None:
    """Remember the user's foreground window and mouse position, so they can be given back after a foreground step."""
    _FOCUS["hwnd"], _FOCUS["cursor"] = None, None
    if os.name != "nt" or BACKEND != "native":
        return
    w = _w32()
    user32 = w["user32"]
    hwnd = user32.GetForegroundWindow()
    if hwnd and not _foreground_is_line():
        _FOCUS["hwnd"] = hwnd
        pt = w["wintypes"].POINT()
        if user32.GetCursorPos(w["ctypes"].byref(pt)):
            _FOCUS["cursor"] = (pt.x, pt.y)


def return_focus() -> None:
    """If this call brought LINE to the front, hand the foreground and the mouse back to where the user was.
    Skipped when the user touched anything during the call (they are already somewhere on purpose)."""
    if not _FOCUS.get("hwnd") or os.name != "nt" or BACKEND != "native":
        return
    if os.environ.get("LINE_RETURN_FOCUS", "1") == "0":
        return
    try:
        if not load_config().get("return_focus", True):
            return
    except Measurement:
        pass
    if not _CALL["done"] or not _foreground_is_line() or _human_active():
        return
    user32 = _w32()["user32"]
    if _FOCUS.get("cursor"):
        user32.SetCursorPos(*_FOCUS["cursor"])
    _native_focus(_FOCUS["hwnd"])


# ---- tools -----------------------------------------------------------------

def _watch_note() -> str:
    if IS_MAC:
        idle = _mac_idle_seconds()
        if idle is None:
            return "User-activity watcher: cannot get seconds since the last real input (check the Mac permissions)."
        layout = "Mac measured coordinates loaded" if _MAC_LAYOUT_LOADED else "⚠ Mac coordinates not measured (still the Windows values)"
        return f"User-activity watcher: the last real-hardware (HID) input was {idle:.1f} s ago. {layout}. Send key: {SEND_KEY}."
    watch = _real_input()
    if watch is None:
        reason = (_WATCH.get("watch").error if _WATCH.get("watch") else "not Windows, or LINE_REAL_INPUT=0")
        return f"User-activity watcher: running on the fallback check (time of the last input). The real-hardware input watcher is unavailable ({reason})."
    if watch.last_real is None:
        return "User-activity watcher: counting real-hardware input only (no user input yet since startup)."
    ago = time.monotonic() - watch.last_real
    return f"User-activity watcher: counting real-hardware input only (last user input {ago:.1f} s ago, {watch.kind})."


def tool_layout(args: dict) -> list[dict]:
    """Show or change the coordinates this server uses (they live in the settings file, see the recalibrate skill)."""
    op = str(args.get("op") or "get")
    if op == "set":
        name = str(args.get("name") or "")
        value = args.get("value")
        if value is None and args.get("x") is not None:
            value = [args.get("x"), args.get("y")]
        path = save_layout_value(name, value)
        return [{"type": "text", "text": f"Saved {name} = {value} for {_platform_key()} in {path}. "
                                         "Try the action once and check the screenshot."}]
    if op != "get":
        raise Measurement("op is get or set")
    values = {}
    for name in sorted(LAYOUT_KEYS):
        value = globals().get(name)
        values[name] = list(value) if isinstance(value, tuple) else value
    info = {"platform": _platform_key(), "settings_file": str(config_path()),
            "overridden": list(_LOADED_LAYOUT), "reference_window_size": list(STANDARD_SIZE),
            "current_window_size": list(_WIN_SIZE.get("main") or []), "values": values, "limits": limits(),
            "action_log": str(action_log_path()) if _log_settings()[0] else "off"}
    return [{"type": "text", "text": json.dumps(info, ensure_ascii=False, indent=1)}]


def tool_status(_args: dict) -> list[dict]:
    visible = _line_visible()
    if not visible:
        return [{"type": "text", "text": "The LINE window is not showing (LINE may be resident in the tray). line_open can bring the window up." + _watch_note()}]
    width, height = _geometry(_state(False, restore=False))
    return [{"type": "text", "text": f"The LINE window is showing ({width}x{height}). " + _watch_note()}]


def tool_open(_args: dict) -> list[dict]:
    ensure_window()
    return _shot("Screenshot taken after bringing LINE to the front. Before reading it, check that it really is LINE (the dark blue strip on the left and the chat list).")


def _which(args: dict) -> str:
    which = args.get("window", "main")
    if which not in ("main", "popup"):
        raise Measurement("window must be main or popup (a separate window such as a menu)")
    return which


def tool_screenshot(args: dict) -> list[dict]:
    ensure_window()
    which = _which(args)
    if which == "popup":
        return _shot("Screenshot of the popup (menu) window (taken without bringing it to the front).", "popup", restore=False)
    if _bg_ok():
        return _shot("Screenshot of LINE taken in the background (not brought to the front, cursor not moved).", background=True)
    return _shot("Screenshot retaken after bringing LINE to the front.")


MENU_MAX_SIZE = (260, 420)     # a popup window up to this size is treated as a menu; a larger one is a dialog
VERIFY_NOTE = ("The server cannot read the name on the screen itself: verify on the returned screenshot that the "
               "dialog names exactly '{target}' and that this is the intended action ({action}).")


def _manage_guard(args: dict, reserve: bool = True) -> str | None:
    """Clicks/keys that confirm a management action (leave a group, invite, remove a member, ...) must name
    the action and the target read from the screen. The limit is checked and the action is RECORDED before the
    click or key happens (fail closed). The server cannot OCR-verify the target; the caller is told to check the
    screenshot. action=menu (None here) is only for opening or choosing in small menus; see _menu_only_guard."""
    action = str(args.get("action") or "").strip()
    if not action or action == "menu":
        return None
    if action not in MANAGE_ACTIONS:
        raise Measurement(f"unknown action {action}. Use menu, or one of: {', '.join(sorted(MANAGE_ACTIONS))}")
    target = str(args.get("expected_target") or "").strip()
    if not target:
        raise Measurement(f"action={action} needs expected_target: the chat / group / person name you read on the "
                          "screen just now (take line_screenshot first). Nothing was done.")
    if reserve:
        reserve_action("manage", action, target)
    return action


MENU_TOKEN_SECONDS = 30
_MENU: dict = {"at": 0.0, "uses": 0}


def _arm_menu() -> None:
    """A click that opens a menu (a right click, the chat's menu button, or a click declared with opens_menu=true)
    allows exactly ONE action=menu click in the popup that appears. A click that leads on to a submenu declares
    opens_menu=true again."""
    _MENU.update(at=time.time(), uses=1, size=None)


def _menu_only_guard(args: dict) -> None:
    """action=menu in a popup is accepted only (a) shortly after a click in this server opened a menu, and (b) while
    the popup is menu-sized. Anything else (a dialog, or a popup nobody opened as a menu) needs the management
    action it confirms. The server cannot read button labels, so this is a gate against accidents, not proof that
    a clicked button is harmless."""
    size = _popup_size()
    fresh = time.time() - float(_MENU.get("at") or 0) <= MENU_TOKEN_SECONDS and int(_MENU.get("uses") or 0) > 0
    if not fresh or size is None or size[0] > MENU_MAX_SIZE[0] or size[1] > MENU_MAX_SIZE[1]:
        raise Measurement("action=menu is only for items of a menu that a click made through this server just opened "
                          "(right click, the chat menu button, or opens_menu=true on the opening click), for one click only, and only while "
                          "the popup is menu-sized. This popup may be a dialog with a confirm button: use the "
                          "management action it confirms (leave, invite, remove_member, ...) with expected_target. "
                          "Nothing was clicked.")
    _MENU["uses"] = 0                      # one click only; a popup of another size after it is a new window anyway
    _MENU["size"] = size


LIST_RIGHT = 330                               # Windows: the chat list column is to the left of this x


def send_region(height: int) -> tuple[int, int] | None:
    """The part of the main window where a message is sent: (min_x, min_y) in window coordinates, every click at or
    below/right of it is refused. Derived from the layout: the strip starts COMPOSER_FROM_BOTTOM + 70 above the bottom.
    Windows: right of the chat list. macOS: the whole width of a chat window (the chat list window has none).
    The settings file can override it: {"send_region": {"min_x": 0, "strip": 173}}."""
    cfg = {}
    try:
        cfg = load_config().get("send_region") or {}
    except Measurement:
        cfg = {}
    if IS_MAC:
        if (_WIN_TITLE.get("main") or "").strip() == APP and "min_x" not in cfg:
            return None                              # the chat list window: no input box in it
        min_x = 0
    else:
        min_x = LIST_RIGHT
    min_x = int(cfg.get("min_x", min_x))
    strip = int(cfg.get("strip", COMPOSER_FROM_BOTTOM + 70))
    return min_x, height - strip


def _navigation_guard(x: int, y: int, width: int, height: int) -> None:
    """Applied to every click and drag in the main window, WHATEVER the action says (a management action never
    clicks in the input row or on the send button): that area is where a message is sent, and only line_send does
    that (it checks the chat and counts and logs the send)."""
    region = send_region(height)
    if region and x >= region[0] and y >= region[1]:
        raise Measurement("clicks in the input row / send button area at the bottom of the chat pane are refused, with or "
                          "without an action: that is where a message is sent, and only line_send does that (it checks the "
                          "chat and counts and logs the send). Nothing was clicked.")


def _confirm_point_guard(x: int, y: int, args: dict) -> None:
    """A plain main-window click close to a known confirm button of a management notice (the leave confirmation) must
    carry the management action and the target. The server cannot tell what other buttons do; clicks elsewhere in the
    main window stay navigation (documented limit)."""
    if str(args.get("action") or "").strip() in MANAGE_ACTIONS:
        return
    for name in MANAGE_CONFIRM_POINTS:
        px, py = globals()[name]
        if abs(x - px) <= CONFIRM_RADIUS and abs(y - py) <= CONFIRM_RADIUS:
            raise Measurement(f"this click is next to the confirm button of a management notice ({name}): pass the "
                              "management action (leave, ...) and expected_target. Nothing was clicked.")


def _with_verify_note(blocks: list[dict], args: dict, action: str | None) -> list[dict]:
    if action and blocks and blocks[0].get("type") == "text":
        blocks[0]["text"] += " " + VERIFY_NOTE.format(target=str(args.get("expected_target") or "").strip(), action=action)
    return blocks


def _point_arg(args: dict) -> tuple[int, int]:
    name = str(args.get("point") or "").strip()
    if name:
        if name not in NAMED_POINTS:
            raise Measurement(f"unknown point {name}. Known: {', '.join(NAMED_POINTS)}")
        value = globals()[name]
        return int(value[0]), int(value[1])
    return _int(args, "x"), _int(args, "y")


def tool_click(args: dict) -> list[dict]:
    """Click in the LINE window. Management clicks are guarded; an unannotated click in the main window is
    allowed (it is navigation: choosing a chat, a tab) and is not counted."""
    action = str(args.get("action") or "").strip()
    return _with_verify_note(_tool_click(args), args, action if action in MANAGE_ACTIONS else None)


def _tool_click(args: dict) -> list[dict]:
    ensure_window()
    _remember_origin(allow_fg=not _bg_ok())
    which = _which(args)
    x, y = _point_arg(args)
    raw_count = int(args.get("count", 1) or 1)
    if raw_count != 1 and (which == "popup" or str(args.get("action") or "").strip() in MANAGE_ACTIONS):
        raise Measurement("clicks inside a popup, and clicks with a management action, must have count=1: a second "
                          "click would land on whatever the first one opened, without being checked again. "
                          "Nothing was clicked.")
    if which == "popup" and not args.get("action"):
        raise Measurement("clicks inside a menu or dialog (window=popup) must say what they do: action=menu for "
                          "ordinary menu items, or the management action they confirm (leave, invite, ...) together "
                          "with expected_target. Nothing was clicked.")
    if which == "popup" and str(args.get("action") or "").strip() == "menu":
        _menu_only_guard(args)
    action = _manage_guard(args, reserve=False)      # validates; the limit and the record come right before the click
    button = args.get("button", "left")
    if button not in ("left", "right", "middle"):
        raise Measurement("button must be left, right or middle")
    count = max(1, min(int(args.get("count", 1) or 1), 3))
    opens_menu = which == "main" and not action and (
        button == "right" or bool(args.get("opens_menu")) or
        (abs(x - CHAT_MENU_BUTTON[0]) <= 12 and abs(y - CHAT_MENU_BUTTON[1]) <= 12))
    if _bg_ok() and which == "main" and button == "left" and count == 1 and not action:
        # A single left click on the main window is delivered straight to the window (not brought to the front, cursor not moved; measured 2026-11-04).
        # Right clicks, multiple clicks and menus (separate windows) are pressed in the foreground as before, because a menu closes unless the window is in front.
        width, height = _geometry(_state(False, background=True), minimum=1)
        check_in_window(x, y, width, height)
        _navigation_guard(x, y, width, height)
        _confirm_point_guard(x, y, args)
        if opens_menu:
            _arm_menu()
        _orca(["computer", "bg-click", "--app", APP, "--session", SESSION, "--window-id", str(_window_id()),
               "--x", str(x), "--y", str(y)])
        time.sleep(0.8)
        return _shot(f"Screen after a background left click at ({x},{y}) in the window (not brought to the front, cursor not moved). "
                     "If you right-clicked or opened a menu, look at the open window with line_screenshot window=popup.", background=True)
    width, height = _geometry(_ready(which), minimum=1)
    check_in_window(x, y, width, height)
    if which == "main":
        _navigation_guard(x, y, width, height)       # also with a management action
        _confirm_point_guard(x, y, args)
    if opens_menu:
        _arm_menu()
    click_tail = _base_tail(which)                   # resolves the target window first: no window, nothing is counted
    if action:
        reserve_action("manage", action, str(args.get("expected_target") or "").strip())   # limit + record BEFORE the click
    _orca(["computer", "click", *click_tail, "--x", str(x), "--y", str(y),
           "--mouse-button", button, "--click-count", str(count)])
    time.sleep(0.8)
    if which == "popup" and str(args.get("action") or "").strip() == "menu" and args.get("opens_menu"):
        _arm_menu()                                  # this item opens a submenu: one more menu click is allowed
    if button == "right":
        # A right-click menu closes when the main window is brought to the front. If one is open, capture it without bringing the window forward
        try:
            return _shot(f"Menu (a separate window) after a right click at ({x},{y}) in the window. Coordinates are relative to this window's top-left. "
                         "To press an item, use line_click window=popup.", "popup", restore=False)
        except Measurement:
            pass
    note = f"Screen after clicking at ({x},{y}) in the window with the {button} button, {count} time(s)"
    if which == "popup":
        note += " (the popup window was clicked)"
    tail = ". If you right-clicked or opened a menu, look at the open window with line_screenshot window=popup."
    try:
        return _shot(note + ". The screen shown is the main window" + tail if which == "popup" else note + tail)
    except Measurement:
        if which != "popup":
            raise
        # If another dialog opens after the click (renaming a friend, creating a group, etc.), the main window disappears from the window list.
        # The click took effect, so do not treat it as a failure; capture the popup that is open and return it (measured 2026-10-28)
        return _shot(note + ". The main window is hidden (a dialog opened), so the popup window that is open was captured.", "popup", restore=False)


def tool_drag(args: dict) -> list[dict]:
    ensure_window()
    _remember_origin(allow_fg=False)
    x1, y1, x2, y2 = (_int(args, k) for k in ("x1", "y1", "x2", "y2"))
    width, height = _geometry(_ready())
    check_in_window(x1, y1, width, height)
    check_in_window(x2, y2, width, height)
    _navigation_guard(x1, y1, width, height)
    _navigation_guard(x2, y2, width, height)
    _orca(["computer", "drag", *_base_tail(), "--from-x", str(x1), "--from-y", str(y1),
           "--to-x", str(x2), "--to-y", str(y2)])
    time.sleep(0.8)
    return _shot(f"Screen after dragging from ({x1},{y1}) to ({x2},{y2}) in the window.")


def _bg_tail() -> list[str]:
    return ["--app", APP, "--session", SESSION, "--window-id", str(_window_id())]


RETURN_KEYS = {"return", "enter", "kp_enter", "numpadenter", "numpad_enter"}
SPACE_KEYS = {"space", "spacebar"}          # Space presses the focused button, like Return
CONFIRM_KEYS = RETURN_KEYS | SPACE_KEYS


def is_return_family(key: str) -> bool:
    """True for Return/Enter alone or with any modifiers (alt+Return, ctrl+Return, shift+Return, ...). Any of
    these can send a message depending on LINE's setting, and the configured send key is one of them."""
    last = key.strip().rsplit("+", 1)[-1].strip().lower()
    return last in CONFIRM_KEYS or key.strip().lower() == _norm_key(SEND_KEY).lower()


def _return_key_guard(key: str, args: dict) -> str:
    """Messages are sent only by line_send (which counts, logs and checks the chat). line_key therefore refuses
    every Return-family key unless it presses a plain Return inside a dialog (window=popup) that a management
    action confirms (action + expected_target). Returns that action."""
    if not is_return_family(key):
        return ""
    if args.get("window") != "popup":
        raise Measurement("line_key refuses Return/Enter/Space (and ctrl/alt/shift+Return and the send key) in the main "
                          "window: they could send a message or press a focused confirm button without the limits, "
                          "the log and the chat check. Use line_send to send. To confirm a dialog, pass "
                          "window=popup with a management action and expected_target. Nothing was pressed.")
    if "+" in key.strip() or key.strip().lower() not in CONFIRM_KEYS:
        raise Measurement("only a plain Return or Space may be pressed in a dialog (window=popup); modified Return "
                          "keys are send keys. Nothing was pressed.")
    action = str(args.get("action") or "").strip()
    if action not in MANAGE_ACTIONS:
        raise Measurement("Return/Space in a dialog needs a management action (leave, invite, remove_member, ...) and "
                          "expected_target, because it confirms the dialog. For an ordinary menu, click the item "
                          "with action=menu. Nothing was pressed.")
    if _confirm_popup_size() is None:
        raise Measurement("no dialog window is open (or the window could not be told apart from a chat window), so the "
                          "key could go to the chat and send its draft. Nothing was pressed.")
    return action


def tool_key(args: dict) -> list[dict]:
    key = _norm_key(str(args.get("key", "")))
    _return_key_guard(key, args)                 # before anything is touched
    action = str(args.get("action") or "").strip()
    return _with_verify_note(_tool_key(args, key), args, action if action in MANAGE_ACTIONS else None)


def _tool_key(args: dict, key: str) -> list[dict]:
    ensure_window()
    _remember_origin(allow_fg=False)
    action = _manage_guard(args)
    if _bg_ok() and "+" not in key and key.lower() in BG_KEYS and not action:
        _state(False, background=True)
        _orca(["computer", "bg-key", *_bg_tail(), "--key", key.lower()])
        time.sleep(0.6)
        return _shot(f"Pressed {key} in the background (LINE was not brought to the front).", background=True)
    _ready("popup" if args.get("window") == "popup" else "main")
    verb = "hotkey" if "+" in key else "press-key"
    tail = _base_tail("popup") if args.get("window") == "popup" else _base_tail()
    _orca(["computer", verb, *tail, "--key", key])
    time.sleep(0.8)
    return _shot(f"Screen after pressing key [{key}].")


def tool_type(args: dict) -> list[dict]:
    """Paste as is where the text input focus currently is. Never sends: control characters (a typed Return or
    similar) are refused, and Return keys are refused by line_key, so no type-then-Return chain exists."""
    text = str(args.get("text", ""))
    if not text:
        raise Measurement("text is empty")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise Measurement("text contains control characters (such as a carriage return or escape). Nothing was typed.")
    ensure_window()
    _remember_origin(allow_fg=False)
    if _bg_ok() and "\n" not in text and "\r" not in text and "\t" not in text:
        _state(False, background=True)
        _orca(["computer", "bg-chars", *_bg_tail(), "--text", text])
        time.sleep(0.6)
        return _shot("Typed the text into the focused field in the background (not sent; LINE stayed behind).",
                     background=True)
    _ready()
    _orca(["computer", "paste-text", *_base_tail(), "--text", text])
    time.sleep(0.8)
    return _shot("Screen after pasting text where the focus is (nothing was sent).")


def tool_scroll(args: dict) -> list[dict]:
    ensure_window()
    _remember_origin(allow_fg=not _bg_ok())
    direction = args.get("direction", "down")
    if direction not in ("up", "down"):
        raise Measurement("direction must be up or down")
    pages = max(1, min(int(args.get("pages", 1) or 1), 5))
    point = CHAT_POINT if args.get("target") == "chat" else LIST_POINT
    if _bg_ok():
        _state(False, background=True)
        _orca(["computer", "bg-scroll", "--app", APP, "--session", SESSION, "--window-id", str(_window_id()),
               "--x", str(point[0]), "--y", str(point[1]), "--direction", direction, "--pages", str(pages)])
        time.sleep(0.8)
        return _shot(f"Screen after scrolling the {'conversation' if args.get('target') == 'chat' else 'chat list'} {direction} by {pages} page(s) "
                     "in the background (not brought to the front, cursor not moved).", background=True)
    _ready()
    _orca(["computer", "scroll", *_base_tail(), "--x", str(point[0]), "--y", str(point[1]),
           "--direction", direction, "--pages", str(pages)])
    time.sleep(0.8)
    return _shot(f"Screen after scrolling the {'conversation' if args.get('target') == 'chat' else 'chat list'} {direction} by {pages} page(s).")


def tool_search(args: dict) -> list[dict]:
    """Put text in the search box. Empty text clears the search. Enter is not pressed."""
    text = str(args.get("text", ""))
    if len(text) > 60 or "\n" in text or "\r" in text:
        raise Measurement("the search text must be one line of at most 60 characters")
    ensure_window()
    _remember_origin(allow_fg=not _bg_ok())
    if _bg_ok():
        _state(False, background=True)
        tail = _bg_tail()
        _orca(["computer", "bg-click", *tail, "--x", str(SEARCH_BOX[0]), "--y", str(SEARCH_BOX[1])])
        time.sleep(0.3)
        _orca(["computer", "bg-key", *tail, "--key", "end"])
        _orca(["computer", "bg-key", *tail, "--key", "backspace", "--repeat", "80"])
        if text:
            _orca(["computer", "bg-chars", *tail, "--text", text])
        # No Escape here: on an empty search box with no chat open, Escape minimizes LINE (measured 2026-11-15).
        time.sleep(1.2)
        note = f"Typed \"{text}\" into the search box in the background." if text else "Cleared the search box."
        return _shot(note + " If the box did not change, try once more.", background=True)
    _ready()
    tail = _base_tail()
    _orca(["computer", "click", *tail, "--x", str(SEARCH_BOX[0]), "--y", str(SEARCH_BOX[1])])
    _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
    if text:
        _orca(["computer", "paste-text", *tail, "--text", text])
    else:
        _orca(["computer", "press-key", *tail, "--key", "Delete"])
    time.sleep(1.2)
    note = f"Screen after entering '{text}' in the search box." if text else "Screen after clearing the search box."
    return _shot(note + " If the search box did not change, retry once.")


_MENTION = re.compile(r"@\[([^\[\]\r\n]{1,40})\]")


def split_mentions(text: str) -> list[tuple[str, str]]:
    """Split a message into plain text (text) and mentions (mention = @[display name])."""
    parts: list[tuple[str, str]] = []
    pos = 0
    for match in _MENTION.finditer(text):
        if match.start() > pos:
            parts.append(("text", text[pos:match.start()]))
        parts.append(("mention", match.group(1).strip()))
        pos = match.end()
    if pos < len(text):
        parts.append(("text", text[pos:]))
    return parts


def declared_send_key() -> str | None:
    """The send key the USER declared (settings file layout SEND_KEY, or the LINE_SEND_KEY environment variable),
    only if it is a modified Return/Enter (alt+Return ...). A per-call send_key does not count. None otherwise."""
    value = os.environ.get("LINE_SEND_KEY", "").strip()
    if not value:
        try:
            value = str(((load_config().get("layout") or {}).get(_platform_key()) or {}).get("SEND_KEY") or "").strip()
        except Measurement:
            value = ""
    if not value:
        return None
    key = _norm_key(value)
    if "+" in key and key.rsplit("+", 1)[-1].strip().lower() in RETURN_KEYS:
        return key
    return None


def _plain_return_sends(key: str) -> bool:
    """True when a plain Return would send (the send key is Return itself), so Return must never be pressed to pick
    a mention candidate."""
    return _norm_key(key).lower() in RETURN_KEYS


def _compose_with_mentions(tail: list[str], parts: list[tuple[str, str]]) -> None:
    """Enter text and mentions, in order, into the (already emptied) input box.

    Measured 2026-10-27: typing "@" shows a list of the group's members. **Pasting** the name narrows the candidates (typing Japanese
    with type-text gets mangled by the IME), and Enter turns the match into a blue "@name" mention. Enter confirms only while the candidate
    list is showing; for a name with no candidates Enter inserts a newline, so always check the dry_run image.
    """
    need_space = False
    for kind, value in parts:
        _require_front()
        if kind == "text":
            if not value:
                continue
            if need_space and not value[0].isspace():
                value = " " + value
            need_space = False
            _orca(["computer", "paste-text", *tail, "--text", value])
        else:
            _orca(["computer", "type-text", *tail, "--text", "@"])
            time.sleep(0.8)
            _require_front()
            _orca(["computer", "paste-text", *tail, "--text", value])
            time.sleep(1.2)
            _require_front()
            # Return picks the highlighted candidate. The candidate list cannot be verified from here, so line_send
            # only gets here when the send key is a modified key (alt+Return ...), where Return cannot send.
            _orca(["computer", "press-key", *tail, "--key", "Return"])
            time.sleep(0.6)
            need_space = True


def _composer_draft(tail: list[str]) -> str | None:
    """Text already in the message box (selected with ctrl+a just before), read through the clipboard and the
    clipboard put back. None when it cannot be checked without losing a non-text clipboard (image, files)."""
    if IS_MAC or BACKEND != "native":
        return None
    before = _clip_get()
    if before["has_other"]:
        raise Measurement("the clipboard holds an image or files, so the message box cannot be checked for an unsent "
                          "draft without losing them. Nothing was sent. Ask the user to copy some text (or clear the "
                          "clipboard) and retry; replace_draft=true sends anyway and discards any draft.")
    got = None
    try:
        _clip_set("")
        _orca(["computer", "hotkey", *tail, "--key", "ctrl+c"])
        time.sleep(0.3)
        got = _clip_get()["text"]
    finally:
        _clip_restore_if_unchanged(before["text"], got)
    return got if (got or "").strip() else None


def _plain(text: str) -> str:
    """How a message looks once pasted: @[name] mentions become @name; spacing ignored."""
    return re.sub(r"\s+", "", re.sub(r"@\[([^\]]+)\]", r"@\1", text or ""))


DRY_RUN_TTL = 600                                # seconds a dry_run record stays valid (10 minutes)
_LAST_DRY: dict = {"fp": None, "hash": None, "at": 0.0}     # (chat header, body) of our own last dry_run: only that text is "ours"


def _body_hash(body: str) -> str:
    return hashlib.sha256(_plain(body).encode("utf-8")).hexdigest()


def _is_own_dry_run(draft: str, body: str, fp: str | None) -> bool:
    """The box text is our own earlier dry_run only if it matches what we pasted, in the same chat."""
    if time.time() - float(_LAST_DRY.get("at") or 0) > DRY_RUN_TTL:
        return False
    return bool(fp) and _LAST_DRY.get("fp") == fp and _LAST_DRY.get("hash") == _body_hash(body) \
        and _plain(draft) == _plain(body)


def _forget_dry_run() -> None:
    _LAST_DRY.update(fp=None, hash=None, at=0.0)


def _header_now() -> str | None:
    try:
        return header_fingerprint(_view_png())
    except Measurement:
        return None


def _draft_text_note(draft: str) -> str:
    return f" The user's unsent draft ({len(draft)} characters) was:{chr(10)}{draft}"


def _restore_draft(tail: list[str], cx: int, cy: int, draft: str, orig_fp: str | None) -> tuple[bool, str]:
    """Put the user's unsent draft back into the message box, but only if the open chat is still the original one
    (its header fingerprint equals the one taken at the start). Otherwise nothing is pasted and the draft text is
    handed back in the note. Any exception, wherever it happens, also hands the text back. Returns (pasted, note)."""
    try:
        now = _header_now()
        if not orig_fp or now != orig_fp:
            return False, ("The open chat could not be confirmed as the original one, so the draft was NOT pasted back."
                           + _draft_text_note(draft))
        _orca(["computer", "click", *tail, "--x", str(cx), "--y", str(cy)])
        _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
        _orca(["computer", "paste-text", *tail, "--text", draft])
        return True, f" The user's unsent draft ({len(draft)} characters) was put back into the message box."
    except BaseException as exc:  # noqa: BLE001 - the text must come back whatever happened
        return False, f"The draft could not be pasted back ({type(exc).__name__}: {exc})." + _draft_text_note(draft)


def tool_send(args: dict) -> list[dict]:
    """Send a message to the chat that is currently open (click the input box -> select all -> paste -> send key).

    Before sending, read the chat name (top of the right pane) in a screenshot to confirm it.
    `expected_chat`  is for recording the chat name you verified (the tool does not read images).
    """
    text = str(args.get("text", "")).rstrip()
    if not text:
        raise Measurement("text is empty")
    expected = str(args.get("expected_chat", "")).strip()
    if not expected:
        raise Measurement("expected_chat (the chat name verified on screen) is required. Before sending, read the name at the top right with line_screenshot")
    signature = str(args.get("signature", "")).strip()
    body = f"{text}\n\n{signature}" if signature else text
    parts = split_mentions(body)
    has_mentions = any(kind == "mention" for kind, _ in parts)
    key = _norm_key(str(args.get("send_key") or os.environ.get("LINE_SEND_KEY") or SEND_KEY))
    if has_mentions:
        declared = declared_send_key()
        if not declared:
            raise Measurement("mentions (@[name]) are refused: picking a mention candidate presses Return, which sends the "
                              "message if LINE is set to send on Enter, and this server cannot read LINE's setting. They "
                              "are allowed only when the user has declared a MODIFIED send key (for example alt+Return) "
                              "in the settings file (line_layout op=set name=SEND_KEY, or LINE_SEND_KEY set by the user). "
                              "A send_key argument does not count. Nothing was done.")
        key = declared
    if has_mentions and not args.get("dry_run") and not args.get("mentions_checked"):
        raise Measurement("a message containing a mention (@[name]) must first be pasted with dry_run=true and checked on the image. Check that the blue mention shows "
                          "the intended person (if several people share a name, the first is chosen), then send with the same arguments plus mentions_checked=true")
    can_read_draft = not (IS_MAC or BACKEND != "native")
    if not can_read_draft and not args.get("replace_draft"):
        raise Measurement("this backend cannot read what is already in the message box, so an unsent draft of the user "
                          "could be overwritten and lost (also by dry_run). Nothing was done. If the box is empty or "
                          "the draft may be discarded, retry with replace_draft=true.")
    if not args.get("dry_run"):
        check_limit("send")
    ensure_window()
    chat_fp = require_same_chat()
    width, height = _geometry(_ready())
    tail = _base_tail()
    cx = int(args["composer_x"]) if args.get("composer_x") is not None else COMPOSER_X
    cy = int(args["composer_y"]) if args.get("composer_y") is not None else height - COMPOSER_FROM_BOTTOM
    check_in_window(cx, cy, width, height)
    orig_fp = chat_fp or _header_now()          # which chat this is; the draft is only ever pasted back into it
    _orca(["computer", "click", *tail, "--x", str(cx), "--y", str(cy)])
    _require_front()   # check again right before pasting and sending (the user may switch to another app midway)
    _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
    # An unsent draft in the box is set aside, the message is sent, and the draft is pasted back afterwards.
    # Text counts as our own earlier dry_run only if it matches the record of that dry_run (same chat, same body).
    # replace_draft=true discards the draft.
    if args.get("replace_draft"):
        _forget_dry_run()                           # the box is overwritten: an older record must not match later
    draft = None if args.get("replace_draft") else _composer_draft(tail)
    if draft is not None and _is_own_dry_run(draft, body, orig_fp):
        draft = None
        _forget_dry_run()                         # a dry_run record is good for one send only
    state = "unexecuted"                        # unexecuted -> unknown (key about to be pressed) -> pressed
    try:
        if has_mentions:
            _orca(["computer", "press-key", *tail, "--key", "Delete"])
            _compose_with_mentions(tail, parts)
        else:
            _orca(["computer", "paste-text", *tail, "--text", body])
        _require_front()
        time.sleep(0.6)
        if args.get("dry_run"):
            shot = _shot(f"[NOT SENT] The message is pasted into the input box of the chat named '{expected}'. Enter was not pressed.")
            if draft is not None:
                ok, note = _restore_draft(tail, cx, cy, draft, orig_fp)
                shot[0]["text"] += (note + (" The real send will set it aside again and restore it after sending." if ok else ""))
            else:
                _LAST_DRY.update(fp=orig_fp, hash=_body_hash(body), at=time.time())
            return shot
        _require_front()
        require_same_chat(chat_fp)                 # second check, right before the key that sends
        _forget_dry_run()                          # any real send ends the life of a dry_run record
        reserve_action("send", "send", expected, body)   # limit + log (cross-process lock) BEFORE the key; fails closed
        state = "unknown"                          # from here on a failure may mean the message went out
        _orca(["computer", "hotkey" if "+" in key else "press-key", *tail, "--key", key])
        state = "pressed"
    except Exception as exc:  # noqa: BLE001 - whatever failed, the user's draft must not be lost
        if draft is None:
            raise
        if state == "unexecuted":
            ok, note = _restore_draft(tail, cx, cy, draft, orig_fp)
            raise Measurement(f"{exc} Nothing was sent. {note}" + ("" if not ok else _draft_text_note(draft))) from exc
        raise Measurement(f"{exc} It is UNKNOWN whether the message was sent: look at the screen before trying again. "
                          "The draft was not pasted back." + _draft_text_note(draft)) from exc
    time.sleep(1.2)
    note = ""
    if draft is not None:
        _ok, note = _restore_draft(tail, cx, cy, draft, orig_fp)
        time.sleep(0.4)
    return _shot(f"Screen after pressing the send key in the chat named '{expected}' (key: {key}).{note} "
                 "**Check on the image that it was sent (a new bubble appeared).**")


def tool_scan_list(args: dict) -> list[dict]:
    """Capture the chat list page by page from the top (the unread count is shown at the right of each row). Stops when the same screen repeats."""
    ensure_window()
    max_pages = max(1, min(int(args.get("max_pages", 6) or 6), 10))
    if _bg_ok():
        return _scan_list_background(max_pages)
    _ready()
    tail = _base_tail()
    # Remember the view before the work (search text, tab, list position); put it back after the scan.
    png0 = _png_bytes(_state(True))
    orig_tab, searching = _active_tab(png0), _search_active(png0)
    orig_search = None
    orig_hash = _list_hash(png0) if searching is False else None      # None (unknown) is not "no search"
    out: list[dict] = []
    last = None
    taken = 0
    match_page = None
    failure: Exception | None = None
    scan_note = ""
    cleared = False
    try:
        if searching:
            try:
                orig_search = _read_search_text()
            except Measurement:
                pass
            if orig_search is None:             # the text cannot be read back later, so the box is left alone
                scan_note = ("A search is in the box and its text could not be read, so it was NOT cleared: the pages "
                             "below are the filtered list (search results only), not the whole chat list. ")
            else:
                _set_search_text("")            # a leftover search filters the list, so the unread counts would be wrong
                cleared = True
                tail = _base_tail()
        _orca(["computer", "click", *tail, "--x", str(ALL_TAB[0]), "--y", str(ALL_TAB[1])])
        _orca(["computer", "scroll", *tail, "--x", str(LIST_POINT[0]), "--y", str(LIST_POINT[1]),
               "--direction", "up", "--pages", "10"])
        time.sleep(1.0)
        for page in range(1, max_pages + 1):
            png = _png_bytes(_state(True))
            digest = hashlib.sha256(png).hexdigest() if png else None
            if png is None or digest == last:
                break
            last = digest
            taken += 1
            if orig_hash and match_page is None and _list_hash(png) == orig_hash:
                match_page = page
            _require_front()
            out.append({"type": "text", "text": f"Chat list, page {page}"})
            out.append(_image_block(png))
            _orca(["computer", "scroll", *tail, "--x", str(LIST_POINT[0]), "--y", str(LIST_POINT[1]),
                   "--direction", "down", "--pages", "1"])
            time.sleep(0.8)
        # return the list to the top (so the next action's coordinates match the first captured page)
        _orca(["computer", "scroll", *tail, "--x", str(LIST_POINT[0]), "--y", str(LIST_POINT[1]),
               "--direction", "up", "--pages", "10"])
    except Exception as exc:  # noqa: BLE001 - reported after the view has been put back
        failure = exc
    finally:
        # Runs on every path, including the clearing of the search box above: the view is put back whatever happened.
        back = _restore_scan_view(orig_tab, orig_search, orig_hash, match_page,
                                  search_was_active=None if searching is None else cleared)
    if failure is not None:
        raise Measurement(f"{scan_note}the scan stopped midway ({failure}). " + back) from failure
    head = (scan_note + f"Captured {taken} page(s) of the chat list ({'to the end' if taken < max_pages else f'up to the limit of {max_pages} pages'}). "
            "The number at the right of each row is the unread count. Muted rows (struck-through speaker icon) are counted too." + back)
    return [{"type": "text", "text": head}, *out]


def _scan_list_background(max_pages: int) -> list[dict]:
    """Background version of tool_scan_list (2026-11-04). Delivers clicks and wheel events straight to the window, without bringing it to the front or moving the cursor.
    It only presses the "All" tab and scrolls the list; no chat is opened (nothing is marked as read). Safe to continue while the user is active."""
    _state(False, background=True)                      # restore in the background if minimized
    tail = ["--app", APP, "--session", SESSION, "--window-id", str(_window_id())]
    lx, ly = str(LIST_POINT[0]), str(LIST_POINT[1])
    # Remember the view before the work. A leftover search filters the list (the unread counts would be wrong), so clear it for the scan
    # and put it back afterwards. Reading the search text needs the foreground (Ctrl+A, Ctrl+C), so that part waits for a pause in the user's input.
    cleared_note = ""
    restore_text = None
    cleared = False
    png0 = _png_bytes(_state(True, background=True))
    orig_tab = _active_tab(png0)
    searching = _search_active(png0)
    orig_hash = _list_hash(png0) if searching is False else None      # None (unknown) is not "no search"
    match_page = None
    out: list[dict] = []
    last = None
    taken = 0
    failure: Exception | None = None
    try:
        if searching:
            try:
                restore_text = _read_search_text()
                if restore_text is None:        # unreadable: it could not be put back, so it is left alone
                    cleared_note = ("A search is in the box and its text could not be read, so it was NOT cleared: the "
                                    "pages below are the filtered list (search results only), not the whole chat list. ")
                else:
                    _set_search_text("")
                    cleared = True
                    cleared_note = "A search was still in the box; cleared it for the scan. "
                    tail = ["--app", APP, "--session", SESSION, "--window-id", str(_window_id())]
            except Measurement as e:
                cleared_note = f"A search is still in the box and could not be cleared ({e}). The list shows search results, so unread counts were not read. "
        _orca(["computer", "bg-click", *tail, "--x", str(ALL_TAB[0]), "--y", str(ALL_TAB[1])])
        _orca(["computer", "bg-scroll", *tail, "--x", lx, "--y", ly, "--direction", "up", "--pages", "10"])
        time.sleep(1.0)
        for page in range(1, max_pages + 1):
            png = _png_bytes(_state(True, background=True))
            digest = hashlib.sha256(png).hexdigest() if png else None
            if png is None or digest == last:
                break
            last = digest
            taken += 1
            if orig_hash and match_page is None and _list_hash(png) == orig_hash:
                match_page = page
            out.append({"type": "text", "text": f"Chat list, page {page}"})
            out.append(_image_block(png))
            _orca(["computer", "bg-scroll", *tail, "--x", lx, "--y", ly, "--direction", "down", "--pages", "1"])
            time.sleep(0.8)
        _orca(["computer", "bg-scroll", *tail, "--x", lx, "--y", ly, "--direction", "up", "--pages", "10"])
    except Exception as exc:  # noqa: BLE001 - reported after the view has been put back
        failure = exc
    finally:
        # Runs on every path, including the clearing of the search box above.
        back = _restore_scan_view(orig_tab, restore_text, orig_hash, match_page,
                                  search_was_active=None if searching is None else cleared)
    if failure is not None:
        raise Measurement(f"{cleared_note}the scan stopped midway ({failure}). " + back) from failure
    cleared_note += back
    head = cleared_note + (f"Captured {taken} page(s) of the chat list in the background (not brought to the front, cursor not moved, "
            f"{'to the end' if taken < max_pages else f'up to the limit of {max_pages} pages'}). "
            "The number at the right of each row is the unread count. Muted rows (struck-through speaker icon) are counted too.")
    return [{"type": "text", "text": head}, *out]


# ---- Leave the screen as you found it: remember the view before the work, put it back afterwards ----------------
# Remember (_remember_origin) -> work -> restore (_restore_origin) in three steps. Tools that open a chat, search, click,
# scroll or type remember the view once (the first call wins); line_read_chat_text restores automatically (restore_view,
# default true) and line_scan_list remembers and restores on its own. line_restore_view is the manual way back.
# What is remembered: the search text, the open chat (header picture + name when it can be read), the tab above the list and the
# list scroll position. Anything that cannot be put back is said in the result text (nothing is dropped silently).
# Windows only (the Mac helper has a different window model: nothing is done there).
# Standard library only. The pixel checks decode our own filter-0 screenshots (see header_fingerprint). Reading chat names
# from the screen uses the Windows OCR engine through PowerShell when it is available and is optional: without it the
# server can still put back the search text, the tab and the list position, and it reopens a chat by a remembered name only.
TAB_UNDERLINE_DY = 20                          # the dark underline of the active tab is this many pixels below the tab point
HEADER_NAME_BOX = (HEADER_BOX[0] + 7, HEADER_BOX[1], HEADER_BOX[0] + 231, HEADER_BOX[3] - 2)   # chat name in the header (not the icons at the right)
LIST_HASH_BOX = (62, 100, 300, 722)            # body of the chat list (without the time and unread count at the right edge)
RESULT_BOX = (70, 110, 312, 700)               # search results, read with OCR when a chat has to be reopened
ORIGIN_TTL = 1800                              # seconds a remembered view stays valid
_ORIGIN: dict = {}
_PENDING_WARN: list[str] = []      # notes for the agent that the next screenshot result should carry (once)
_PIX: dict = {"png": None, "img": None}

_OCR_SCRIPT = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null=[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null=[Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]
function Await($op,$t){$m=[System.WindowsRuntimeSystemExtensions].GetMethods()|?{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'}|select -first 1;$k=$m.MakeGenericMethod($t).Invoke($null,@($op));$k.Wait();$k.Result}
$a=([Console]::In.ReadToEnd()).Trim().Split('|')
$l=[int]$a[1];$t=[int]$a[2];$w=[int]$a[3]-$l;$h=[int]$a[4]-$t;$s=[int]$a[5]
$src=[System.Drawing.Bitmap]::FromFile($a[0])
$dst=New-Object System.Drawing.Bitmap ($w*$s),($h*$s)
$g=[System.Drawing.Graphics]::FromImage($dst)
$g.InterpolationMode=[System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.DrawImage($src,(New-Object System.Drawing.Rectangle 0,0,($w*$s),($h*$s)),$l,$t,$w,$h,[System.Drawing.GraphicsUnit]::Pixel)
$g.Dispose();$src.Dispose()
$p=$a[0]+'.scaled.png'
$dst.Save($p,[System.Drawing.Imaging.ImageFormat]::Png);$dst.Dispose()
$f=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
$st=Await ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$d=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$b=Await ($d.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$e=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if(-not $e){Remove-Item $p -ErrorAction SilentlyContinue;exit 3}
$r=Await ($e.RecognizeAsync($b)) ([Windows.Media.Ocr.OcrResult])
foreach($ln in $r.Lines){ $w0=@($ln.Words)[0]; ([int]$w0.BoundingRect.Y).ToString()+'|'+$ln.Text }
$st.Dispose()
Remove-Item $p -ErrorAction SilentlyContinue
"""


def _decode_png(png: bytes | None):
    """Decode one of our own screenshots (8-bit RGB, every row filter 0) into (width, height, raw, stride). None for anything else."""
    import struct
    import zlib
    if not png or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if _PIX["png"] is png:
        return _PIX["img"]
    pos, width, height, data = 8, 0, 0, b""
    while pos + 8 <= len(png):
        length, tag = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            if depth != 8 or color != 2:
                return None
        elif tag == b"IDAT":
            data += body
        pos += 12 + length
    try:
        raw = zlib.decompress(data)
    except zlib.error:
        return None
    stride = width * 3 + 1
    img = None
    if width > 0 and height > 0 and len(raw) >= stride * height and not any(raw[0:stride * height:stride]):
        img = (width, height, raw, stride)
    _PIX["png"], _PIX["img"] = png, img
    return img


def _px(img, x: int, y: int):
    """(r, g, b) of one pixel, or None outside the image."""
    width, height, raw, stride = img
    if not (0 <= x < width and 0 <= y < height):
        return None
    i = y * stride + 1 + x * 3
    return raw[i], raw[i + 1], raw[i + 2]


def _search_active(png: bytes | None) -> bool | None:
    """Does the search box still hold text? The grey circle of the × at its right end only shows then. Looks at five pixels on the
    circle's left and right edge around SEARCH_CLEAR (the white × itself is not tested). None (unknown) when the picture cannot be read."""
    img = _decode_png(png)
    if img is None:
        return None
    cx, cy = SEARCH_CLEAR
    dark = 0
    for dx in (-7, -6, -5, 5, 6):
        p = _px(img, cx + dx, cy)
        if p and max(p) <= 215:
            dark += 1
    return dark >= 3


def _chat_open(png: bytes | None) -> bool | None:
    """Is a conversation open on the right (dark text in the chat-name strip of the header)? None if the picture cannot be read."""
    img = _decode_png(png)
    if img is None:
        return None
    left, top, right, bottom = HEADER_NAME_BOX
    count = 0
    for y in range(top, bottom):
        for x in range(left, right):
            p = _px(img, x, y)
            if p and max(p) < 130:
                count += 1
    return count >= 40


def _tab_points() -> list[tuple[int, int]]:
    """The tabs above the chat list, left to right (read at call time so a recalibrated layout is used)."""
    return [tuple(ALL_TAB), tuple(TAB_FRIENDS), tuple(GROUP_TAB), tuple(TAB_OPENCHAT), tuple(TAB_OFFICIAL)]


def _active_tab(png: bytes | None) -> int | None:
    """Index into _tab_points() of the tab with the dark underline, or None if it cannot be told."""
    img = _decode_png(png)
    if img is None:
        return None
    for i, (x, y) in enumerate(_tab_points()):
        for dy in (TAB_UNDERLINE_DY, TAB_UNDERLINE_DY + 1):
            p = _px(img, x, y + dy)
            if p and max(p) < 100:
                return i
    return None


def _list_hash(png: bytes | None) -> str | None:
    """Hash of the body of the chat list, to find the same scroll position again."""
    img = _decode_png(png)
    if img is None:
        return None
    width, height, raw, stride = img
    left, top, right, bottom = LIST_HASH_BOX
    right, bottom = min(right, width), min(bottom, height)
    if right <= left or bottom <= top:
        return None
    digest = hashlib.sha1()
    for y in range(top, bottom):
        digest.update(raw[y * stride + 1 + left * 3:y * stride + 1 + right * 3])
    return digest.hexdigest()


def _ocr_lines(png: bytes | None, box: tuple[int, int, int, int], scale: int = 3) -> list[tuple[int, str]] | None:
    """Read part of a screenshot with the Windows OCR engine (through PowerShell; cropping and enlarging are done there too, so no
    image library is needed). Returns (y in the window, text) per line, [] when nothing was read, and None when OCR is not available."""
    if not png or os.name != "nt" or not shutil.which("powershell"):
        return None
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png", prefix="line_ocr_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        out = _ps(_OCR_SCRIPT, stdin="|".join([path, *(str(v) for v in box), str(scale)]), timeout=40)
    except (Measurement, OSError):
        return None
    finally:
        for leftover in (path, path + ".scaled.png"):
            try:
                os.remove(leftover)
            except OSError:
                pass
    lines: list[tuple[int, str]] = []
    for raw in out.splitlines():
        y, _, text = raw.partition("|")
        if y.strip().lstrip("-").isdigit() and text.strip():
            lines.append((box[1] + int(y) // scale, text.strip()))
    return lines


def _norm_name(text: str) -> str:
    import unicodedata
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()
    s = re.sub(r"\(\d{1,4}\)\W*$", "", s)          # member or unread count at the end, "(18)"
    return re.sub(r"\W+$", "", s)                   # icons that OCR read as punctuation


def _name_score(a: str, b: str) -> float:
    import difflib
    a, b = _norm_name(a), _norm_name(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _search_query(name: str) -> str:
    """The word to type into the search box to find a chat again: the longest run of word characters (short, so OCR slips matter less)."""
    import unicodedata
    runs = re.findall(r"\w+", unicodedata.normalize("NFKC", name))
    return (max(runs, key=len) if runs else name)[:20]


def _read_header_name(png: bytes | None) -> str | None:
    """Name of the open chat read from the header with OCR. None when OCR is not available or nothing was read."""
    lines = _ocr_lines(png, HEADER_NAME_BOX, 3)
    return ("".join(t for _y, t in lines).strip() or None) if lines else None


def _view_png() -> bytes | None:
    """The screen right now (in the background when that works)."""
    return _png_bytes(_state(True, background=True)) if _bg_ok() else _png_bytes(_state(True))


def _read_search_text() -> str | None:
    """Read the text in the search box (Ctrl+A, Ctrl+C in the foreground; the clipboard is put back). None when it cannot be read
    safely, for example when the clipboard holds an image."""
    saved = _clip_get()
    if saved["has_other"]:
        return None
    seen = saved["text"]
    try:
        ensure_window()
        _ready()
        tail = _base_tail()
        _orca(["computer", "click", *tail, "--x", str(SEARCH_BOX[0]), "--y", str(SEARCH_BOX[1])])
        _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
        _orca(["computer", "hotkey", *tail, "--key", "ctrl+c"])
        time.sleep(0.4)
        seen = _clip_get().get("text")
        got = (seen or "").strip()
        return got if got and got != (saved["text"] or "").strip() and len(got) <= 60 and "\n" not in got else None
    finally:
        _clip_restore_if_unchanged(saved["text"], seen)


def _set_search_text(text: str) -> None:
    """Put text into the search box (empty = clear it). Tries the background first (the same steps as line_search) and checks the
    result with the × detector; if the box did not change, repeats it in the foreground. Does not take a picture for the caller."""
    ensure_window()
    if _bg_ok():
        try:
            _state(False, background=True)
            tail = _bg_tail()
            _orca(["computer", "bg-click", *tail, "--x", str(SEARCH_BOX[0]), "--y", str(SEARCH_BOX[1])])
            time.sleep(0.3)
            _orca(["computer", "bg-key", *tail, "--key", "end"])
            _orca(["computer", "bg-key", *tail, "--key", "backspace", "--repeat", "80"])
            if text and "\n" not in text:
                _orca(["computer", "bg-chars", *tail, "--text", text])
            time.sleep(1.2)
            if _search_active(_view_png()) == bool(text):
                return
        except Measurement:
            pass                                    # fall through to the foreground
    _ready()
    tail = _base_tail()
    _orca(["computer", "click", *tail, "--x", str(SEARCH_BOX[0]), "--y", str(SEARCH_BOX[1])])
    _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
    if text:
        _orca(["computer", "paste-text", *tail, "--text", text])
    else:
        _orca(["computer", "press-key", *tail, "--key", "Delete"])
    time.sleep(1.2)


def _click_win(x: int, y: int) -> None:
    """One left click in the main window (background when possible)."""
    if _bg_ok():
        _orca(["computer", "bg-click", *_bg_tail(), "--x", str(x), "--y", str(y)])
    else:
        _ready()
        _orca(["computer", "click", *_base_tail(), "--x", str(x), "--y", str(y)])


def _scroll_list(direction: str, pages: int) -> None:
    if _bg_ok():
        _orca(["computer", "bg-scroll", *_bg_tail(), "--x", str(LIST_POINT[0]), "--y", str(LIST_POINT[1]),
               "--direction", direction, "--pages", str(pages)])
    else:
        _ready()
        _orca(["computer", "scroll", *_base_tail(), "--x", str(LIST_POINT[0]), "--y", str(LIST_POINT[1]),
               "--direction", direction, "--pages", str(pages)])


def _capture_origin(allow_fg: bool = True) -> dict:
    """The view before the work: search text, open chat (header picture and name), tab, list position."""
    png = _view_png()
    o: dict = {"at": time.time(), "chat_open": _chat_open(png), "fp": header_fingerprint(png), "tab": _active_tab(png),
               "search_active": _search_active(png), "search": None, "name": None, "list_hash": None, "notes": []}
    if o["chat_open"]:
        o["name"] = _read_header_name(png)
    if o["search_active"] is None:
        o["notes"].append("whether a search was in the box could not be measured (the picture was unreadable), so the "
                          "search text and the list position are unknown")
    if o["search_active"]:
        if allow_fg:
            try:
                o["search"] = _read_search_text()
            except Measurement:
                pass
        if o["search"] is None:
            o["notes"].append("the search text that was in the box could not be read (it may have been empty), so the box stays empty")
    elif o["search_active"] is False:
        o["list_hash"] = _list_hash(png)
    return o


def _remember_origin(allow_fg: bool = True) -> None:
    """Remember the view once (an existing, fresh memory is never overwritten). Failing to remember never stops the real work."""
    if IS_MAC or os.name != "nt":
        return
    old = _ORIGIN.get("view")
    if old and time.time() - old["at"] < ORIGIN_TTL:
        return
    try:
        _ORIGIN["view"] = _capture_origin(allow_fg)
    except Measurement:
        _ORIGIN.pop("view", None)
        return
    if _ORIGIN["view"].get("notes"):
        # e.g. the search text could not be saved in the background: tell the agent in the next result
        _PENDING_WARN.extend(_ORIGIN["view"]["notes"])


def _restore_scan_view(orig_tab: int | None, orig_search: str | None, orig_hash: str | None, match_page: int | None,
                       search_was_active: bool | None = False) -> str:
    """After line_scan_list: put the tab, list position and search text back (the list has just been scrolled to the
    top) and check each one afterwards; what cannot be confirmed is reported as not put back."""
    done: list[str] = []
    failed: list[str] = []
    try:
        if orig_tab not in (None, 0):
            _click_win(*_tab_points()[orig_tab])
            time.sleep(0.8)
            if _active_tab(_view_png()) == orig_tab:
                done.append("the tab above the list")
            else:
                failed.append("the tab above the list (it could not be confirmed after clicking)")
        elif orig_hash and match_page and match_page > 1:
            for _ in range(match_page - 1):
                _scroll_list("down", 1)
                time.sleep(0.8)
            if _list_hash(_view_png()) == orig_hash:
                done.append("the list scroll position")
            else:
                failed.append("the list scroll position (it could not be confirmed after scrolling)")
        elif orig_hash and match_page is None:
            failed.append("the list scroll position (the original view was not among the captured pages; the list stays at the top)")
        if orig_search:
            _set_search_text(orig_search)
            if _read_search_text() == orig_search:           # read back: it must be the original text, not just "some text"
                done.append(f"the search text \"{orig_search}\"")
            else:
                failed.append("the search text (reading the box back did not give the original text)")
    except Measurement as e:
        failed.append(f"stopped midway ({e})")
    if search_was_active is None:
        failed.append("whether a search was in the box could not be measured, so the search text is unknown")
    if search_was_active and not orig_search:
        failed.append("the original search text could not be read, so the search box stays empty")
    note = ""
    if done:
        note += "Put back after the scan: " + ", ".join(done) + ". "
    if failed:
        note += "Could not put back: " + "; ".join(failed) + ". "
    return note


def _reopen_chat(names: list[str]) -> tuple[str, str]:
    """Find a chat by name and open it again (search -> read the result rows with OCR and compare names -> press only the matching row ->
    clear the search). Without OCR nothing is pressed (a guessed row could open, and mark as read, the wrong chat): the search is
    cleared and a problem text is returned so the user or agent opens the chat by hand. Returns (problem, how); problem is empty on success."""
    _set_search_text(_search_query(names[0]))
    _ready()
    lines = _ocr_lines(_view_png(), RESULT_BOX, 2)
    if lines is None:
        # Without OCR the rows cannot be read; pressing a guessed row could open (and mark as read) the wrong chat.
        _set_search_text("")
        return ("OCR is not available, so the original chat was not reopened (a guessed row could open the wrong "
                "chat); ask the user to open it, or reopen it with line_open_chat after checking the name"), ""
    else:
        limit = 10 ** 6
        for ly, text in lines:
            if _norm_name(text).startswith(("メッセ", "messages")):   # below the "Messages" heading come message hits, not chats
                limit = ly
                break
        best, best_score = None, 0.0
        for ly, text in lines:
            if ly >= limit or ly < FIRST_RESULT_POINT[1] - 40:
                continue
            score = max(_name_score(text, n) for n in names)
            if score > best_score:
                best, best_score = ly, score
        if best is None or best_score < 0.75:
            _set_search_text("")
            return "the original chat was not found among the search results, so it was not reopened", ""
        y, how = best + 10, "matched by name in the search results"
    _orca(["computer", "click", *_base_tail(), "--x", str(FIRST_RESULT_POINT[0]), "--y", str(y)])
    time.sleep(1.0)
    _require_front()
    _set_search_text("")
    return "", how


def _restore_supported() -> bool:
    return not (IS_MAC or os.name != "nt")


def _restore_origin(o: dict | None, extra_names: list[str] | None = None) -> str:
    """Put the remembered view back. Returns what was put back and what was not, as one text (never raises)."""
    if not o or not _restore_supported():
        return ""
    done: list[str] = []
    failed: list[str] = list(o.get("notes") or [])
    try:
        png = _view_png()
        cur_open, cur_fp = _chat_open(png), header_fingerprint(png)
        if o.get("chat_open"):
            if cur_open and o.get("fp") and cur_fp == o["fp"]:
                done.append("the chat that was open is still open")
            else:
                names = [n for n in [o.get("name"), *(extra_names or [])] if n]
                if not names:
                    failed.append("the chat that was open could not be reopened (its name is unknown: it could not be read from "
                                  "the screen, and no name was given). Open it with line_open_chat if needed")
                else:
                    problem, how = _reopen_chat(names)
                    if problem:
                        failed.append(problem)
                    else:
                        after = _view_png()
                        same = bool(o.get("fp")) and header_fingerprint(after) == o["fp"]
                        if not same and o.get("name"):
                            same = _name_score(_read_header_name(after) or "", o["name"]) >= 0.75
                        if same:
                            done.append(f"reopened the chat that was open ({how})")
                        else:
                            failed.append(f"reopened a chat ({how}) but could not confirm it is the original one - "
                                          "check the name in the next screenshot")
        elif o.get("chat_open") is False and cur_open:
            _ready()
            _orca(["computer", "click", *_base_tail(), "--x", str(FOCUS_CHAT_POINT[0]), "--y", str(FOCUS_CHAT_POINT[1])])
            _orca(["computer", "press-key", *_base_tail(), "--key", "Escape"])
            time.sleep(0.8)
            if _chat_open(_view_png()) is False:
                done.append("closed the chat that the work opened (none was open before)")
            else:
                failed.append("the chat that the work opened could not be confirmed closed (none was open before)")
        # search text
        png = _view_png()
        if o.get("search"):
            _set_search_text(o["search"])
            if _read_search_text() == o["search"]:
                done.append(f"put the search text \"{o['search']}\" back")
            else:
                failed.append("the search text (it could not be confirmed to be back in the box)")
        elif _search_active(png) is None:
            failed.append("the search box (its state could not be measured, so it is unknown)")
        elif _search_active(png) and o.get("search_active") is False:
            _set_search_text("")
            if _search_active(_view_png()) is False:
                done.append("cleared the search text")
            else:
                failed.append("the search text (the box could not be confirmed empty)")
        # tab and list position (when a search was active the list shows search results, so its position is not compared)
        png = _view_png()
        if o.get("tab") is not None:
            cur_tab = _active_tab(png)
            if cur_tab == o["tab"]:
                pass
            else:
                _click_win(*_tab_points()[o["tab"]])
                time.sleep(0.8)
                if _active_tab(_view_png()) == o["tab"]:
                    done.append("the tab above the list")
                else:
                    failed.append("the tab above the list (it could not be confirmed after clicking)")
        if o.get("list_hash") and not o.get("search_active"):
            now = _list_hash(_view_png())
            if now is None:
                failed.append("the list scroll position (the list could not be measured, so it is unknown)")
            elif now != o["list_hash"]:
                _scroll_list("up", 10)
                time.sleep(0.8)
                found = False
                for _ in range(8):
                    if _list_hash(_view_png()) == o["list_hash"]:
                        found = True
                        break
                    _scroll_list("down", 1)
                    time.sleep(0.8)
                if found:
                    done.append("the list scroll position")
                else:
                    _scroll_list("up", 10)          # the search above left the list scrolled down: bring it to the top
                    time.sleep(0.8)
                    failed.append("the list scroll position (the original position was not found again; the list was "
                                  "scrolled back to the top, which is where it is now)")
    except Measurement as e:
        failed.append(f"stopped midway ({e})")
    except Exception as e:  # noqa: BLE001 - restoring is best effort; the caller's real result must still be returned
        failed.append(f"stopped midway ({type(e).__name__})")
    parts = []
    if done:
        parts.append("View put back: " + ", ".join(done) + ".")
    if failed:
        parts.append("Could not put back: " + "; ".join(failed) + ".")
    return " ".join(parts)


def _restore_and_keep(o: dict | None, extra_names: list[str] | None = None) -> str:
    """Restore the remembered view. The memory is dropped only when everything was put back; after a failure it
    is kept (until its time limit) so line_restore_view can be called again."""
    note = _restore_origin(o, extra_names)
    if o is not None and "Could not put back" in note:
        _ORIGIN["view"] = o
        note += " The remembered view was kept: call line_restore_view again after fixing the cause."
    else:
        _ORIGIN.pop("view", None)
    return note or "Nothing needed putting back."


def tool_restore_view(_args: dict) -> list[dict]:
    """Put LINE back the way it was before the work (search text, open chat, tab, list position)."""
    ensure_window()
    o = _ORIGIN.get("view")
    if not o or time.time() - o["at"] >= ORIGIN_TTL:
        _ORIGIN.pop("view", None)
        return [{"type": "text", "text": "Nothing to put back: no view was remembered (it is remembered at the first call of "
                                         "line_open_chat, line_search, line_click, line_scroll, line_key, line_type or line_drag, "
                                         "and is dropped after 30 minutes or once restored). Nothing was done."}]
    note = _restore_and_keep(o)
    return [{"type": "text", "text": note}, *_shot("Screen after putting the view back.", background=_bg_ok())[1:]]


# ---- Open a chat by name, read a conversation as text (added 2026-10-27) ----------------
# Background: "open a chat by name" and "read the conversation text" were added with safeguards
# (a record of the verified chat name, a foreground check, an immediate stop on user activity, never reading the Keep memo).
FIRST_RESULT_POINT = (230, 165)         # first row of the chats section of the search results (window 767x722, measured 2026-10-27)
# After Ctrl+A, right-click a selected message and press "コピー" (Copy) (Ctrl+C does not work; measured 2026-10-27).
# Candidates are tried in turn. A menu appears only over a message, so move on to the next point otherwise.
COPY_RIGHT_CLICK_POINTS = [(500, 320), (560, 260), (520, 420), (600, 500), (480, 560)]
COPY_MENU_FIRST_ROW = (42, 28)          # first row of the popup = "コピー" (Copy) (after select-all there are 3 rows: Copy / Delete / Forward to Keep)
COPY_MENU_MAX_SIZE = (200, 160)         # a menu larger than this is a different menu (a right click on a single message gives 110x270) = do not press
FOCUS_CHAT_POINT = (372, 420)           # margin at the left of the conversation. It is not over a bubble or link, so pressing it opens nothing
TEXT_CLIPBOARD_NAMES = {"UnicodeText", "Text", "OEMText", "Locale"}   # anything else counts as non-text
IMAGE_CLIPBOARD_FORMATS = {"Bitmap", "System.Drawing.Bitmap", "DeviceIndependentBitmap", "PNG", "FileDrop", "FileNameW",
                           "FileName", "Shell IDList Array", "FileGroupDescriptorW", "FileContents", "EnhancedMetafile",
                           "MetaFilePict"}

# Component that masks strings shaped like keys or tokens. **Not used by default** (characters needed for the user's work must not be silently altered).
# Used only when the caller asks for redact_secrets=true.
_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"),
]
_SECRET_QUERY = re.compile(r"(?i)([?&](?:token|key|secret|password|pwd|passwd|api_key|apikey|access_token)=)[^&\s]+")


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace strings shaped like keys or tokens with "●●●". Also returns the number of replacements."""
    count = 0

    def sub(_match) -> str:
        nonlocal count
        count += 1
        return "●●●"

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(sub, text)

    def sub_query(match) -> str:
        nonlocal count
        count += 1
        return match.group(1) + "●●●"

    text = _SECRET_QUERY.sub(sub_query, text)
    return text, count


def _ps(script: str, stdin: str = "", timeout: int = 20) -> str:
    """Call PowerShell (no window, STA) for clipboard reads and writes. Input is passed on stdin (so long text is not cut off)."""
    if os.name != "nt":
        raise Measurement("clipboard read/write is Windows-only")
    exe = shutil.which("powershell") or "powershell"
    full = ("[Console]::InputEncoding=[Text.Encoding]::UTF8;[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Add-Type -AssemblyName System.Windows.Forms;" + script)
    try:
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", full],
                              input=stdin.encode("utf-8"), capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise Measurement("clipboard read/write did not finish in time") from exc
    if proc.returncode == 3:
        raise Measurement("the clipboard holds something other than plain text (image, files, rich text). Nothing was written.")
    if proc.returncode != 0:
        raise Measurement("could not read or write the clipboard")
    return proc.stdout.decode("utf-8", errors="replace")


def _clip_get() -> dict:
    """The current clipboard: its text (None if there is none), and whether it holds something other than text, such as images or files."""
    if IS_MAC:
        other = _mac_clip_has_other()
        return {"text": None if other else _mac_clip_text(), "has_other": other, "formats": []}
    out = _ps("$d=[System.Windows.Forms.Clipboard]::GetDataObject();$f=@();if($d){$f=$d.GetFormats($false)};"
              "$t='';$has=[System.Windows.Forms.Clipboard]::ContainsText();"
              "if($has){$t=[System.Windows.Forms.Clipboard]::GetText()};"
              "$b=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t));"
              "($f -join ',')+'|'+([int]$has)+'|'+$b").strip()
    formats, has_text, b64 = (out.split("|", 2) + ["", "", ""])[:3]
    names = set(filter(None, formats.split(",")))
    text = base64.b64decode(b64).decode("utf-8", errors="replace") if has_text == "1" else None
    return {"text": text, "has_other": bool(names - TEXT_CLIPBOARD_NAMES), "formats": sorted(names)}


PS_CLIP_GUARD = ("$d=[System.Windows.Forms.Clipboard]::GetDataObject();$f=@();if($d){$f=$d.GetFormats($false)};"
                 "$bad=@($f|Where-Object{@('UnicodeText','Text','OEMText','Locale') -notcontains $_});"
                 "if($bad.Count -gt 0){exit 3};")


def _clip_set(text: str | None) -> None:
    """Put text into the clipboard (clears it if None or empty). The format check and the write are one
    PowerShell step, which narrows the gap but does not remove it: another program can still change the clipboard
    between that check and the write, or between an unchanged-check and a restore. That residual race is accepted."""
    if IS_MAC:
        _mac_clip_set(text)
        return
    if text:
        _ps(PS_CLIP_GUARD + "$t=[Console]::In.ReadToEnd();[System.Windows.Forms.Clipboard]::SetText($t)", stdin=text)
    else:
        _ps(PS_CLIP_GUARD + "[System.Windows.Forms.Clipboard]::Clear()")


def _clip_restore_if_unchanged(saved_text: str | None, expected_text: str | None) -> bool:
    """Put the user's clipboard text back only if the clipboard still holds exactly what we left there
    (expected_text); if the user copied something since, leave it alone. An originally empty clipboard
    (saved_text None) is cleared again. Returns whether it restored."""
    try:
        current = _clip_get()
        if current["has_other"] or (current["text"] or "") != (expected_text or ""):
            return False
        _clip_set(saved_text)
        return True
    except Measurement:
        return False


def _popup_size() -> tuple[int, int] | None:
    """Size of the menu (a separate window) that is currently open. None if there is none."""
    wins = _windows()
    if IS_MAC:
        small = [w for w in wins if _mac_kind(w) in ("dialog", "uncertain")]      # a chat window is never a popup
    else:
        small = [w for w in wins if not (int(w["width"]) >= 500 and int(w["height"]) >= 500)]
    if not small:
        return None
    last = small[-1]
    return int(last["width"]), int(last["height"])


def _confirm_popup_size() -> tuple[int, int] | None:
    """The size of an open dialog that a confirm key (Return/Space) may be sent to. On macOS only a window that is
    clearly a dialog counts, and any window of uncertain kind makes the answer None (refuse when unsure)."""
    if not IS_MAC:
        return _popup_size()
    wins = _windows()
    if any(_mac_kind(w) == "uncertain" for w in wins):
        return None
    dialogs = [w for w in wins if _mac_kind(w) == "dialog"]
    if not dialogs:
        return None
    return int(dialogs[-1]["width"]), int(dialogs[-1]["height"])


def _log_dir() -> Path:
    base = os.environ.get("LINE_MCP_LOG_DIR")
    if base:
        return Path(base)
    return state_dir() / "chat-logs"


def unsafe_log_dir_reason(path: Path) -> str | None:
    """If the logs must not be stored in a location, return the reason. **Only inside a Git repository** (because commit and push would publish them).

    Putting them in Drive, Dropbox, OneDrive or a chat is the user's own choice and is not refused.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return "could not determine the location"
    for parent in [resolved, *resolved.parents]:
        if (parent / ".git").exists():
            return "inside a Git repository (commit and push would publish it)"
    return None


def save_chat_log(chat: str, text: str, now: float | None = None, redacted: bool = False) -> Path:
    """Save the text to the log folder as a text file. Returns the saved path (the text is not pasted into the conversation)."""
    folder = _log_dir()
    reason = unsafe_log_dir_reason(folder)
    if reason:
        raise Measurement(f"the log location is not safe ({reason}). Point LINE_MCP_LOG_DIR outside any Git repository: {folder}")
    if _is_link(folder):
        raise Measurement(f"the log folder {folder} is a symbolic link or reparse point; refusing to write there")
    _private_dir(folder, "chat log folder")
    moment = dt.datetime.fromtimestamp(now if now is not None else time.time())
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", chat).strip()[:40] or "chat"
    path = folder / f"{stamp}_{safe}.txt"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}_{safe}_{n}.txt"
    header = (f"# LINE conversation log (captured {moment.strftime('%Y-%m-%d %H:%M:%S')}, "
              f"chat '{chat}'" + (", key-like strings masked" if redacted else "") + ")\n"
              "# 🔴 Do not put the contents of this folder into Git. Delete them when no longer needed.\n\n")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # exclusive create (never follows a link), owner only
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(header + text)
    except OSError as exc:
        raise Measurement(f"could not write the chat log {path} ({exc})") from exc
    return path


def tool_open_chat(args: dict) -> list[dict]:
    """Find and open a chat by name (type into the search box -> press the first search result -> clear the search text).

    The tool cannot verify the chat name (it returns images). Always read the chat name at the top right of the returned image to confirm it.
    If a different chat is open, redo it before sending or reading. For a name whose search results have no chats section (only messages match),
    the first row shifts and a different screen may open.
    """
    name = str(args.get("name", "")).strip()
    if not name or len(name) > 60 or "\n" in name or "\r" in name:
        raise Measurement("name (the chat to look for) must be one line of 1 to 60 characters")
    ensure_window()
    which = "list" if IS_MAC else "main"
    _ready(which)
    _remember_origin()                        # remember the view before the work (search text, open chat); put back by line_read_chat_text or line_restore_view
    tail = _base_tail(which)
    _orca(["computer", "click", *tail, "--x", str(SEARCH_BOX[0]), "--y", str(SEARCH_BOX[1])])
    _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
    _orca(["computer", "paste-text", *tail, "--text", name])
    time.sleep(1.5)
    _require_front(which)
    _orca(["computer", "click", *tail, "--x", str(FIRST_RESULT_POINT[0]), "--y", str(FIRST_RESULT_POINT[1]),
           "--click-count", "2" if IS_MAC else "1"])
    time.sleep(1.0)
    if IS_MAC:
        return _shot(f"Screen after searching for '{name}' and opening the chat in the first result row in a separate window. "
                     "**Always check on the image that the window's chat name is the person you looked for.** On Mac the search text stays on the list side, so "
                     "go back to the list afterwards and clear it. If it is wrong, stop before sending or reading.")
    # clear the search text (otherwise it stays on the user's screen). The chat stays open
    _require_front()
    _orca(["computer", "click", *tail, "--x", str(SEARCH_BOX[0]), "--y", str(SEARCH_BOX[1])])
    _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
    _orca(["computer", "press-key", *tail, "--key", "Delete"])
    time.sleep(0.8)
    kept = _ORIGIN.get("view")
    memo = ""
    if kept:
        was = "a search was active, " if kept.get("search_active") else ""
        was += "a chat was open" if kept.get("chat_open") else "no chat was open"
        memo = (f" The view before the work ({was}) was remembered; line_read_chat_text puts it back after reading "
                "(otherwise call line_restore_view).")
    return _shot(f"Screen after searching for '{name}' and opening the chat in the first result row (search text cleared)." + memo +
                 " **Always check on the image that the chat name at the top right is the person you looked for.** If it is wrong, redo it before sending or reading.")


def tool_read_chat_text(args: dict) -> list[dict]:
    """Get the text of the currently open conversation as text (the loaded range).

    Steps: press the margin of the conversation -> Ctrl+A (select all) -> right-click a selected message -> "コピー" (Copy) -> read the clipboard ->
    restore the clipboard -> Escape (clears the selection; in LINE this closes the chat).
    The text is saved to a file only when save_log is set; otherwise it is just returned to the caller. Strings shaped like keys or tokens are replaced with "●●●" only when redact_secrets=true.
    """
    expected = str(args.get("expected_chat", "")).strip()
    if not expected:
        raise Measurement("expected_chat (the chat name verified on screen) is required. Before reading, read the name at the top right with line_screenshot")
    require_same_chat()
    if re.search(r"keep|キープ", expected, re.I):
        raise Measurement("the Keep memo (the user's notes to self) is not read, because keys are often pasted there")
    if args.get("save_log"):                       # check that the log location is safe before starting any action
        reason = unsafe_log_dir_reason(_log_dir())
        if reason:
            raise Measurement(f"the log location is not safe ({reason}). Point LINE_MCP_LOG_DIR outside any Git repository: {_log_dir()}")
    try:
        return _read_chat_text_work(args, expected)
    except Exception as exc:  # noqa: BLE001 - from here on, any failure (preparation, copy, log saving) puts the view back
        note = ""
        if args.get("restore_view", True) and _ORIGIN.get("view"):
            note = _restore_and_keep(_ORIGIN.get("view"), [expected])
        raise Measurement(f"{exc} {note}".strip()) from exc


def _read_chat_text_work(args: dict, expected: str) -> list[dict]:
    older = max(0, min(int(args.get("load_older_pages", 0) or 0), 30))
    max_chars = max(500, min(int(args.get("max_chars", 12000) or 12000), 60000))
    ensure_window()
    _remember_origin()                         # no-op if line_open_chat already remembered it; otherwise the view as it is now
    width, height = _geometry(_ready())
    tail = _base_tail()
    saved = _clip_get()
    if saved["has_other"]:
        raise Measurement("the clipboard holds something other than plain text (image, files, rich text). Aborted because "
                          "overwriting it cannot be undone. Ask the user to copy plain text or clear the clipboard.")
    marker = "__LINE_MCP_COPY_" + uuid.uuid4().hex + "__"
    last_seen = marker
    got: str | None = None
    problem = ""
    try:
        _clip_set(marker)
        for _ in range(older):                      # load older messages (scrolling up makes LINE load more)
            _require_front()
            _orca(["computer", "scroll", *tail, "--x", str(CHAT_POINT[0]), "--y", str(CHAT_POINT[1]),
                   "--direction", "up", "--pages", "3"])
            time.sleep(0.8)
        _require_front()
        _orca(["computer", "click", *tail, "--x", str(FOCUS_CHAT_POINT[0]), "--y", str(FOCUS_CHAT_POINT[1])])
        _require_front()
        _orca(["computer", "hotkey", *tail, "--key", "ctrl+a"])
        time.sleep(0.5)
        for (x, y) in COPY_RIGHT_CLICK_POINTS:
            if not (0 <= x < width and 0 <= y < height):
                continue
            _require_front()
            _orca(["computer", "click", *tail, "--x", str(x), "--y", str(y), "--mouse-button", "right"])
            time.sleep(0.7)
            size = _popup_size()
            if size is None:
                continue                              # not over a message = no menu appears
            if size[0] > COPY_MENU_MAX_SIZE[0] or size[1] > COPY_MENU_MAX_SIZE[1]:
                problem = f"an unexpected menu ({size[0]}x{size[1]}) appeared, so it was closed without pressing"
                _orca(["computer", "press-key", *_base_tail("popup"), "--key", "Escape"])   # close the menu without pressing
                time.sleep(0.5)
                continue
            _ready("popup")
            _orca(["computer", "click", *_base_tail("popup"), "--x", str(COPY_MENU_FIRST_ROW[0]),
                   "--y", str(COPY_MENU_FIRST_ROW[1])])
            time.sleep(0.8)
            current = _clip_get()["text"]
            if current:
                last_seen = current
            if current and current != marker:
                got = current
                break
        _ready()
        _orca(["computer", "press-key", *tail, "--key", "Escape"])   # clear the selection (this closes the chat)
    finally:
        _clip_restore_if_unchanged(saved["text"], last_seen)     # the user's clipboard, unless they changed it meanwhile
    if not got:
        raise Measurement("could not copy the text. " + (problem or "no right-click menu appeared over a message (the conversation is empty, or the window layout is not the standard one)"))
    cleaned, hidden = redact_secrets(got) if args.get("redact_secrets") else (got, 0)   # not masked by default
    truncated = len(cleaned) > max_chars
    body = cleaned[-max_chars:] if truncated else cleaned
    saved_note = "Not saved."
    if args.get("save_log"):
        saved_path = save_chat_log(expected, cleaned, redacted=bool(hidden or args.get("redact_secrets")))   # everything, before truncating what is returned
        saved_note = f"Log saved (the text is not pasted into the conversation): {saved_path}"
    if args.get("restore_view", True):
        restored = _restore_and_keep(_ORIGIN.get("view"), [expected])
    else:
        restored = "(restore_view=false: the view was not put back. Call line_restore_view when you are done.)"
    head = (f"Text of the chat named '{expected}' (as text, loaded range, {len(cleaned)} characters"
            + (f", the last {max_chars} shown" if truncated else "") + "). " + saved_note
            + (f" {hidden} key/token-like string(s) masked with ●●●." if hidden else "")
            + " Selection cleared and chat closed after reading. Clipboard restored." + (" " + restored if restored else ""))
    return [{"type": "text", "text": head}, {"type": "text", "text": body}]


_PT = {"type": "string", "enum": list(NAMED_POINTS),
       "description": "Use a named point from the layout instead of x/y (see line_layout)."}
_ACTION = {"type": "string", "enum": ["menu", *sorted(MANAGE_ACTIONS)],
           "description": ("What this click/key does. 'menu' = an ordinary menu item. A management action "
                           "(leave, invite, remove_member, create_group, add_friend, rename, block, delete_chat, "
                           "unsend) is for the button that CONFIRMS it, and needs expected_target.")}
_TARGET = {"type": "string",
           "description": "The chat / group / person name you just read on the screen for this action."}

TOOLS = [
    {"name": "line_status",
     "description": "Check whether the LINE desktop window is open (does not open it). Also reports the input watcher.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "line_open",
     "description": ("Show the LINE window (LINE usually sits in the tray), bring it to the front and return a "
                     "screenshot. Call this first."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "line_screenshot",
     "description": ("Return a fresh screenshot of LINE. On Windows the main window is captured in the background "
                     "(not brought to the front)."),
     "inputSchema": {"type": "object",
                     "properties": {"window": {"type": "string", "enum": ["main", "popup"],
                                               "description": "popup = a menu or dialog window opened by a click"}}}},
    {"name": "line_scan_list",
     "description": ("Scroll the chat list from the top and return one screenshot per page (unread counts are the "
                     "numbers on the right of each row). The entry point for checking unread messages. Opens no chat. "
                     "A leftover search is cleared for the scan; the search text, tab and list position are put back afterwards."),
     "inputSchema": {"type": "object",
                     "properties": {"max_pages": {"type": "integer", "minimum": 1, "maximum": 10}}}},
    {"name": "line_click",
     "description": ("Click inside the LINE window (window coordinates, top-left origin; reference size 767x722) and "
                     "return a screenshot. A single left click on the main window runs in the background on Windows. "
                     "Clicks inside a menu/dialog (window=popup) must declare action; action=menu works only for a menu this server just opened (right click, chat menu button, opens_menu=true) and only while the popup is menu-sized. Clicks in the input row / send area at the bottom of the chat pane are refused (only line_send sends). The server cannot read button labels. A click that confirms a "
                     "management action (leave a group, invite, remove a member, create a group, add a friend, ...) "
                     "must pass action and expected_target; it counts toward the manage limits and is logged."),
     "inputSchema": {"type": "object",
                     "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "point": _PT,
                                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                                    "window": {"type": "string", "enum": ["main", "popup"],
                                               "description": "popup = click inside a menu/dialog (its own top-left origin)"},
                                    "action": _ACTION, "expected_target": _TARGET,
                                    "opens_menu": {"type": "boolean",
                                                   "description": "This click on the main window opens a menu (a right click and the chat menu button are detected on their own). Lets the next clicks in the menu use action=menu."}}}},
    {"name": "line_drag",
     "description": "Drag inside the LINE window from one point to another and return a screenshot.",
     "inputSchema": {"type": "object",
                     "properties": {"x1": {"type": "integer"}, "y1": {"type": "integer"},
                                    "x2": {"type": "integer"}, "y2": {"type": "integer"}},
                     "required": ["x1", "y1", "x2", "y2"]}},
    {"name": "line_key",
     "description": ("Press one key (Escape, Tab, arrows, Backspace, ...) or a combination (ctrl+a). Plain "
                     "keys run in the background on Windows; combinations bring LINE to the front. Return/Enter "
                     "(also alt/ctrl/shift+Return and the send key) is REFUSED here, because it can send a message: "
                     "use line_send. The only exception is a plain Return in a dialog: window=popup together with a "
                     "management action and expected_target."),
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}, "action": _ACTION,
                                                      "expected_target": _TARGET,
                                                      "window": {"type": "string", "enum": ["main", "popup"],
                                                                 "description": "popup = the key goes to the open dialog"}},
                     "required": ["key"]}},
    {"name": "line_type",
     "description": ("Type text into the field that has focus inside LINE. Does not press Enter (nothing is sent). "
                     "Single-line text runs in the background on Windows; multi-line text is pasted in the front."),
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "line_scroll",
     "description": "Scroll the chat list (default) or the open conversation and return a screenshot.",
     "inputSchema": {"type": "object",
                     "properties": {"direction": {"type": "string", "enum": ["up", "down"]},
                                    "pages": {"type": "integer", "minimum": 1, "maximum": 5},
                                    "target": {"type": "string", "enum": ["list", "chat"]}}}},
    {"name": "line_search",
     "description": ("Put text into LINE's search box (empty text clears it) and return a screenshot. Never touches "
                     "the message box. Runs in the background on Windows."),
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string", "maxLength": 60}},
                     "required": ["text"]}},
    {"name": "line_send",
     "description": ("Send a message to the conversation that is open now. BEFORE calling, take line_screenshot, "
                     "read the chat name at the top right and pass it as expected_chat. Afterwards check the "
                     "screenshot for the new bubble. dry_run=true only fills the message box. Write @[display name] "
                     "for a mention; with mentions, do a dry_run first and then send with mentions_checked=true. "
                     "Sends are limited per minute/day and logged locally (no message text by default)."),
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"},
                                    "expected_chat": {"type": "string", "description": "The chat name you read on the screen"},
                                    "signature": {"type": "string", "description": "Appended after a blank line"},
                                    "send_key": {"type": "string",
                                                 "description": "Default alt+Return (Windows) / Return (Mac); or SEND_KEY in the layout"},
                                    "composer_x": {"type": "integer", "description": "Where to click the message box (x)"},
                                    "composer_y": {"type": "integer", "description": "Where to click the message box (y)"},
                                    "dry_run": {"type": "boolean"},
                                    "replace_draft": {"type": "boolean",
                                                      "description": "Discard an unsent draft in the message box instead of setting it aside and putting it back after sending (default false)"},
                                    "mentions_checked": {"type": "boolean",
                                                         "description": "You checked the blue @name in a dry_run screenshot"}},
                     "required": ["text", "expected_chat"]}},
    {"name": "line_open_chat",
     "description": ("Find a chat by name and open it (search box -> first result -> clear the search). Always check "
                     "the chat name at the top right of the returned screenshot before sending or reading."),
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "maxLength": 60}},
                     "required": ["name"]}},
    {"name": "line_restore_view",
     "description": ("Put LINE back the way it was before the work: search text, the chat that was open, the tab above "
                     "the list and the list scroll position (Windows only). The view is remembered automatically at the "
                     "first call of line_open_chat, line_search, line_click, line_scroll, line_key, line_type or "
                     "line_drag. line_read_chat_text and line_scan_list put it back by themselves; call this at the end "
                     "of anything else (only looking around, a send, several chats read with restore_view=false). "
                     "The result says what could not be put back."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "line_read_chat_text",
     "description": ("Return the loaded part of the open conversation as text (select all -> Copy -> read the "
                     "clipboard, then restore it). Pass expected_chat read from the screen. Refuses Keep memos. "
                     "Not saved anywhere unless save_log=true."),
     "inputSchema": {"type": "object",
                     "properties": {"expected_chat": {"type": "string", "description": "The chat name you read on the screen"},
                                    "load_older_pages": {"type": "integer", "minimum": 0, "maximum": 30},
                                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 60000},

                                    "restore_view": {"type": "boolean",
                                                     "description": "After reading, put LINE back the way it was before the work (search text, open chat, tab, list position). Default true. Set false only when reading several chats in a row, then call line_restore_view at the end"},
                                    "redact_secrets": {"type": "boolean",
                                                       "description": "Mask strings that look like keys or tokens"},
                                    "save_log": {"type": "boolean",
                                                 "description": "Save the text under the local state folder (never inside a Git repository)"}},
                     "required": ["expected_chat"]}},
    {"name": "line_layout",
     "description": ("Show (op=get) or change (op=set, name, value or x/y) the coordinates and keys this server uses. "
                     "Values are saved in the user's settings file. Use this when LINE's update moved buttons "
                     "(see the line-desktop-recalibrate skill)."),
     "inputSchema": {"type": "object",
                     "properties": {"op": {"type": "string", "enum": ["get", "set"]},
                                    "name": {"type": "string"},
                                    "value": {"description": "[x, y], a number, or a key name for SEND_KEY"},
                                    "x": {"type": "integer"}, "y": {"type": "integer"}}}},
]

HANDLERS = {
    "line_status": tool_status, "line_open": tool_open, "line_screenshot": tool_screenshot,
    "line_scan_list": tool_scan_list, "line_click": tool_click, "line_drag": tool_drag,
    "line_key": tool_key, "line_type": tool_type, "line_scroll": tool_scroll,
    "line_search": tool_search, "line_send": tool_send,
    "line_open_chat": tool_open_chat, "line_read_chat_text": tool_read_chat_text,
    "line_restore_view": tool_restore_view,
    "line_layout": tool_layout,
}



# ---- JSON-RPC ----------------------------------------------------------------

def _reply(msg_id, result=None, error=None) -> dict:
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def respond(msg_id, result=None, error=None) -> None:
    sys.stdout.write(json.dumps(_reply(msg_id, result, error), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def process(req: dict) -> dict | None:
    """Handle one JSON-RPC message and return the dict to send back (None when no reply is needed, e.g. a notification). Shared by stdio and HTTP."""
    method = req.get("method", "")
    msg_id = req.get("id")
    if method == "initialize":
        return _reply(msg_id, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return _reply(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = req.get("params") or {}
        fn = HANDLERS.get(params.get("name", ""))
        if fn is None:
            return _reply(msg_id, error={"code": -32601, "message": f"unknown tool: {params.get('name')}"})
        arguments = params.get("arguments") or {}
        try:
            # The UI lock covers the whole call: remembering the user's focus, the tool, and giving the focus back.
            with (_ui_lock() if _needs_ui_lock(params.get("name", ""), arguments) else contextlib.nullcontext()):
                _CALL["start"], _CALL["done"] = time.monotonic(), []
                remember_focus()
                content = fn(arguments)
                try:
                    if params.get("name") in GIVE_BACK_TOOLS:
                        return_focus()
                except Exception:  # noqa: BLE001 - giving focus back is best effort
                    pass
            return _reply(msg_id, {"content": content, "isError": False})
        except Measurement as exc:
            return _reply(msg_id, {"content": [{"type": "text", "text": (
                f"⛔ MEASUREMENT_FAILED: {exc} **Do not treat this as 'nothing in LINE'.**")}], "isError": True})
        except Exception as exc:  # noqa: BLE001
            return _reply(msg_id, {"content": [{"type": "text", "text": (
                f"⛔ MEASUREMENT_FAILED: {type(exc).__name__}. **Do not treat this as 'nothing in LINE'.**")}],
                "isError": True})
    if msg_id is not None:
        return _reply(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})
    return None


def handle(req: dict) -> None:
    out = process(req)
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ---- HTTP mode (optional, off by default; added 2026-10-27) -------------------------------------
# Unlike some other LINE desktop MCP servers, whose HTTP mode listened on all interfaces without authentication, this server's HTTP mode **listens only on loopback (127.0.0.1, localhost, ::1)**
# and requires a token. No endpoint is offered that lets another PC or Mac drive the owner's LINE.
# Host is checked, and any request with an Origin header is refused (browsers add Origin; MCP clients do not).
# Request bodies and headers are never logged. Because this is an entry point that can drive LINE, start it only when needed (do not leave it running).
HTTP_MAX_BODY = 1_000_000
HTTP_LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}
HTTP_MIN_TOKEN = 24
HTTP_LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
HTTP_SOCKET_TIMEOUT = 15        # seconds a connection may stay silent (slow or half-open clients are dropped)
HTTP_MAX_CONNECTIONS = 8        # connections served at the same time; more get 503
_HTTP_LOCK = threading.Lock()   # one screen action at a time (running two at once would mix up coordinates)


def http_token_problem(token: str | None) -> str | None:
    if not token or len(token) < HTTP_MIN_TOKEN:
        return f"the environment variable LINE_MCP_TOKEN must hold a token of at least {HTTP_MIN_TOKEN} characters (do not put it on the command line: it would show up in the process list)"
    if len(set(token)) < 8:
        return "LINE_MCP_TOKEN is too simple (use a hard-to-guess string with at least 8 distinct characters)"
    return None


def _name_part(value: str) -> str:
    """Extract only the name, without the port, from a Host / Origin value ([::1]:3000 -> [::1])."""
    value = value.strip().lower().split("://", 1)[-1].split("/", 1)[0]
    if value.startswith("["):
        return value[:value.find("]") + 1] if "]" in value else value
    return value.rsplit(":", 1)[0] if ":" in value else value


def make_http_server(host: str, port: int, token: str):
    """Build the HTTP-mode server (it listens only once serve_forever is called). Not built when the token is missing or weak."""
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    host = host.strip()
    if not host:
        raise Measurement("host is empty")
    problem = http_token_problem(token)
    if problem:
        raise Measurement(problem)
    if host.lower() not in HTTP_LOOPBACK:
        raise Measurement("HTTP mode only listens on loopback (127.0.0.1, localhost, ::1)"
                          "(no endpoint is offered that lets another PC or Mac drive LINE)")
    bind_host = {"localhost": "127.0.0.1", "[::1]": "::1"}.get(host.lower(), host.strip("[]"))
    loopback = True

    slots = threading.BoundedSemaphore(max(1, int(HTTP_MAX_CONNECTIONS)))

    class Handler(BaseHTTPRequestHandler):
        timeout = HTTP_SOCKET_TIMEOUT
        server_version = "line-desktop-mcp"
        sys_version = ""

        def log_message(self, *_args):               # log neither headers nor body (the token would be recorded)
            return

        def _drain(self) -> None:
            """Read (a bounded part of) an unread request body before an early refusal, so closing the connection
            does not reset it under the client's feet."""
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 65536)
                if length > 0:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass

        def _send(self, code: int, body: bytes = b"") -> None:
            if code >= 400:
                self._drain()
                self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            self._send(405, b'{"error":"POST /mcp only"}')

        def do_POST(self):
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, b'{"error":"not found"}')
            # Refuse ANY request that carries an Origin header, even from localhost: browsers add Origin to
            # cross-site and fetch requests, while MCP clients and command-line tools do not. When listening on
            # loopback, Host is checked as well.
            if self.headers.get("Origin") is not None:
                return self._send(403, b'{"error":"forbidden"}')
            if loopback and _name_part(self.headers.get("Host") or "") not in HTTP_LOCAL_NAMES:
                return self._send(403, b'{"error":"forbidden"}')
            sent = self.headers.get("Authorization") or ""
            if not hmac.compare_digest(sent.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
                return self._send(401, b'{"error":"unauthorized"}')
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, b'{"error":"bad length"}')
            if length > HTTP_MAX_BODY:
                return self._send(413, b'{"error":"too large"}')
            if length <= 0:
                return self._send(400, b'{"error":"empty"}')
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send(400, b'{"error":"bad json"}')
            batch = isinstance(payload, list)
            if not (batch or isinstance(payload, dict)):
                return self._send(400, b'{"error":"bad request"}')
            outs = []
            with _HTTP_LOCK:
                for item in (payload if batch else [payload]):
                    if isinstance(item, dict):
                        try:
                            out = process(item)
                        except Exception:  # noqa: BLE001  do not drop the connection on unexpected input (wrong types, etc.)
                            out = _reply(item.get("id"), error={"code": -32600, "message": "invalid request"})
                        if out is not None:
                            outs.append(out)
            if not outs:
                return self._send(202)
            self._send(200, json.dumps(outs if batch else outs[0], ensure_ascii=False).encode("utf-8"))

    class Server(ThreadingHTTPServer):
        allow_reuse_address = False               # do not coexist with another program that already holds the same port
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET

        def process_request(self, request, client_address):
            if not slots.acquire(blocking=False):          # too many connections at once
                try:
                    request.settimeout(1)
                    request.sendall(b"HTTP/1.1 503 Service Unavailable" + bytes([13, 10]) + b"Content-Length: 0"
                                    + bytes([13, 10, 13, 10]))
                except OSError:
                    pass
                self.shutdown_request(request)
                return
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                slots.release()

    return Server((bind_host, port), Handler)


def main_http(argv: list[str]) -> int:
    port = 3000
    host = "127.0.0.1"
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
    try:
        server = make_http_server(host, port, os.environ.get("LINE_MCP_TOKEN", ""))
    except Measurement as exc:
        sys.stderr.write(f"could not start HTTP mode: {exc}\n")
        return 2
    sys.stderr.write(f"line-desktop MCP (HTTP): http://{server.server_address[0]}:{server.server_address[1]}/mcp  (token required)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _unsupported_platform() -> str | None:
    if sys.platform in ("win32", "darwin"):
        return None
    return ("This server is for the LINE desktop app on Windows and Mac (Linux is not supported). "
            "For Mac setup, see the project documentation")


def main() -> int:
    problem = _unsupported_platform()
    if problem:
        sys.stderr.write(problem + "\n")
        return 3
    global SEND_KEY
    if IS_MAC:
        SEND_KEY = os.environ.get("LINE_SEND_KEY") or "Return"     # default for LINE on Mac (a guess; measure it in testing)
        _MAC_LAYOUT_LOADED[:] = load_mac_layout()
        if "--selftest" in sys.argv[1:]:
            return mac_selftest()
    try:
        apply_layout()
    except Measurement as exc:
        sys.stderr.write(f"{exc}\n")
    _real_input()          # start the human-input watcher first (so the user's input is counted before the first action)
    if "--http" in sys.argv[1:]:
        return main_http(sys.argv[1:])
    # force UTF-8 instead of relying on startup environment variables (the Windows default cp932 fails on ⛔ and Japanese text)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(req)
    return 0


def mac_selftest() -> int:
    """Check permissions and the watcher on Mac (does not press or type anything in LINE).python3 line_desktop_mcp.py --selftest"""
    print("1) helper (JXA):", "OK" if MAC_HELPER.is_file() else "✗ line_desktop_mac.js is missing")
    try:
        wins = _mac_windows()
        print(f"2) LINE windows: {len(wins)}", [(w['width'], w['height'], w.get('title', '')) for w in wins][:5])
        if wins and not any(w.get("title") for w in wins):
            print("   (empty window titles = the Screen Recording permission may be missing)")
    except Measurement as exc:
        print("2) ✗", exc)
    try:
        print("3) frontmost app:", _mac_js("front"))
    except Measurement as exc:
        print("3) ✗", exc)
    before = _mac_idle_seconds()
    print(f"4) last real-hardware input: {before} s ago (easier to read if you keep your hands off for about 2 seconds first)")
    try:
        here = subprocess.run(["osascript", "-l", "JavaScript", "-e",
                               "ObjC.import('CoreGraphics');var p=$.CGEventGetLocation($.CGEventCreate(null));"
                               "Math.round(p.x)+','+Math.round(p.y)"], capture_output=True, timeout=10)
        x, y = here.stdout.decode().strip().split(",")
        _mac_js("move", x, y)              # only "moves" to the current cursor position (nothing visibly changes)
        time.sleep(0.3)
        after = _mac_idle_seconds()
        counted = after is not None and before is not None and after < 0.3 < before
        print(f"5) seconds since our own event: {after} ->",
              "⚠ our own input is also counted as real-hardware input (the 0.4 s exclusion acts as insurance against mix-ups)" if counted else
              "OK our own input is not counted (only human input is)")
    except (Measurement, ValueError, subprocess.SubprocessError) as exc:
        print("5) ✗ could not send events (Accessibility permission):", exc)
    try:
        path = _shot_dir() / "selftest.png"
        _write_private(path, _mac_capture(0, 0, 200, 120))
        print("6) capture: OK", path)
    except Measurement as exc:
        print("6) ✗", exc)
    print("7) coordinate table:", "Mac measurements loaded " + ",".join(_MAC_LAYOUT_LOADED) if _MAC_LAYOUT_LOADED
          else "not measured (mac_layout.json is missing)", "/ send key:", SEND_KEY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
