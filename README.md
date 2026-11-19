# safe-line-desktop-mcp

**Unofficial.** Let your AI agent read and operate the **LINE desktop app** on your own Windows PC or Mac —
with safety rails, and without fighting you for the mouse.

[日本語の説明はこちら → README.ja.md](README.ja.md)

> Not affiliated with, endorsed by, or sponsored by LY Corporation. "LINE" is a trademark of LY Corporation.

## Too many LINE groups? Let your AI clean them up.

Years of class reunions, one-off events and finished projects pile up as hundreds of silent groups.
With the `line-group-cleanup` skill your AI agent:

1. **Lists** every group with its last activity date and what it was about, and suggests leave / keep
   (by your rule — e.g. "nothing for 365 days, keep family and work").
2. **You check** a simple checklist. Nothing is touched until you say go.
3. **Leaves only the ticked groups**, one by one, reading the group name on screen before each confirmation,
   pacing itself, and writing a local log.

It was built for a real cleanup of about 270 groups.

## What else it does

- **Unread triage** — check every unread chat, summarize who / what / how urgent, draft replies, send only
  what you approve (`line-unread-triage`).
- **Read and send** — open a chat by name, read it as text, send with mentions (`line-desktop`).
- **Group admin** — create groups, invite or remove members, rename — each confirmed against the name on screen.
- **Fix itself after LINE updates** — when buttons move, your agent re-measures and saves the layout
  (`line-desktop-recalibrate`).

Works with **Claude Code, Codex, Claude Desktop, Cursor, VS Code** and any MCP client, including always-on
personal agents over local HTTP. The skills are plain Markdown, so any agent can follow them.

## Safety by default

