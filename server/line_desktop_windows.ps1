# Window operations for reading the LINE desktop app's Note window and enlarged-image window (read-only).
# Steps measured on 2026-10-27. Used when reading a chat's Note tab.
# It does not press the send key, but its clicks (note-click, real-click) are OUTSIDE the MCP server's guards: no limits,
# no target check, no action log, no check for a human at the keyboard beyond the input declaration. They are restricted to
# LINE windows (real-click: inside the main window and only while it is in front; screen-shot: inside a LINE window;
# the viewer is found only in a process named LINE; real-click re-checks the front window, the cursor and the user's
# input right before the button goes down, and screen-shot captures the LINE window itself with PrintWindow). Use them to open and read windows, never to confirm a
# management action: use line_click with action and expected_target for that.
# Call it with the window hidden (-WindowStyle Hidden).
#   powershell -NoProfile -WindowStyle Hidden -File scripts\line_desktop_windows.ps1 -Cmd front
# Commands that move the real cursor or press Alt declare right afterwards to the tool that "the last input was mine" (Mark-Own).
#   Without the declaration the tool would mistake it for the user's activity and stall for a few seconds. The declaration only counts when it matches Windows' last input time exactly,
#   so if the user touches anything afterwards the tool stops at once (the user can still stop it at any time).
# Note: PowerShell variable names are case-insensitive ($h and $H are the same). The window handle and the width/height use different names.
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('list', 'front', 'note-size', 'note-shot', 'note-scroll', 'note-click', 'viewer-shot', 'viewer-close', 'cleanup', 'real-click', 'screen-shot')]
  [string]$Cmd,
  [string]$Out = '',          # where *-shot saves (png); empty = a private temp folder; never inside a Git working tree
  [int]$X = 0, [int]$Y = 0,   # note-click: coordinates with the note window's top-left as the origin
  [switch]$Right,             # note-click: right click (opens a menu)
  [int]$Notches = -3,         # note-scroll: negative scrolls down, positive up (1 notch = about 1/3 screen)
  [int]$Width = 428, [int]$Height = 1000,  # note-size
  [ValidateSet('move', 'left', 'right')][string]$Mode = 'move',   # real-click: hover only / left / right
  [int]$SX = 0, [int]$SY = 0, [int]$SW = 300, [int]$SH = 540      # screen-shot: region in absolute screen coordinates
)
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
public class LW {
  public delegate bool CB(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(CB c, IntPtr l);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern void keybd_event(byte v, byte s, uint f, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  public struct PT { public int X, Y; }
  [DllImport("user32.dll")] static extern bool GetCursorPos(out PT p);
  public static bool CursorIs(int x, int y) { PT p; return GetCursorPos(out p) && p.X == x && p.Y == y; }
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LII i);
  public struct LII { public uint cbSize; public uint dwTime; }
  public static long LastInputTick() { var i = new LII(); i.cbSize = (uint)Marshal.SizeOf(typeof(LII)); return GetLastInputInfo(ref i) ? (long)i.dwTime : -1; }
  public struct R { public int L, T, Rr, B; }
  public class Win { public IntPtr H; public uint Pid; public string Title; public R Rect; }
  public static List<Win> All() {
    var o = new List<Win>();
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      uint p; GetWindowThreadProcessId(h, out p);
      var s = new StringBuilder(200); GetWindowText(h, s, 200);
      R r; GetWindowRect(h, out r);
      o.Add(new Win { H = h, Pid = p, Title = s.ToString(), Rect = r });
      return true;
    }, IntPtr.Zero);
    return o;
  }
  // Main window = title LINE, width 767 (the standard layout). Note window = title LINE, width other than 767, height over 400.
  public static IntPtr FindLine(uint pid, bool main) {
    IntPtr f = IntPtr.Zero;
    foreach (var w in All()) {
      if (w.Pid != pid || w.Title != "LINE") continue;
      int wd = w.Rect.Rr - w.Rect.L, ht = w.Rect.B - w.Rect.T;
      if (ht <= 400 || wd >= 1200) continue;
      if ((wd == 767) == main) f = w.H;
    }
    return f;
  }
  // Return one window titled LINE, including a minimized main window (so the minimized state can be undone)
  public static IntPtr FindAnyLine(uint pid) {
    foreach (var w in All()) if (w.Pid == pid && w.Title == "LINE") return w.H;
    return IntPtr.Zero;
  }
  // Enlarged-image window = a title of the form "sender name(1/3)". Opens in a separate process (a different pid from the main window).
  // True when the process that owns the window is the LINE app (checked by process name, so another app whose title
  // ends in "(1/3)" is never treated as the viewer).
  public static bool IsLineProcess(uint pid) {
    try { return string.Equals(System.Diagnostics.Process.GetProcessById((int)pid).ProcessName, "LINE", StringComparison.OrdinalIgnoreCase); }
    catch { return false; }
  }
  // The visible window of the LINE app that contains the screen rectangle (IntPtr.Zero when there is none).
  public static IntPtr LineWindowContaining(int x, int y, int w, int h) {
    foreach (var win in All()) {
      if (!IsLineProcess(win.Pid)) continue;
      if (x >= win.Rect.L && y >= win.Rect.T && x + w <= win.Rect.Rr && y + h <= win.Rect.B) return win.H;
    }
    return IntPtr.Zero;
  }
  public static IntPtr FindViewer() {
    IntPtr f = IntPtr.Zero;
    foreach (var w in All()) if (IsLineProcess(w.Pid) && Regex.IsMatch(w.Title, @"\(\d+/\d+\)$")) f = w.H;
    return f;
  }
}
'@

