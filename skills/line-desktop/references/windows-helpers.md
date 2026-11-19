# Windows helper script (separate LINE windows)

`server/line_desktop_windows.ps1` handles LINE windows that the MCP tools do not see: a group's **Notes**
window, the **image viewer**, and stacked dialogs (member list, friend picker). Run it without a console window:

```
powershell -NoProfile -WindowStyle Hidden -File server/line_desktop_windows.ps1 -Cmd <command> [options]
```

| Command | What it does |
|---|---|
| `list` | List LINE windows with position and size |
| `front` | Bring the main LINE window to the front (and tell the MCP server the input was ours) |
| `note-size -Width 428 -Height 1000` | Resize the Notes window (428 is the widest) |
| `note-shot [-Out path]` | Screenshot the Notes window |
| `note-scroll -Notches -3` | Scroll the Notes window (negative = down; about 1/3 screen per notch) |
| `note-click -X n -Y n [-Right]` | Click inside the Notes window (its own top-left origin) |
| `viewer-shot [-Out path]` / `viewer-close` | Screenshot / close the image viewer |
| `real-click -X n -Y n [-Mode move\|left\|right]` | Move the real cursor (hover-only buttons, e.g. the × on a member row) |
| `screen-shot -SX -SY -SW -SH [-Out path]` | Screenshot a rectangle inside a LINE window (the window itself is captured, then cropped) |
| `cleanup` | Close the viewer and Notes windows, and delete the helper's default picture folder |

Pictures can show conversations. Without `-Out` they are saved in `%TEMP%\line-desktop-mcp-helper` (per user); a path
inside a Git working tree is refused (`OUT_INSIDE_GIT_TREE`). Use a path in your temp folder if you give `-Out`, and delete the
files when you are done (`cleanup` removes the default folder; your own `-Out` files stay until you delete them).

## Reading a group's Notes

1. Open the group and check its name.
2. The document icon left of ⋮ at the top right of the chat (about (720, 81)) opens **Notes** in its own window.
3. `note-size`, then `note-shot` and read the image. Newest posts are on top.
4. `note-scroll -Notches -3` to go down. "…more" expands with `note-click`.
5. Small images: `note-click` on the image opens the viewer; `viewer-shot`, then `viewer-close`.
6. `cleanup`, then `line_search text=""`.

## Notes

- These commands may bring windows to the front. Screenshots (`note-shot`, `viewer-shot`, `screen-shot`) capture the
  LINE window itself with PrintWindow, so other apps that overlap it do not appear; if PrintWindow fails nothing is
  saved. `screen-shot` takes a rectangle that lies inside one LINE window.
- Clicks made with this script bypass the MCP server's guards (limits, target check, action log). The script limits
  them to LINE windows (`real-click` only inside the main window while it is in front, `screen-shot` only inside a
  LINE window, the image viewer only in a process named LINE), but nothing more. Use them to
  open and read windows. Confirm management actions with `line_click ... action=... expected_target=...`
  whenever the button is reachable that way.
- When two dialogs are stacked, `line_click window=popup` may not know which one to click. Use `real-click`
  (main-window coordinates) and `screen-shot` for that case.
