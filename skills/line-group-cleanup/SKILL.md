---
name: line-group-cleanup
description: Clean up LINE groups you no longer use. The AI lists your groups with the last activity date and a suggestion, you tick the ones to leave in a checklist, and only the ticked groups are left, one by one, with a log. Use when the user says they have too many LINE groups, wants to leave old/unused groups, or asks to tidy up LINE. Windows (measured) and Mac (measure first).
---

# Leave unused LINE groups (list → you check → leave)

Leaving a group cannot be undone: you lose its history and member list, and the other members see
"<you> left the group". So this skill always goes **list → the owner checks → leave only what was checked**.
Tools: the `line-desktop` MCP server (see the `line-desktop` skill).

## 0. Agree on the rules with the owner

Ask once, then write the answers at the top of the checklist file:

- **Inactivity threshold** — e.g. "no message for 180 days" (default 365 days).
- **Always keep** — family, current work, clubs they are in, any group with Notes or an Album (save first if
  they still want to leave), groups with a specific word in the name.
- **Scope** — named groups only, or also unnamed multi-person chats.
- **How many per session** — default: stop after 50 and report.

## 1. Make the list (read-only)

1. `line_click point=GROUP_TAB` to show only groups. Don't search by name or scroll the "All" list (it mixes
   friends and official accounts).
2. Go from the bottom of the list (oldest) upwards with `line_scroll` / `line_screenshot`. For each group record:
   name, member count, date of the last message, whether it has Notes/Album, and a one-line "what it was about"
   (open it if needed: click the row, then `line_key key=Up` moves to the next one up).
3. Sort into buckets by the last message date:
   - **A** — empty history, or only "… left the group" lines
   - **B** — older than the threshold
   - **C** — newer than the threshold (normally keep)
4. Suggest leave / keep for each, based on **who and what** — not on the name or the size alone:
   - leave: finished events, one-off gatherings, finished projects, people the owner no longer deals with
   - keep: family, current work, current friends, groups with Notes/Album
5. Do not open memo chats (they can contain passwords) and skip "no members" leftovers (there is nothing to leave).

Opening a chat marks its messages as read. Tell the owner before the listing starts.

## 2. The owner checks

Write the list to a local file the owner can edit — for example `~/line-group-review.md` (never inside a Git
repository, never uploaded):

```markdown
# LINE groups to leave — rules: no message for 365 days; keep family / work / Notes
- [x] A | Spring BBQ 2023 | 12 members | last: 2023-05-20 | BBQ planning, finished
- [ ] B | Neighborhood kids soccer | 30 | last: 2024-01-10 | owner's child was a member — ask
- [ ] C | Project X core | 5 | last: 2026-10-30 | current work — keep
```

`[x]` = the AI suggests leaving. The owner changes the boxes (a whole bucket at once or one by one) and says
"done". Read the file back and repeat the final count to the owner: "Leaving 84 groups, keeping 37. Go?"
Wait for a clear yes. Anything not ticked is not touched.

If your AI client can show an interactive page (a web artifact, a form), you can offer the same checklist there,
with "leave all in bucket / keep all in bucket / follow the suggestion" buttons per bucket. The file is the
fallback that works everywhere.

## 3. Save first when needed

If a ticked group has Notes or an Album the owner wants, save it before leaving:
`line_read_chat_text expected_chat=<name> save_log=true` for the messages, and the Notes window steps in
`line-desktop/references/windows-helpers.md`. Save outside any Git repository.

## 4. Leave, one at a time

For each ticked group, from the bottom of the "Groups" tab:

1. Open it. `line_screenshot`. **The name at the top right must match the checklist line exactly.** If not, skip.
2. `line_click point=CHAT_MENU_BUTTON` (⋮ at the top right).
3. In the menu (`line_screenshot window=popup`), click the last item:
   - named group: "グループ退会" (Leave group)
   - unnamed multi-person chat: "このトークから退出" (Leave chat)
   with `action=menu`. **The item right above it is "Report" — read the image, do not guess.**
4. A confirmation appears ("このグループを退会すると…" / "このトークルームから退出すると…"). Read it in the
   screenshot, then click the green "退会" / "退出" button with
   `action=leave expected_target="<exact group name>"`.
5. Check that the group disappeared from the list. Add a line to the log file next to the checklist:
   `2026-11-15 21:03 | left | Spring BBQ 2023`.

Coordinates measured on Windows (767×722 window): ⋮ (746, 81); the "退出" confirm button in the main window about
(344, 397) (the named point `LEAVE_CONFIRM_POINT`; a click within 60 px of it needs `action=leave` and `expected_target`); the "退会" button in the dialog about (105, 293). Always confirm with the screenshot.

The `manage` limits (default 6 per minute, 150 per day) pace this. If a limit is hit, stop and report; resume
later. If the owner touches the mouse or keyboard during a step, the tool stops; continue only when they say so.

## 5. Report

"Left N groups, kept M, could not leave K (why)." Clear the search box and switch back to the "All" tab
(`line_click point=ALL_TAB`).

## Notes

- Leaving does not delete the group for others. To close a group entirely you would remove every member first
  and leave last — that affects other people; do it only on explicit request, with `action=remove_member`.
- On a Mac, measure the Groups tab, ⋮, the leave item and the confirm button with the owner on the first 2–3
  groups (`line-desktop-recalibrate`), then continue.
- Measured on Windows by leaving about 270 groups in one cleanup.
