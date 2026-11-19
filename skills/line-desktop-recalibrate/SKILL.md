---
name: line-desktop-recalibrate
description: Re-measure the LINE desktop coordinates after a LINE update, a different window size, display scaling or a new PC/Mac, and save them so every LINE flow works again. Use when line-desktop clicks land in the wrong place, the screenshot shows a layout warning, or after updating LINE.
---

# Recalibrate the LINE layout

The `line-desktop` server clicks fixed points inside the LINE window. They were measured on a 767×722 window at
100% scaling. A LINE update, another window size, display scaling or language can move them. This skill
re-measures them with screenshots and saves them in the user's settings file, which overrides the built-in values.

Only read-only clicks are used here (tabs, search box). Nothing is sent and no group is left.

## 1. Look at the current values

`line_layout op=get` returns the settings file path, the window size now and the reference size, and every value:

| Name | What it is |
|---|---|
| `ALL_TAB`, `TAB_FRIENDS`, `GROUP_TAB`, `TAB_OPENCHAT`, `TAB_OFFICIAL` | the tabs above the chat list (All / Friends / Groups / Open chats / Official accounts). Used to put the tab back; the dark underline 20 px below a tab point tells which one is active |
| `SEARCH_BOX`, `SEARCH_CLEAR` | the search box and the × inside it (the × also shows whether a search is active, so put the point on the middle of the grey circle) |
| `LIST_POINT`, `CHAT_POINT` | a point inside the chat list / the conversation (for scrolling) |
| `CHAT_MENU_BUTTON` | ⋮ at the top right of an open chat |
| `NEW_CHAT_BUTTON` | the green button at the bottom right of the list |
| `FIRST_RESULT_POINT` | first row of the "Chats" section in search results |
| `FOCUS_CHAT_POINT` | an empty spot left of the bubbles (no link, no image) |
| `COMPOSER_X`, `COMPOSER_FROM_BOTTOM` | message box: x, and height above the bottom edge |
| `STANDARD_SIZE` (Mac: `LIST_SIZE`, `CHAT_SIZE`) | the window size the values belong to |
| `SEND_KEY` | the key that sends (`alt+Return`, `Return`, `cmd+Return`) |

## 2. Simplest fix first: window size

If the current window size differs from the reference size, ask the user whether they mind resizing LINE to the
reference size. If yes, ask them to drag the edge (or use `server/line_desktop_windows.ps1 -Cmd list` to read the
size) and test again. Often nothing else is needed.

## 3. Measure

1. `line_screenshot`. The image is the window itself: image pixel (x, y) = window coordinate (x, y).
   (On a Mac Retina display the server already scales the image to window points.)
2. Find each element and read the center of it from the image. Zoom into the region if your client can.
3. For each value that moved: `line_layout op=set name=SEARCH_BOX x=196 y=84`
   (for `SEND_KEY`: `value=Return`; for `COMPOSER_FROM_BOTTOM`: `value=103`).
4. If the user keeps a different window size on purpose, also save `STANDARD_SIZE` with that size so the
   size warning goes away.

## 4. Test each value safely

| Value | Safe test | Expected |
|---|---|---|
| `ALL_TAB`, `GROUP_TAB` | `line_click point=GROUP_TAB`, then `point=ALL_TAB` | the list switches |
| `SEARCH_BOX` | `line_search text=zz`, then `line_search text=""` | "zz" appears, then the box is empty |
| `LIST_POINT` | `line_scroll direction=down`, then `up` | the list moves |
| `CHAT_MENU_BUTTON` | open the user's own memo-free chat, `line_click point=CHAT_MENU_BUTTON`, `line_screenshot window=popup`, `line_key key=Escape` | a menu appears, then closes |
| `COMPOSER_*` | `line_send text=test expected_chat=<name> dry_run=true` in a chat the user picks, then clear the box **only if the dry_run result did not say it put an unsent draft back** (`line_key key=ctrl+a`, `line_key key=Delete`); if a draft was put back, leave the box alone; if the result says the draft text was **returned instead of restored** (the chat could not be confirmed, or pasting failed), stop, give the user that text and ask what to do before touching the box | the text is in the message box, not sent |
| `SEND_KEY` | only with the user's consent, send one message to a chat only they can see | one new bubble |

Fix and re-test any value that fails. Then tell the user what changed.

## 5. Share (optional)

If the user wants to help others with the same LINE version, they can open an issue / pull request with
`layout` values, the LINE version, OS, scaling and window size. **Remove screenshots or crop them: they show
chat names and messages.**

## Notes

- Settings file: Windows `%LOCALAPPDATA%\line-desktop-mcp\config.json`, macOS
  `~/Library/Application Support/line-desktop-mcp/config.json` (or `LINE_MCP_CONFIG`). It is plain JSON; you can
  also edit it by hand. Restart the MCP server after hand edits; `line_layout op=set` applies immediately.
- Coordinates for menus inside popups (for example the leave item) are read from screenshots each time, not stored.
