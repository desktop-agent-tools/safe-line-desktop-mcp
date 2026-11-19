# Using it on a Mac (experimental)

The same MCP server runs on macOS. Tool names and arguments are the same as on Windows. Mac support is
**experimental and not tested on a real Mac** (the coordinates were measured on one machine; the Mac code paths are
only exercised by tests with stand-ins), so look at every screenshot before clicking.

User-activity watching on macOS: the server reads the time since the last real input (HIDIdleTime). Input counts as
yours (and stops the step) when it is newer than the last event this server sent; only input inside the execution
window of one of our own events (a fraction of a second) is attributed to the server. If the idle time cannot be read,
foreground steps are refused. A touch inside that sub-second window is not seen.

Checks before every injected event: the user-activity check and a check that LINE is the frontmost application run
before every key press, typed text, cursor move, click, drag and scroll, and again (without waiting) right before Cmd+V
and before the button of a click. macOS can only check the application, not the individual window. A multi-click, a drag
and a scroll are each sent to the helper as one helper call, so they cannot be interrupted midway: the check happens
before the call, not between its clicks or steps.

## Why a dedicated helper

LINE for Mac does not answer accessibility queries, so generic "computer use" tools can read the screen but
refuse to type into the message box. This server posts mouse/keyboard events (CGEvent) straight to screen
coordinates through `server/line_desktop_mac.js` (JXA, `osascript -l JavaScript`) and pastes text with Cmd+V.
Nothing else is needed: Python 3 and macOS built-in tools only.

| Part | Mac |
|---|---|
| Input | `line_desktop_mac.js`: window list, bring to front, click, drag, scroll, keys, seconds since the last real input |
| Screenshot | `screencapture -R`, scaled to window points (image coordinates = click coordinates) |
| Paste | `pbcopy` → Cmd+V; the previous text on the clipboard is restored (images/files are left alone) |
| Modifiers | `ctrl+a` in tool calls means **Cmd**+A on Mac; `alt` is Option; `realctrl` is the Control key |
| Send key | Measured once: **Cmd+Return** sent, Return did not (LINE setting dependent). Set `SEND_KEY` with `line_layout` |
| Watching the user | seconds since the last HID input (`CGEventSourceSecondsSinceLastEventType`) |
| Background operation | none: every step brings LINE to the front |

## Setup (once)

1. Register the MCP server in your AI client (see `examples/mcp-clients.md`), using `python3`.
2. System Settings → Privacy & Security: give the app that runs your AI agent (Terminal, Claude, Codex, ...)
   - **Accessibility** (clicking and typing)
   - **Screen Recording** (screenshots and window titles)
   Restart that app. Allow "control System Events" if osascript asks.
3. Self-test (touches nothing in LINE): `python3 server/line_desktop_mcp.py --selftest`. Fix every ✗ line.

## First run, with the owner watching

1. `line_status` → `line_screenshot`. The image must show LINE and not be black (black = no Screen Recording).
2. Read-only steps: `line_scroll`, `line_scan_list`.
3. Move the mouse while a tool runs: it must stop with "user is operating". **Do not send anything until this
   works.**
4. Open a chat that only you can see (your own memo chat). Read its name in the screenshot.
5. `line_send ... dry_run=true replace_draft=true` with `composer_x` / `composer_y` read from the screenshot. Check the
   pasted text. (On macOS the server cannot read what is already in the box, so `line_send` refuses unless
   `replace_draft=true`: use this only in a memo chat whose box is empty, and never when the user has a draft there.)
6. Send for real. If nothing is sent, try `send_key=cmd+Return`.
7. Save what worked: `line_layout op=set name=SEND_KEY value=cmd+Return`, and the points you measured
   (see the `line-desktop-recalibrate` skill).

## Measured once (LINE for Mac, one machine)

- The chat list is a window titled `LINE` (382×687). An opened chat is a **separate window** (375×566).
- Search results open with a double click, not a single click.
- Message box at (190, 463), i.e. 103 points above the bottom of the chat window.
- Cmd+Return sent a message; Return did not.
- When the user moved the mouse, the tool stopped before sending any input.

`server/mac_layout.json` holds these defaults. Your own settings file overrides them.

## Not verified yet

- Whether the tool's own events are counted as "real input" (`--selftest` step 5 tells you).
- Whether right-click menus show up as separate windows in the window list.
- Typing `@` for mentions while a Japanese IME is on. Avoid mentions on Mac until you have tested this.
- Restoring a window hidden in the Dock with `open -a LINE`.