function Get-LinePid { $p = Get-Process LINE -ErrorAction SilentlyContinue | Select-Object -First 1; if ($p) { return [uint32]$p.Id } else { return [uint32]0 } }
function Save-Shot([IntPtr]$hw, [string]$path) {
  $rc = New-Object LW+R
  [LW]::GetWindowRect($hw, [ref]$rc) | Out-Null
  $bw = [int]($rc.Rr - $rc.L); $bh = [int]($rc.B - $rc.T)
  # The window itself is captured with PrintWindow, so other apps that overlap it never appear in the picture.
  $bmp = New-Object System.Drawing.Bitmap($bw, $bh)
  $gr = [System.Drawing.Graphics]::FromImage($bmp)
  $hdc = $gr.GetHdc()
  $okp = [LW]::PrintWindow($hw, $hdc, 2)       # PW_RENDERFULLCONTENT
  $gr.ReleaseHdc($hdc); $gr.Dispose()
  if (-not $okp) { $bmp.Dispose(); 'PRINTWINDOW_FAILED (nothing saved)'; exit 1 }
  $bmp.Save($path); $bmp.Dispose()
  "saved $bw x $bh -> $path"
}
function Mark-Own {
  # Call right after producing input. Writes Windows' last input time to a file the tool reads (the tool treats input as its own only when the time matches)
  $tick = [LW]::LastInputTick()
  if ($tick -lt 0) { return }
  $base = $env:LINE_MCP_OWN_TICK_FILE
  if (-not $base) { $base = Join-Path $env:LOCALAPPDATA 'line-desktop-mcp\own-input-tick.txt' }
  New-Item -ItemType Directory -Force -Path (Split-Path $base) | Out-Null
  [System.IO.File]::WriteAllText($base, [string]$tick, [System.Text.Encoding]::ASCII)
}
function Make-Top([IntPtr]$hw) { [LW]::SetWindowPos($hw, [IntPtr](-1), 0, 0, 0, 0, 0x0013) | Out-Null; Start-Sleep -Milliseconds 400 }   # temporarily topmost (NOSIZE|NOMOVE|NOACTIVATE)
function Make-Normal([IntPtr]$hw) { [LW]::SetWindowPos($hw, [IntPtr](-2), 0, 0, 0, 0, 0x0013) | Out-Null }

