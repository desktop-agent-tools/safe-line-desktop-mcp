# Contributing / 参加のしかた

Thank you for helping. 日本語でも英語でも歓迎します。

## Good first contributions

- **Field reports** (issues): OS, LINE version, window size and display scaling, AI client, what worked and what did not.
  Please **remove names, chat names, message text and screenshots of other people's messages** before posting.
- **Layout updates**: run the `line-desktop-recalibrate` skill and send the changed points in a PR (say which LINE version).
- **New skills**: a folder under `skills/<name>/SKILL.md`, written for any MCP-capable agent (no product-specific steps).
- **Docs and translations**.

## Rules that keep this project safe

- Personal use on your own PC and account only. No bulk messaging, advertising, scraping or other uses LINE's
  Terms of Use forbid. PRs that weaken the guards (send limits, name checks, management confirmation, the
  local-only action log) need a clear reason and a test.
- Never commit real personal data: names, chat names, phone numbers, message text, tokens, screenshots.
- The server stays a single Python file using only the standard library.
- Run `python -m unittest discover -s tests` before sending a PR. Add a test for new guards.

## Security

Report vulnerabilities privately through GitHub's "Report a vulnerability" (see SECURITY.md), not in public issues.

## License

By contributing you agree that your contribution is licensed under the Apache License 2.0 (see LICENSE).

---

### 日本語の要点

- 「動いた／動かなかった」の報告（OS・LINE の版・窓の大きさ・使った AI）が一番助かります。人の名前・トーク名・本文・画面写真は消してから送ってください。
- LINE の更新でずれた座標は、測り直した値を PR で。新しいスキルも歓迎です。
- 自分の PC・自分のアカウントでの個人利用に限ります。一斉送信・宣伝など LINE の規約に反する使い方の変更は受け付けません。
- 脆弱性は公開の Issue ではなく、非公開で（SECURITY.md）。
