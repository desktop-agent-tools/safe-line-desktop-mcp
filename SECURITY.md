# Security Policy / セキュリティ

## Reporting

Please report vulnerabilities privately via **Security → Report a vulnerability** on GitHub, not in public issues.
We will reply, fix what we can, and agree on disclosure with the reporter.

脆弱性は公開の Issue に書かず、GitHub の「Security」→「Report a vulnerability」から非公開で送ってください。

## Threat model

In scope:

- Anyone other than the local user being able to read or send LINE messages through this server
  (network exposure of HTTP mode, missing/weak token checks, browser-originated requests, DNS rebinding).
- Ways to bypass the target check (`expected_chat` / `expected_target`), the send/manage limits, or the
  "stop when the user takes over" behavior through the MCP interface.
- Chat text or logs being written somewhere other than the documented local folders.
- Message text being stored in the action log while `include_body` is off.

Out of scope:

- Malicious software already running as the same user (it can drive LINE directly without this server).
- An AI agent that the user configured and instructed to do something harmful.
- LINE's own behavior, account restrictions applied by LY Corporation, and versions other than the latest release.

## Design notes

- stdio by default. HTTP binds to loopback only, requires `LINE_MCP_TOKEN` (24+ characters, not trivially
  repetitive), compares it in constant time, checks Host, refuses any request with an Origin header, drops silent connections, caps concurrent connections, limits bodies to 1 MB, and handles one
  request at a time. Some other LINE desktop MCP servers have exposed unauthenticated HTTP on all
  interfaces; this server is designed not to allow that configuration.
- No shell is invoked with user-controlled strings; message text goes to helper processes through stdin or
  arguments, never through a shell command line.
