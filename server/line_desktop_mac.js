// Helper for driving LINE for Mac (JXA = osascript -l JavaScript). The Mac "hands" of line_desktop_mcp.py.
// Created 2026-11-02.
//
// Why JXA: LINE for Mac does not answer accessibility queries (tools that check "which document is the input box editing" refuse input;
// measured on a Mac in 2026-11). Here mouse and key events (CGEvent) are sent straight to screen coordinates,
// so LINE can be clicked and pasted into even though it does not answer accessibility queries. No extra Python package (pyobjc) is needed.
//
// Usage: osascript -l JavaScript line_desktop_mac.js <command> [args...]  -> one line of JSON on stdout
//   windows                         LINE windows (those showing on screen)
//   running                         whether LINE is running
//   front                           the app currently in front
//   activate                        bring LINE to the front
//   click X Y BUTTON COUNT          click a screen coordinate (top-left origin, points). BUTTON = left|right
//   move X Y                        move only the cursor
//   drag X1 Y1 X2 Y2                drag
//   scroll X Y LINES                positive scrolls up, negative down (lines)
//   key KEYCODE FLAGS               one key (FLAGS = sum of modifier keys, e.g. cmd 0x100000)
//   idle                            seconds since the last real-hardware (HID) input
//
// Events are sent to the session layer from a dedicated source (private). They do not pass through the HID layer (real keyboard and mouse), so
// idle (seconds since the last HID input) is expected to count human input only (an assumption; measure it with `--selftest`).
ObjC.import('CoreGraphics');
ObjC.import('AppKit');

var BUNDLE_IDS = ['jp.naver.line.mac', 'jp.naver.line'];
var SESSION_TAP = 1;          // kCGSessionEventTap
var PRIVATE_STATE = -1;       // kCGEventSourceStatePrivate
var HID_STATE = 1;            // kCGEventSourceStateHIDSystemState

function isLine(name, bundle) {
  return name === 'LINE' || BUNDLE_IDS.indexOf(bundle) >= 0;
}

function lineApp() {
  var apps = $.NSWorkspace.sharedWorkspace.runningApplications;
  for (var i = 0; i < apps.count; i++) {
    var a = apps.objectAtIndex(i);
    var name = a.localizedName.isNil() ? '' : a.localizedName.js;
    var bundle = a.bundleIdentifier.isNil() ? '' : a.bundleIdentifier.js;
    if (isLine(name, bundle)) return a;
  }
  return null;
}

function windows() {
  var opts = $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements;
  var info = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(opts, $.kCGNullWindowID))) || [];
  var app = lineApp();
  var pid = app ? app.processIdentifier : -1;
  var out = [];
  for (var i = 0; i < info.length; i++) {
    var w = info[i];
    if (w.kCGWindowOwnerPID !== pid && w.kCGWindowOwnerName !== 'LINE') continue;
    var b = w.kCGWindowBounds || {};
    if ((b.Width || 0) < 30 || (b.Height || 0) < 30) continue;
    if ((w.kCGWindowAlpha === 0)) continue;
    out.push({id: w.kCGWindowNumber, title: w.kCGWindowName || '', layer: w.kCGWindowLayer || 0,
              x: Math.round(b.X), y: Math.round(b.Y), width: Math.round(b.Width), height: Math.round(b.Height)});
  }
  return {windows: out};
}

function src() { return $.CGEventSourceCreate(PRIVATE_STATE); }

function post(e) { $.CGEventPost(SESSION_TAP, e); }

function mouse(type, x, y, button, clickState) {
  var e = $.CGEventCreateMouseEvent(src(), type, $.CGPointMake(x, y), button);
  if (clickState) $.CGEventSetIntegerValueField(e, 1, clickState);   // kCGMouseEventClickState
  post(e);
}

function sleep(sec) { delay(sec); }

function run(argv) {
  var cmd = argv[0];
  var n = function (i) { return Number(argv[i]); };
  var result = {};
  if (cmd === 'windows') {
    result = windows();
  } else if (cmd === 'running') {
    result = {running: lineApp() !== null};
  } else if (cmd === 'front') {
    var f = $.NSWorkspace.sharedWorkspace.frontmostApplication;
    var name = f.localizedName.isNil() ? '' : f.localizedName.js;
    var bundle = f.bundleIdentifier.isNil() ? '' : f.bundleIdentifier.js;
    result = {name: name, bundle: bundle, is_line: isLine(name, bundle)};
  } else if (cmd === 'activate') {
    var app = lineApp();
    if (!app) return JSON.stringify({ok: false, error: 'LINE is not running'});
    app.activateWithOptions(2);   // NSApplicationActivateIgnoringOtherApps
    sleep(0.3);
  } else if (cmd === 'move') {
    mouse(5, n(1), n(2), 0, 0);   // mouseMoved
  } else if (cmd === 'click') {
    var right = argv[3] === 'right';
    var down = right ? 3 : 1, up = right ? 4 : 2, btn = right ? 1 : 0;
    var count = Math.max(1, n(4) || 1);
    mouse(5, n(1), n(2), 0, 0);
    sleep(0.08);
    for (var c = 1; c <= count; c++) {
      mouse(down, n(1), n(2), btn, c);
      mouse(up, n(1), n(2), btn, c);
      sleep(0.06);
    }
  } else if (cmd === 'drag') {
    var x1 = n(1), y1 = n(2), x2 = n(3), y2 = n(4);
    mouse(5, x1, y1, 0, 0);
    sleep(0.08);
    mouse(1, x1, y1, 0, 1);
    for (var s = 1; s <= 10; s++) {
      mouse(6, x1 + (x2 - x1) * s / 10, y1 + (y2 - y1) * s / 10, 0, 1);   // leftMouseDragged
      sleep(0.03);
    }
    mouse(2, x2, y2, 0, 1);
  } else if (cmd === 'scroll') {
    mouse(5, n(1), n(2), 0, 0);
    sleep(0.08);
    var lines = n(3), step = lines > 0 ? 1 : -1;
    for (var k = 0; k < Math.abs(lines); k++) {
      var ev;
      if (typeof $.CGEventCreateScrollWheelEvent2 === 'function') {
        ev = $.CGEventCreateScrollWheelEvent2(src(), 1, 1, step * 3, 0, 0);   // kCGScrollEventUnitLine
      } else {
        ev = $.CGEventCreateScrollWheelEvent(src(), 1, 1, step * 3);
      }
      post(ev);
      sleep(0.02);
    }
  } else if (cmd === 'key') {
    var code = n(1), flags = n(2) || 0;
    var kd = $.CGEventCreateKeyboardEvent(src(), code, true);
    var ku = $.CGEventCreateKeyboardEvent(src(), code, false);
    $.CGEventSetFlags(kd, flags);
    $.CGEventSetFlags(ku, flags);
    post(kd);
    sleep(0.02);
    post(ku);
  } else if (cmd === 'idle') {
    result = {seconds: $.CGEventSourceSecondsSinceLastEventType(HID_STATE, 4294967295)};   // kCGAnyInputEventType
  } else {
    return JSON.stringify({ok: false, error: 'unknown command: ' + cmd});
  }
  result.ok = true;
  return JSON.stringify(result);
}
