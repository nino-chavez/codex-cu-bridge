# codex-cu-bridge

Lets Claude Code drive native Mac apps with Codex's Computer Use tools. Claude decides what to do; Codex's tools click, type, and return a screenshot plus an accessibility tree. Work runs in the background without moving your mouse, and it uses your existing ChatGPT/Codex subscription.

Inspired by Mark Kashef's [These Mods Take Claude Code to Another Level](https://www.youtube.com/watch?v=UcvD53jqHRU) (2026-10-05). This is an independent build, not his code.

## How it works

```
Claude Code
└─ codex_cu_bridge.py              MCP stdio server, registered at user scope
   └─ codex app-server             signed Codex CLI inside ChatGPT.app
      └─ SkyComputerUseClient mcp  Codex's computer-use server
```

- Codex's computer-use server lists its tools for any client but refuses calls with `Sender process is not authenticated`. It checks that its parent process is signed by OpenAI.
- The signed Codex CLI passes that check. Its `app-server` mode has `mcpServer/tool/call`, which runs one tool with no Codex model turn and no model tokens.
- The bridge opens one ephemeral app-server thread with only `computer-use` enabled. Every other Codex MCP server and plugin is switched off for that thread.
- Codex starts on the first tool call, not when Claude Code connects. It stops after 10 idle minutes or when the Claude session ends.

## Tools

The ten Codex tools pass through unchanged: `list_apps`, `get_app_state`, `click`, `perform_secondary_action`, `set_value`, `select_text`, `scroll`, `drag`, `press_key`, `type_text`. Call `get_app_state` for an app before acting on it each turn. Element indices are valid only for the latest state; each action returns a fresh tree, and elements can renumber after a click. A batch of clicks from one reading hit the wrong Calculator buttons in the first live test, so the server instructions say to re-read between clicks and to prefer `type_text` / `press_key`.

## Approvals

- The first use of each app in a session opens a macOS dialog from the bridge: **Allow for this session** or **Don't Allow**. It gives up after 120 seconds.
- Approval lasts until the Claude session ends. The bridge never answers "always". Grant permanent access in the Codex app if you want it.
- The bridge asks with its own dialog because Claude Code advertises MCP elicitation but declines it without showing anything, in both the desktop app and `claude -p` (measured 2026-10-05). A native dialog also cannot be answered by text on an app's screen, so the model has no way to approve an app for itself.

## Install

    claude mcp add -s user codex-cu -- python3 ~/Workspace/dev/tools/codex-cu-bridge/codex_cu_bridge.py

Requires macOS, the ChatGPT desktop app with Codex, and Computer Use enabled once in Codex. Python 3.11+ (stdlib only). New Claude sessions pick the tools up; there is no command to run.

## Configuration

- `CODEX_CU_CLI` — path to a signed Codex CLI, if not in ChatGPT.app.
- `CODEX_CU_APP_DIR` — Computer Use install dir. Default `~/.codex/computer-use`.
- `CODEX_CU_IDLE_SECS` — idle seconds before Codex stops. Default 600.
- `CODEX_CU_CALL_TIMEOUT` — seconds per tool call. Default 180.

Log: `~/Library/Logs/codex-cu-bridge.log`.

## Known quirks

- **Stalled approval prompts.** app-server holds a pending approval request until the client sends something. The bridge sends a cheap `thread/loaded/list` when the thread reports `waitingOnApproval`. Without it, the first call waited 120 seconds.
- **No turn boundary.** Codex runs `SkyComputerUseClient turn-ended` after each of its own turns. The bridge never does. Repeated `get_app_state` calls work without it; whether the "using your computer" indicator clears on its own is unverified.
- **App updates.** Paths are resolved at each start and fail with a message naming what is missing. An update that renames the app-server methods breaks the bridge loudly, not silently.

## License

MIT — see [LICENSE](LICENSE).