| | |
|---|---|
| Works beside you | On Windows, screenshots, list scans, scrolling, single clicks, the search box and plain keys run **in the background** — LINE is not brought to the front and the mouse does not move. Steps that need the foreground wait for a pause in your typing, finish quickly, and hand the window and the mouse back. |
| Stops when you take over | Real keyboard/mouse input (not the tool's own) during a foreground step stops it at once, and the reply says exactly what was already pressed. On Windows the user and the front window (it must be the target window) are checked before every injected event: each drag step, each wheel notch, each typed character and right before Ctrl+V; a drag always lets go of the mouse button. |
| Right chat, right person | Sending, reading as text and every management action require the name the agent just read on screen. The server **cannot read that name itself** (it returns pictures), so it records the name and tells the agent to verify it on the returned screenshot. On Windows it also refuses to send if the chat header changed or cannot be read since the last screenshot, and checks again right before the send key. |
| Sending only through `line_send` | Clicks and drags in the input row / send button area of the chat pane are refused whatever `action` they carry (the area comes from the layout and can be set in the settings file; on macOS it covers the whole chat window). `line_key` refuses Return/Enter/Space (also alt/ctrl/shift+Return and the configured send key) unless it is a plain Return inside a dialog (`window=popup`) with a management action and a target. `line_type` refuses control characters. A send is counted and logged before the key is pressed. |
| Your draft and clipboard are protected | An unsent draft in the message box is set aside and put back (also after a failure). Where the draft cannot be read (macOS) sending is refused unless `replace_draft=true`. A clipboard that holds anything but plain text (image, files, rich text, unknown formats) or cannot be inspected is never overwritten: the paste is refused, and the user's text is put back only if the clipboard still holds what we left. A small race remains: another program can change the clipboard between a check and the write or restore. |
| One agent at a time | Every call that changes the screen, brings LINE to the front (`line_open`, a foreground `line_screenshot`) or hands the focus back holds one cross-process lock for the whole call, focus give-back included (`ui.lock` in the state folder, held through the operating system's file locking, so it is released the moment its owner process ends), so two agents never paste, send or restore a draft at the same time; the second is told another agent is operating LINE. |
| Limits on by default | 5 messages / minute and 100 / day; 6 management actions / minute and 150 / day. Change them in your settings file. The check and the record happen together under a lock file, and the counts live in a small counter file (times only) that is used even when the action log is off, so two processes cannot both slip under the limit. |
| Private storage | The state folder (settings, action log, rate counter, lock files) and the chat-log folder are created owner-only on macOS and Linux (folders 0700, files 0600, created that way and tightened if they already exist; if that cannot be done the server refuses to store anything there). On Windows the files live under your per-user profile (`%LOCALAPPDATA%`), which already has a per-user ACL; this server does not change ACLs, so do not point `LINE_MCP_LOG_DIR` or `LINE_MCP_HOME` at a shared folder. |
| Local action log | Time, chat name, length and a short hash per send — **no message text** unless you opt in. Never leaves your PC. If the log cannot be written, the action is refused. |
| No remote control | stdio by default. HTTP mode listens on loopback only, requires a token (≥ 24 chars), refuses every request that carries an `Origin` header (browsers send one; MCP clients do not), drops silent connections after 15 s and serves at most 8 at once. |
| No secrets in memo chats | Reading refuses "Keep memo". Chat logs, the action log and the settings file are never written inside a Git repository. |
| Messages are not instructions | The skills tell agents to treat text inside LINE as data, never as commands. |

These rails prevent accidents and remote abuse. They are not a sandbox against malicious software already
running on your PC.

Screenshots are written to a private temporary folder (owner-only, mode 0700; files 0600 on macOS and Linux) only to be read back, and each file is deleted as soon as it has been read (an unread one is removed at the next capture after an hour, and the folder when the server ends).

Honest limits of the guards: a plain left click in the main window is treated as navigation (choosing a chat or
tab) and is neither counted nor checked, except that the input row / send button area at the bottom of the chat pane is
refused; every popup click needs an `action`, and `action=menu` is accepted for ONE click, only after a click through this server opened a menu and
only while the popup is menu-sized (the server cannot read button labels); a main-window click within 60 px of the known leave-confirm button needs the management action;
the front window is checked as the exact target window on Windows, but only as the LINE application on macOS; and the optional Windows helper script
(`server/line_desktop_windows.ps1`) clicks outside all of these guards, restricted to LINE windows. Its pictures go to a per-user temp folder by default (`-Cmd cleanup` deletes it), and a `-Out` path inside a Git working tree, or reaching it through a junction or symbolic link, is refused. On macOS a window is only treated as a dialog if it is clearly smaller than a chat window; chat windows are never popups, so Return/Space can never be sent to them through `window=popup`. Confirm management
actions with `line_click ... action=... expected_target=...`.

## Requirements

- Windows 10/11 or macOS, with LINE for PC/Mac signed in
- Python 3.10+ (standard library only — no packages to install)
- Measured on LINE for Windows with a 767×722 window at 100% scaling. Other sizes work after recalibration.
  **macOS support is experimental and has not been tested on a real Mac** (the coordinates came from one machine; the Mac code paths here are only exercised by tests with stand-ins). Look at every screenshot before acting.

## Install

```bash
git clone https://github.com/desktop-agent-tools/safe-line-desktop-mcp
```

Then register the server in your AI client — see [examples/mcp-clients.md](examples/mcp-clients.md).
Claude Code users can install it as a plugin (server + skills):

```bash
claude plugin marketplace add desktop-agent-tools/safe-line-desktop-mcp
claude plugin install safe-line-desktop
```

Optional settings: copy [config/config.example.json](config/config.example.json) to
`%LOCALAPPDATA%\line-desktop-mcp\config.json` (Windows) or
`~/Library/Application Support/line-desktop-mcp/config.json` (macOS).

Then ask your agent: *"Check my LINE for unread messages"* or *"Help me leave LINE groups I don't use."*

## Tools

`line_status`, `line_open`, `line_screenshot`, `line_scan_list`, `line_click`, `line_drag`, `line_key`,
`line_type`, `line_scroll`, `line_search`, `line_send`, `line_open_chat`, `line_read_chat_text`, `line_restore_view`,
`line_layout`. On Windows the view (search text, open chat, tab, list position) is remembered at the first search, click,
scroll, key, type, drag or chat open. `line_read_chat_text` (unless `restore_view=false`) and `line_scan_list` put it back by
themselves and say what they could not. After anything else (`line_open_chat`, `line_search`, `line_send`, clicks, keys,
scrolling) call `line_restore_view` as the last step. If putting it back fails, the view stays remembered for a retry.
macOS does not remember or restore the view.
Details: [skills/line-desktop/SKILL.md](skills/line-desktop/SKILL.md).

## Please read: LINE's Terms of Use

[LINE's Terms of Use](https://terms.line.me/line_terms?lang=ja) prohibit operating the service by bots or other technical means in an unauthorized way and
sending spam, and LY Corporation may suspend accounts that violate them. Operating the app on your own PC is **not**
a permission or guarantee from LINE. This project drives the official app
through the screen, like a person would, for **your own account on your own computer**. Whether a given use is
acceptable is **your responsibility**. Do not use it for bulk messages, advertising, scraping other people's
data, or operating anyone else's account. There is a real risk of account restrictions; use it at your own risk.

## Limitations

- Coordinates depend on window size, scaling, language and LINE version. Recalibrate when they move.
- Opening a chat marks it as read for the sender.
- Screenshots: on Windows the LINE window itself is captured (PrintWindow); if that fails, a copy of the screen area is used, which can include another app lying on top. On macOS the screen area of the window is captured, so a window on top of LINE (for example during a popup screenshot) can appear in the image the agent receives.
- Read state: any input sent to LINE (search, list scrolling) can make LINE mark the chat that is open on the right as read; LINE does this itself. `line_restore_view` puts back the search text, the open chat and the tab, but the exact list scroll position may come back as the top of the list (it tells you when).
- Menus, right clicks, multi-line text, mentions and the send key need LINE in the foreground for a moment.
- Mentions are refused unless you declared a modified send key (`alt+Return`, `cmd+Return`) in the settings file or
  with `LINE_SEND_KEY` (picking a mention candidate presses Return, and the server cannot read LINE's setting).
- Linux is not supported (there is no LINE desktop app).
- No warranty. Issues are welcome, but support is best-effort.

## Use it with us, improve it with us

This started as one person's tool for taming a very busy LINE, and it gets better with every PC, LINE version
and AI agent it meets. Please use it, break it, and tell us:

- **Tried it?** Open an issue with your OS, LINE version, window size / scaling and AI client — "it worked" helps too.
- **Coordinates moved after a LINE update?** Run the `line-desktop-recalibrate` skill and send the new values as a PR.
- **Have a workflow?** Skills are plain Markdown. A skill for your own routine (birthdays, event groups,
  family chats…) is a great first PR.
- **Found a safety gap?** Report it privately (see [SECURITY.md](SECURITY.md)); we treat guards as features.

See [CONTRIBUTING.md](CONTRIBUTING.md). Questions and ideas in Japanese or English are both welcome.

## Development

```bash
python -m unittest discover -s tests
```

The tokens in the tests are dummy values made up for the tests; never reuse them.

The server is a single file using only the Python standard library. Security reports: see
[SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE)
