#!/usr/bin/env python3
"""codex-cu: an MCP stdio server that lets Claude Code drive Codex's Computer Use tools.

Codex's computer-use server refuses calls unless its parent process is signed by
OpenAI ("Sender process is not authenticated"). The signed Codex CLI bundled in
ChatGPT.app passes that check, and its `app-server` mode exposes
`mcpServer/tool/call`, which runs one tool with no model turn. So:

    Claude Code -> this bridge -> codex app-server (signed) -> SkyComputerUseClient mcp

Per-app approval prompts arrive as `mcpServer/elicitation/request`. The bridge
asks the user with its own macOS dialog, because Claude Code declines MCP
elicitations without showing them. It never answers "always"; approvals last
for this bridge process only.

Stdlib only. Log: ~/Library/Logs/codex-cu-bridge.log
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib

LOG_PATH = os.path.expanduser("~/Library/Logs/codex-cu-bridge.log")
IDLE_SECS = int(os.environ.get("CODEX_CU_IDLE_SECS", "600"))
CALL_TIMEOUT = int(os.environ.get("CODEX_CU_CALL_TIMEOUT", "180"))
APPROVAL_TIMEOUT = 120  # must stay below CALL_TIMEOUT so a slow answer cannot outlive the call
CLI_CANDIDATES = [
    os.environ.get("CODEX_CU_CLI"),
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
    "/Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
]
CU_DIR = os.path.expanduser(os.environ.get("CODEX_CU_APP_DIR", "~/.codex/computer-use"))
CU_BIN = os.path.join(
    CU_DIR, "Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient"
)
SERVER = "computer-use"
INSTRUCTIONS = (
    "Operate native Mac apps (Calculator, Notes, Finder, System Settings, ...) through Codex Computer Use. "
    "Call get_app_state for an app before acting on it each turn: it returns a screenshot and an accessibility "
    "tree whose element indices click/set_value/scroll accept. Indices are valid only for the latest state: "
    "every action returns a fresh tree and elements can renumber, so never batch several clicks from one "
    "reading. Prefer type_text/press_key for keyboard-driven input such as numbers and shortcuts. Work runs in the background without moving the "
    "user's mouse. The first use of each app shows the user a macOS approval dialog; "
    "if they decline, do not work around it. For web pages, use a browser tool instead."
)


def log(*parts):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")
    except OSError:
        pass


def locate_cli():
    for path in CLI_CANDIDATES:
        if path and os.access(path, os.X_OK):
            return path
    raise RuntimeError(
        "codex-cu: no signed Codex CLI found. Looked in: "
        + ", ".join(p for p in CLI_CANDIDATES if p)
        + ". Install or update the ChatGPT/Codex desktop app, or set CODEX_CU_CLI."
    )


def locate_cu_bin():
    if not os.access(CU_BIN, os.X_OK):
        raise RuntimeError(
            f"codex-cu: Computer Use client not found at {CU_BIN}. Enable Computer Use in the Codex app once, "
            "or set CODEX_CU_APP_DIR."
        )
    return CU_BIN


class Rpc:
    """Line-delimited JSON-RPC over a pair of streams, with request/response matching."""

    def __init__(self, write_stream, name):
        self.out = write_stream
        self.name = name
        self.lock = threading.Lock()
        self.next_id = 0
        self.pending = {}

    def write(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def request(self, method, params=None, timeout=None):
        with self.lock:
            self.next_id += 1
            rid = f"b{self.next_id}" if self.name == "client" else self.next_id
        slot = {"event": threading.Event(), "msg": None}
        self.pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.write(msg)
        if not slot["event"].wait(timeout):
            self.pending.pop(rid, None)
            raise TimeoutError(f"{self.name}: no response to {method} within {timeout}s")
        return slot["msg"]

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.write(msg)

    def respond(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self.write(msg)

    def resolve(self, msg):
        slot = self.pending.pop(msg.get("id"), None)
        if slot:
            slot["msg"] = msg
            slot["event"].set()

    def fail_all(self, reason):
        for rid in list(self.pending):
            slot = self.pending.pop(rid, None)
            if slot:
                slot["msg"] = {"error": {"code": -32000, "message": reason}}
                slot["event"].set()


class Bridge:
    def __init__(self):
        self.client = Rpc(sys.stdout.buffer, "client")
        self.dialog_lock = threading.Lock()
        self.allowed = set()  # lower-cased app names approved this session
        self.last_denied = None  # app refused for lack of approval during the current call
        self.proc = None
        self.codex = None
        self.thread_id = None
        self.start_lock = threading.Lock()
        self.call_lock = threading.Lock()
        self.last_used = time.time()
        self.tools_cache = None
        threading.Thread(target=self._idle_watchdog, daemon=True).start()

    # ---- Codex side -------------------------------------------------------

    def _thread_config(self):
        """Enable only computer-use; switch off every other server and plugin Codex would start."""
        try:
            with open(os.path.expanduser("~/.codex/config.toml"), "rb") as f:
                cfg = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            cfg = {}
        plugins = set(cfg.get("plugins", {})) | {
            "unified-computer-use@openai-bundled",
            "computer-history@openai-bundled",
            "codex-app-tools@openai-bundled",
            "browser@openai-bundled",
            "chrome@openai-bundled",
        }
        servers = {name: {"enabled": False} for name in cfg.get("mcp_servers", {})}
        servers[SERVER] = {"enabled": True, "command": locate_cu_bin(), "args": ["mcp"], "cwd": CU_DIR}
        return {"mcp_servers": servers, "plugins": {name: {"enabled": False} for name in plugins}}

    def _ensure_codex(self):
        with self.start_lock:
            if self.proc and self.proc.poll() is None and self.thread_id:
                return
            cli = locate_cli()
            stderr = open(LOG_PATH, "a")
            self.proc = subprocess.Popen(
                [cli, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr
            )
            self.codex = Rpc(self.proc.stdin, "codex")
            threading.Thread(target=self._codex_reader, args=(self.proc, self.codex), daemon=True).start()
            resp = self.codex.request("initialize", {"clientInfo": {"name": "codex-cu-bridge", "version": "0.1.0"}}, 60)
            if "error" in resp:
                raise RuntimeError(f"codex app-server initialize failed: {resp['error']}")
            self.codex.notify("initialized")
            resp = self.codex.request(
                "thread/start", {"ephemeral": True, "config": self._thread_config(), "cwd": CU_DIR}, 120
            )
            if "error" in resp:
                raise RuntimeError(f"codex thread/start failed: {resp['error']}")
            self.thread_id = resp["result"]["thread"]["id"]
            log("codex started pid", self.proc.pid, "thread", self.thread_id)

    def _stop_codex(self, why):
        with self.start_lock:
            proc, self.proc, self.thread_id = self.proc, None, None
            if not proc:
                return
            try:
                proc.stdin.close()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            if self.codex:
                self.codex.fail_all("codex app-server stopped")
            log("codex stopped:", why)

    def _idle_watchdog(self):
        while True:
            time.sleep(30)
            if self.proc and not self.call_lock.locked() and time.time() - self.last_used > IDLE_SECS:
                self._stop_codex(f"idle {IDLE_SECS}s")

    def _codex_reader(self, proc, rpc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            method = msg.get("method")
            if method is None:
                rpc.resolve(msg)
            elif "id" in msg:
                threading.Thread(target=self._codex_server_request, args=(rpc, msg), daemon=True).start()
            elif method == "thread/status/changed" and "waitingOnApproval" in json.dumps(msg.get("params")):
                # app-server holds a pending server request until the client sends something; nudge it.
                threading.Thread(target=self._nudge, args=(rpc,), daemon=True).start()
        rpc.fail_all("codex app-server exited")

    def _nudge(self, rpc):
        try:
            rpc.request("thread/loaded/list", {}, 10)
        except Exception:
            pass

    def _codex_server_request(self, rpc, msg):
        method, params = msg["method"], msg.get("params") or {}
        if method != "mcpServer/elicitation/request":
            log("refusing codex server request", method)
            rpc.respond(msg["id"], error={"code": -32601, "message": "codex-cu bridge does not handle " + method})
            return
        text = params.get("message", "")
        m = re.search(r"use (.+?)\?$", text)
        app = m.group(1) if m else text
        ok = self._approve(app)
        log("approval", repr(app), "->", "accept" if ok else "decline")
        # No _meta.persist: the approval lives only as long as this session.
        rpc.respond(msg["id"], result={"action": "accept", "content": {}} if ok else {"action": "decline"})

    def _approve(self, app):
        """Ask the user with a native macOS dialog. Claude Code advertises elicitation but, in both the desktop
        app and `claude -p`, declines it instantly without showing anything (measured 2026-10-05)."""
        if app.lower() in self.allowed:
            return True
        with self.dialog_lock:
            if app.lower() in self.allowed:
                return True
            safe = app.replace("\\", "").replace('"', "")
            script = (
                f'display dialog "Let Claude control {safe} through Codex Computer Use?" & return & return & '
                f'"Applies until this Claude session ends." with title "codex-cu" '
                f'buttons {{"Don\'t Allow", "Allow for this session"}} default button "Allow for this session" '
                f'cancel button "Don\'t Allow" with icon caution giving up after {APPROVAL_TIMEOUT}'
            )
            try:
                out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                                     timeout=APPROVAL_TIMEOUT + 10)
            except subprocess.TimeoutExpired:
                self.last_denied = (app, "no-answer")
                return False
            if "button returned:Allow for this session" in out.stdout and "gave up:true" not in out.stdout:
                self.allowed.add(app.lower())
                self.last_denied = None
                return True
            self.last_denied = (app, "no-answer" if "gave up:true" in out.stdout else "declined")
            return False

    # ---- Claude side ------------------------------------------------------

    def list_tools(self):
        if self.tools_cache is None:
            # Listing tools needs no authentication, so ask the client binary directly.
            proc = subprocess.Popen(
                [locate_cu_bin(), "mcp"], cwd=CU_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            rpc = Rpc(proc.stdin, "cu-list")
            reader = threading.Thread(target=lambda: [rpc.resolve(json.loads(l)) for l in proc.stdout], daemon=True)
            reader.start()
            try:
                rpc.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "codex-cu-bridge", "version": "0.1.0"}}, 30)
                rpc.notify("notifications/initialized")
                tools = rpc.request("tools/list", {}, 30)["result"]["tools"]
            finally:
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self.tools_cache = [
                {k: t[k] for k in ("name", "description", "inputSchema", "annotations") if k in t} for t in tools
            ]
        return self.tools_cache

    def call_tool(self, name, args):
        with self.call_lock:
            self.last_used = time.time()
            self.last_denied = None
            self._ensure_codex()
            resp = self.codex.request(
                "mcpServer/tool/call",
                {"threadId": self.thread_id, "server": SERVER, "tool": name, "arguments": args or {}},
                CALL_TIMEOUT,
            )
            self.last_used = time.time()
        if "error" in resp:
            return {"content": [{"type": "text", "text": f"codex-cu: {resp['error'].get('message')}"}], "isError": True}
        result = resp["result"]
        out = {"content": result.get("content", [])}
        if result.get("isError"):
            out["isError"] = True
            if self.last_denied:
                app, why = self.last_denied
                if why == "declined":
                    hint = f"The user clicked Don't Allow for {app} in the macOS dialog. Do not retry unless they ask."
                else:
                    hint = (f"The macOS approval dialog for {app} got no answer within {APPROVAL_TIMEOUT}s. Tell the "
                            f"user; retrying will show the dialog again.")
                out["content"].append({"type": "text", "text": hint})
        if "structuredContent" in result:
            out["structuredContent"] = result["structuredContent"]
        return out

    def handle(self, msg):
        method, rid = msg.get("method"), msg.get("id")
        try:
            if method == "initialize":
                params = msg.get("params") or {}
                log("client", params.get("clientInfo"))
                self.client.respond(rid, {
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "codex-cu", "version": "0.1.0"},
                    "instructions": INSTRUCTIONS,
                })
            elif method == "tools/list":
                self.client.respond(rid, {"tools": self.list_tools()})
            elif method == "tools/call":
                params = msg.get("params") or {}
                self.client.respond(rid, self.call_tool(params.get("name"), params.get("arguments")))
            elif method == "ping":
                self.client.respond(rid, {})
            elif rid is not None:
                self.client.respond(rid, error={"code": -32601, "message": f"unknown method {method}"})
        except Exception as e:  # report every failure to the client instead of dying
            log("error handling", method, repr(e))
            if rid is not None:
                if method == "tools/call":
                    self.client.respond(rid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
                else:
                    self.client.respond(rid, error={"code": -32000, "message": str(e)})

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: (self._stop_codex("SIGTERM"), os._exit(0)))
        for line in sys.stdin.buffer:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "method" not in msg:
                continue
            if msg["method"] == "tools/call":
                threading.Thread(target=self.handle, args=(msg,), daemon=True).start()
            else:
                self.handle(msg)
        self._stop_codex("client closed stdin")


if __name__ == "__main__":
    Bridge().run()