function Resolve-Out([string]$p, [string]$name) {
  # Pictures of LINE windows can show conversations. They are never written inside a Git working tree, and without -Out
  # they go to a per-user temp folder (-Cmd cleanup deletes it; delete your own -Out files when you are done).
  $dir = Join-Path ([System.IO.Path]::GetTempPath()) 'line-desktop-mcp-helper'
  if (-not $p) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $p = Join-Path $dir ($name + '-' + [guid]::NewGuid().ToString('N') + '.png')
  }
  $full = [System.IO.Path]::GetFullPath($p)
  # A junction or symbolic link anywhere on the path (the file itself or any folder above it that exists) could point
  # into a Git working tree, so any reparse point is refused before the .git walk below.
  $probe = $full
  while ($probe) {
    if (Test-Path -LiteralPath $probe) {
      $item = Get-Item -LiteralPath $probe -Force
      if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { 'OUT_THROUGH_LINK (refused: ' + $probe + ' is a junction or symbolic link)'; exit 1 }
    }
    $above = Split-Path -Parent $probe
    if ($above -eq $probe) { break }
    $probe = $above
  }
  $d = Split-Path -Parent $full
  while ($d) {
    if (Test-Path -LiteralPath (Join-Path $d '.git')) { 'OUT_INSIDE_GIT_TREE (refused: ' + $full + ')'; exit 1 }
    $up = Split-Path -Parent $d
    if ($up -eq $d) { break }
    $d = $up
  }
  return $full
}

$linePid = Get-LinePid
if ($linePid -eq 0) { 'LINE_NOT_RUNNING'; exit 1 }
if ($Cmd -in @('note-shot', 'viewer-shot', 'screen-shot')) { $Out = Resolve-Out $Out $Cmd }

