# Connecting your AI agent

The server is one Python file with no dependencies (Python 3.10+). It speaks MCP over **stdio** (default) or
**HTTP on loopback with a token**. Replace `/path/to/safe-line-desktop-mcp` with where you cloned the repository.
On Windows use `python` (or `py`); on macOS use `python3`.

## Claude Code

As a plugin (installs the MCP server and the skills together):

```bash
claude plugin marketplace add desktop-agent-tools/safe-line-desktop-mcp
claude plugin install safe-line-desktop
```

Or only the MCP server:

```bash
claude mcp add line-desktop -- python /path/to/safe-line-desktop-mcp/server/line_desktop_mcp.py
```

Skills: copy `skills/*` to `~/.claude/skills/` (the plugin does this for you).

## Codex CLI / Codex app

`~/.codex/config.toml`:

```toml
[mcp_servers.line_desktop]
command = "python"
args = ["/path/to/safe-line-desktop-mcp/server/line_desktop_mcp.py"]
```

Skills: copy `skills/*` to `~/.codex/skills/`, or point your `AGENTS.md` at `AGENTS.md` in this repository.

## Claude Desktop, Cursor, Windsurf and other MCP clients

Most clients take this JSON shape (`claude_desktop_config.json`, `.cursor/mcp.json`, ...). Check your client's
documentation for the file name and confirm the server shows up after registering:

```json
{
  "mcpServers": {
    "line-desktop": {
      "command": "python",
      "args": ["/path/to/safe-line-desktop-mcp/server/line_desktop_mcp.py"]
    }
  }
}
```

## VS Code

VS Code uses its own format: `.vscode/mcp.json` in the workspace (or the user-level MCP file) with a top-level
`servers` object and an explicit `type`:

```json
{
  "servers": {
    "line-desktop": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/safe-line-desktop-mcp/server/line_desktop_mcp.py"]
    }
  }
}
```

Clients without a skills feature: paste `AGENTS.md` (or the skill you need) into the client's rules / custom
instructions. The skills are plain Markdown and do not depend on any one AI product.

## Always-on personal agents (HTTP)

Agents that run continuously on the same PC (a personal assistant that checks LINE on a schedule, an agent
framework with an MCP client, ...) can use HTTP instead of spawning the server each time.

```bash
# PowerShell: $env:LINE_MCP_TOKEN = "<random, at least 24 characters>"
export LINE_MCP_TOKEN="<random, at least 24 characters>"
python server/line_desktop_mcp.py --http --port 3000
```

- Endpoint: `POST http://127.0.0.1:3000/mcp` with `Authorization: Bearer <token>`, JSON-RPC body.
- It only listens on loopback (127.0.0.1 / localhost / ::1). Other machines cannot reach it, by design.
- Any request with an `Origin` header is refused (browsers send one, MCP clients and command-line tools do not), and
  the `Host` header must be a loopback name. Silent connections are dropped after 15 seconds, at most 8 connections
  are served at once, and screen actions run one at a time.
- Start it only while you need it. Anyone who has the token can send messages as you.

Scheduled agents: run unread checks with read-only steps (`line_scan_list`, `line_screenshot`) and keep sending
for when you can confirm, or set tight `limits` in the settings file.

## Settings file

`config/config.example.json` shows every option. Copy it to:

- Windows: `%LOCALAPPDATA%\line-desktop-mcp\config.json`
- macOS: `~/Library/Application Support/line-desktop-mcp/config.json`

or point `LINE_MCP_CONFIG` at any file. Environment variables override the file:

| Variable | Meaning |
|---|---|
| `LINE_SEND_MAX_PER_MINUTE` / `LINE_SEND_MAX_PER_DAY` | send limits (0 = no limit) |
| `LINE_MANAGE_MAX_PER_MINUTE` / `LINE_MANAGE_MAX_PER_DAY` | limits for leave / invite / remove / create ... |
| `LINE_ACTION_LOG=0` | turn the local action log off |
| `LINE_ACTION_LOG_BODY=1` | also store message text in the action log |
| `LINE_WAIT_FOR_IDLE_SECONDS` | how long to wait for a pause in your typing before a foreground step |
| `LINE_RETURN_FOCUS=0` | do not hand the foreground back after a foreground step |
| `LINE_BACKGROUND=0` | never use background operation (Windows) |
| `LINE_SEND_KEY` | send key, e.g. `Return` if your LINE sends on Enter |
| `LINE_MCP_HOME` | folder for settings, action log and chat logs. It must not be inside a Git repository: the server refuses to write there |