switch ($Cmd) {
  'list' {
    foreach ($w in [LW]::All()) {
      if ($w.Pid -eq $linePid -or ([LW]::IsLineProcess($w.Pid) -and $w.Title -match '\(\d+/\d+\)$')) {
        $wd = $w.Rect.Rr - $w.Rect.L; $ht = $w.Rect.B - $w.Rect.T
        if ($wd -gt 200 -and $ht -gt 200) { "$($w.Title) | $($w.Rect.L),$($w.Rect.T) ${wd}x${ht}" }
      }
    }
  }
  'front' {
    # Used when LINE is hidden behind other windows and the tool's bring-to-front does not work.
    # Press Alt once (to allow bringing the window to the front). This input is declared with Mark-Own, so the tool does not mistake it for the user's activity.
    $hm = [LW]::FindLine($linePid, $true)
    if ($hm -eq [IntPtr]::Zero) {
        # A minimized window is about 160x28 and is not found as the main window. Find the window titled LINE and restore it to its normal size
        $hm = [LW]::FindAnyLine($linePid)
        if ($hm -eq [IntPtr]::Zero) { 'MAIN_NOT_FOUND'; exit 1 }
        [LW]::ShowWindow($hm, 9) | Out-Null          # SW_RESTORE
        Start-Sleep -Milliseconds 800
        $hm = [LW]::FindLine($linePid, $true)
        if ($hm -eq [IntPtr]::Zero) { 'MAIN_NOT_FOUND_AFTER_RESTORE'; exit 1 }
    }
    Make-Top $hm
    [LW]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [LW]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
    Mark-Own
    [LW]::ShowWindow($hm, 9) | Out-Null
    [LW]::SetForegroundWindow($hm) | Out-Null
    'fronted'
  }
  'note-size' {
    $hn = [LW]::FindLine($linePid, $false)
    if ($hn -eq [IntPtr]::Zero) { 'NOTE_WINDOW_NOT_FOUND'; exit 1 }
    $rc = New-Object LW+R; [LW]::GetWindowRect($hn, [ref]$rc) | Out-Null
    [LW]::SetWindowPos($hn, [IntPtr](-1), $rc.L, $rc.T, $Width, $Height, 0x0010) | Out-Null   # NOACTIVATE
    "resized to $Width x $Height"
  }
  'note-shot' {
    $hn = [LW]::FindLine($linePid, $false)
    if ($hn -eq [IntPtr]::Zero) { 'NOTE_WINDOW_NOT_FOUND'; exit 1 }
    Make-Top $hn
    Save-Shot $hn $Out
  }
  'note-scroll' {
    $hn = [LW]::FindLine($linePid, $false)
    if ($hn -eq [IntPtr]::Zero) { 'NOTE_WINDOW_NOT_FOUND'; exit 1 }
    $rc = New-Object LW+R; [LW]::GetWindowRect($hn, [ref]$rc) | Out-Null
    $sx = $rc.L + 200; $sy = $rc.T + 500
    $delta = $Notches * 120
    $wp = [IntPtr]([int64](($delta -band 0xFFFF) -shl 16))
    $lp = [IntPtr]([int64]((($sy -band 0xFFFF) -shl 16) -bor ($sx -band 0xFFFF)))
    [LW]::PostMessage($hn, 0x020A, $wp, $lp) | Out-Null   # WM_MOUSEWHEEL. Inertial scrolling takes 1 to 2 seconds to stop
    Start-Sleep -Milliseconds 1500
    'scrolled'
  }
  'note-click' {
    # Click the note window's coordinates (top-left origin). Clicking an image opens the enlarged-image window in a separate process. Clicking "もっと見る" (See more) expands the text.
    $hn = [LW]::FindLine($linePid, $false)
    if ($hn -eq [IntPtr]::Zero) { 'NOTE_WINDOW_NOT_FOUND'; exit 1 }
    $lp = [IntPtr]([int64]((($Y -band 0xFFFF) -shl 16) -bor ($X -band 0xFFFF)))
    [LW]::PostMessage($hn, 0x0200, [IntPtr]0, $lp) | Out-Null; Start-Sleep -Milliseconds 150
    if ($Right) {
        [LW]::PostMessage($hn, 0x0204, [IntPtr]2, $lp) | Out-Null; Start-Sleep -Milliseconds 80   # WM_RBUTTONDOWN
        [LW]::PostMessage($hn, 0x0205, [IntPtr]0, $lp) | Out-Null; Start-Sleep -Milliseconds 1500  # WM_RBUTTONUP
    } else {
        [LW]::PostMessage($hn, 0x0201, [IntPtr]1, $lp) | Out-Null; Start-Sleep -Milliseconds 80
        [LW]::PostMessage($hn, 0x0202, [IntPtr]0, $lp) | Out-Null; Start-Sleep -Milliseconds 1500
    }
    'clicked'
  }
  'real-click' {
    # With the real cursor, move to a coordinate of the main window (top-left origin) and click if -Mode is left/right. It uses mouse_event, so
    # buttons that appear only on hover (such as the x in the member list) show up too. -Mode move only hovers (the default).
    # Assumes the main window is in front (run front first). Never presses a confirm button.
    $hm = [LW]::FindLine($linePid, $true)
    if ($hm -eq [IntPtr]::Zero) { 'MAIN_NOT_FOUND'; exit 1 }
    $rc = New-Object LW+R; [LW]::GetWindowRect($hm, [ref]$rc) | Out-Null
    if ($X -lt 0 -or $Y -lt 0 -or $X -ge ($rc.Rr - $rc.L) -or $Y -ge ($rc.B - $rc.T)) { 'OUTSIDE_LINE_WINDOW'; exit 1 }
    if ([LW]::GetForegroundWindow() -ne $hm) { 'LINE_NOT_IN_FRONT (run front first)'; exit 1 }
    $ax = $rc.L + $X; $ay = $rc.T + $Y
    $lastInput = [LW]::LastInputTick()           # any input from the user after this point aborts the click
    [LW]::SetCursorPos($ax, $ay) | Out-Null; Start-Sleep -Milliseconds 250
    [LW]::SetCursorPos($ax + 1, $ay + 1) | Out-Null; Start-Sleep -Milliseconds 250
    if ($Mode -ne 'move') {
        # Checked immediately before the button goes down: LINE still in front and not moved, the cursor where we
        # put it, and no input from the user since the start.
        $rc2 = New-Object LW+R; [LW]::GetWindowRect($hm, [ref]$rc2) | Out-Null
        if ([LW]::GetForegroundWindow() -ne $hm -or $rc2.L -ne $rc.L -or $rc2.T -ne $rc.T) { 'ABORTED_LINE_NOT_IN_FRONT'; exit 1 }
        if (-not [LW]::CursorIs($ax + 1, $ay + 1)) { 'ABORTED_CURSOR_MOVED'; exit 1 }
        if ([LW]::LastInputTick() -ne $lastInput) { 'ABORTED_USER_INPUT'; exit 1 }
    }
    if ($Mode -eq 'left') { [LW]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 60; [LW]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero) }
    if ($Mode -eq 'right') { [LW]::mouse_event(8, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 60; [LW]::mouse_event(16, 0, 0, 0, [UIntPtr]::Zero) }
    Mark-Own
    Start-Sleep -Milliseconds 900
    "done $Mode at screen $ax,$ay"
  }
  'screen-shot' {
    # Capture a region in absolute screen coordinates (for dialogs and menus in other windows; note-shot works only for the note window)
    # The LINE window itself is captured with PrintWindow, so other apps that overlap it never appear in the picture.
    $hw = [LW]::LineWindowContaining($SX, $SY, $SW, $SH)
    if ($hw -eq [IntPtr]::Zero) { 'OUTSIDE_LINE_WINDOW'; exit 1 }
    $rw = New-Object LW+R; [LW]::GetWindowRect($hw, [ref]$rw) | Out-Null
    $ww = [int]($rw.Rr - $rw.L); $wh = [int]($rw.B - $rw.T)
    $full = New-Object System.Drawing.Bitmap($ww, $wh)
    $gw = [System.Drawing.Graphics]::FromImage($full)
    $hdc = $gw.GetHdc()
    $okp = [LW]::PrintWindow($hw, $hdc, 2)       # PW_RENDERFULLCONTENT
    $gw.ReleaseHdc($hdc); $gw.Dispose()
    if (-not $okp) { $full.Dispose(); 'PRINTWINDOW_FAILED (nothing saved)'; exit 1 }
    $crop = $full.Clone((New-Object System.Drawing.Rectangle(($SX - $rw.L), ($SY - $rw.T), $SW, $SH)), $full.PixelFormat)
    $crop.Save($Out); $crop.Dispose(); $full.Dispose()
    "saved $SW x $SH -> $Out"
  }
  'viewer-shot' {
    $hv = [LW]::FindViewer()
    if ($hv -eq [IntPtr]::Zero) { 'VIEWER_NOT_FOUND'; exit 1 }
    Make-Top $hv
    Save-Shot $hv $Out
  }
  'viewer-close' {
    $hv = [LW]::FindViewer()
    if ($hv -eq [IntPtr]::Zero) { 'VIEWER_ALREADY_CLOSED'; exit 0 }
    [LW]::PostMessage($hv, 0x0010, [IntPtr]0, [IntPtr]0) | Out-Null   # WM_CLOSE. Takes about 1 second to close
    Start-Sleep -Milliseconds 1500
    if ([LW]::FindViewer() -eq [IntPtr]::Zero) { 'viewer closed' } else { 'viewer still open' }
  }
  'cleanup' {
    # Cleanup after reading: close the enlarged-image and note windows and release the main window's topmost state. Clear the search box with line_search "".
    $hv = [LW]::FindViewer()
    if ($hv -ne [IntPtr]::Zero) { [LW]::PostMessage($hv, 0x0010, [IntPtr]0, [IntPtr]0) | Out-Null; Start-Sleep -Milliseconds 1500 }
    $hn = [LW]::FindLine($linePid, $false)
    if ($hn -ne [IntPtr]::Zero) { Make-Normal $hn; [LW]::PostMessage($hn, 0x0010, [IntPtr]0, [IntPtr]0) | Out-Null; Start-Sleep -Milliseconds 1500 }
    $hm = [LW]::FindLine($linePid, $true)
    if ($hm -ne [IntPtr]::Zero) { Make-Normal $hm }
    Remove-Item -LiteralPath (Join-Path ([System.IO.Path]::GetTempPath()) 'line-desktop-mcp-helper') -Recurse -Force -ErrorAction SilentlyContinue
    'viewer_open=' + ([LW]::FindViewer() -ne [IntPtr]::Zero) + ' note_open=' + ([LW]::FindLine($linePid, $false) -ne [IntPtr]::Zero) + ' main_found=' + ($hm -ne [IntPtr]::Zero)
  }
}
