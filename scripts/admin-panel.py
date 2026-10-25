#!/usr/bin/env python3
"""Minecraft admin panel for this stack.

A single standard-library-only process that:

  * serves a small web UI (no external assets) on a loopback-published port
  * lists the online players and executes in-game commands through RCON
  * mirrors the server's console - player chat plus system lines - by
    following <data>/logs/latest.log, which the stack mounts read-only

It never wakes the server. When the server container is stopped, the RCON
dial is simply refused and the panel reports the server as offline; the UI
then shows the last state it knows. The panel does not talk to the router or
the Docker daemon at all, so nothing it does can start the server.

Environment:
  PANEL_PORT     port to listen on inside the container (default 8080)
  PANEL_TOKEN    optional shared token; when set, every request must carry
                 it in the X-Panel-Token header (the page asks once and
                 keeps it in the browser session)
  RCON_HOST      server hostname on the compose network (default minecraft-server)
  RCON_PORT      RCON port (default 25575)
  RCON_PASSWORD  RCON password (required)
  DATA_PATH      the server's data directory (default /data); the panel
                 follows $DATA_PATH/logs/latest.log read-only
"""

import hmac
import json
import os
import re
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "")
RCON_HOST = os.environ.get("RCON_HOST", "minecraft-server")
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")
LOG_PATH = os.path.join(os.environ.get("DATA_PATH", "/data"), "logs", "latest.log")

CHAT_BUFFER_MAX = 1000       # console lines kept in memory for the feed
PLAYER_POLL_SECONDS = 10.0   # how often "list" is asked of a live server
TAIL_POLL_SECONDS = 1.5      # how often latest.log is polled
SEED_BYTES = 256 * 1024      # how much of an existing log to read at start
COMMAND_MAX_LENGTH = 1000    # RCON commands are short; keep them short
CONNECT_TIMEOUT = 10         # seconds, dialing the server container
COMMAND_TIMEOUT = 60         # seconds, waiting for a command's response


# ─────────────────────────────────────────────────────────────────────────────
# RCON
# ─────────────────────────────────────────────────────────────────────────────

class RconError(Exception):
    """The RCON session failed in a way the worker should recover from."""


class Rcon:
    """A minimal RCON client: the little-endian packet protocol Paper ships.

    One session per connection; login (type 3) then commands (type 2).
    Responses arrive as type 2 with the request's id, or id -1 on error.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sock = None
        self._next_id = 1

    def _alloc_id(self):
        self._next_id = self._next_id % 0x7FFFFFFF + 1
        return self._next_id

    @staticmethod
    def _recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    @staticmethod
    def _pack(pkt_id, pkt_type, payload):
        data = payload.encode("utf-8")
        # The size field counts the bytes after itself: id + type + payload
        # + the two trailing nulls.
        body = struct.pack("<ii", pkt_id, pkt_type) + data + b"\x00\x00"
        return struct.pack("<i", len(body)) + body

    @staticmethod
    def _unpack(body):
        pkt_id, pkt_type = struct.unpack("<ii", body[:8])
        payload = body[8:]
        # Command and auth responses carry two trailing nulls; split packets
        # (huge responses) do not. Our commands produce small responses, but
        # strip only when present so neither shape breaks us.
        if pkt_type == 2 and len(payload) >= 2 and payload.endswith(b"\x00\x00"):
            payload = payload[:-2]
        return pkt_type, pkt_id, payload.decode("utf-8", "replace")

    def connect(self):
        sock = socket.create_connection((RCON_HOST, RCON_PORT), timeout=CONNECT_TIMEOUT)
        sock.settimeout(CONNECT_TIMEOUT)
        pkt_id = self._alloc_id()
        sock.sendall(self._pack(pkt_id, 3, RCON_PASSWORD))
        size = struct.unpack("<i", self._recv_exact(sock, 4))[0]
        _, rid, _ = self._unpack(self._recv_exact(sock, size))
        if rid == -1:
            sock.close()
            raise RconError("RCON login rejected (bad password?)")
        self._sock = sock

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def command(self, line):
        """Send one command and return the server's response text."""
        with self._lock:
            if self._sock is None:
                raise RconError("not connected")
            self._sock.settimeout(COMMAND_TIMEOUT)
            pkt_id = self._alloc_id()
            self._sock.sendall(self._pack(pkt_id, 2, line))
            size = struct.unpack("<i", self._recv_exact(self._sock, 4))[0]
            _, rid, payload = self._unpack(self._recv_exact(self._sock, size))
            if rid == -1:
                raise RconError(payload or "command rejected by the server")
            return payload


class ServerState:
    """What the RCON worker currently knows about the server."""

    def __init__(self):
        self._lock = threading.Lock()
        self.online = False
        self.players = []
        self.max_players = 0
        self.last_error = ""
        self.updated = 0.0

    def snapshot(self):
        with self._lock:
            return {
                "online": self.online,
                "players": list(self.players),
                "player_count": len(self.players),
                "max_players": self.max_players,
                "last_error": self.last_error,
                "updated": self.updated,
            }

    def set_offline(self, error):
        with self._lock:
            self.online = False
            self.players = []
            self.last_error = error
            self.updated = time.time()

    def set_players(self, names, max_players):
        with self._lock:
            self.online = True
            self.players = names
            self.max_players = max_players
            self.last_error = ""
            self.updated = time.time()


LIST_RE = re.compile(
    r"There are (\d+) of (?:a )?max of? (\d+) players online:(.*)"
)


def parse_list_response(text):
    """Parse Paper's list response.

    Examples:
      There are 0 of 20 max players online:
      There are 0 of a max of 20 players online:
    """
    m = LIST_RE.search(text or "")
    if not m:
        return None
    names = [n.strip() for n in m.group(3).split(",") if n.strip()]
    return names, int(m.group(2))


class RconWorker(threading.Thread):
    """Keeps one RCON session alive and polls the player list.

    Dialing RCON while the server container is stopped is a refused
    connection - it cannot wake anything. The backoff doubles to 30 s so an
    asleep server costs a handful of refused dials a minute, nothing more.
    """

    def __init__(self, state):
        super().__init__(daemon=True, name="rcon")
        self.state = state
        self.rcon = Rcon()

    def _refresh(self):
        parsed = parse_list_response(self.rcon.command("list"))
        if parsed is not None:
            self.state.set_players(*parsed)
        # Online but the list did not parse: keep the last known list.

    def run(self):
        backoff = 5.0
        while True:
            try:
                self.rcon.connect()
                backoff = 5.0
                self._refresh()
                last = time.time()
                while True:
                    if time.time() - last >= PLAYER_POLL_SECONDS:
                        self._refresh()
                        last = time.time()
                    time.sleep(1.0)
            except Exception as exc:
                self.rcon.close()
                self.state.set_offline(str(exc) or exc.__class__.__name__)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


# ─────────────────────────────────────────────────────────────────────────────
# Console feed (latest.log tail)
# ─────────────────────────────────────────────────────────────────────────────

class ConsoleFeed(threading.Thread):
    """Follows latest.log and keeps the last N parsed lines in memory.

    Paper writes one line per event:
        [15:20:51] [Server thread/INFO]: System chat: kapelape joined the game
    Newer revisions use "[15:20:51] [INFO] [Server]: ..."; both are handled.
    Chat-channel messages carry the "System chat: " prefix; player chat is
    "System chat: Name: text" (or plain "Name: text" in older logs).
    Everything else - joins, deaths, plugin output, stack traces - is system.
    """

    LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] \[(?P<logger>[^\]]+)\]: (?P<msg>.*)$")
    MODERN_RE = re.compile(
        r"^\[(\d{2}:\d{2}:\d{2})\] \[(?P<level>[A-Z]+)\] \[(?P<logger>[^\]]+)\]: (?P<msg>.*)$"
    )
    LEVEL_RE = re.compile(r"^(?P<name>.*)/(?P<level>[A-Z]+)$")
    CHAT_RE = re.compile(r"^([A-Za-z0-9_]{1,16}): (.*)$")
    ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    SYSTEM_CHAT_PREFIX = "System chat: "

    def __init__(self):
        super().__init__(daemon=True, name="tail")
        self._entries = []
        self._seq = 0
        self._lock = threading.Lock()

    def _reset(self):
        """A new session started: drop the previous session's lines."""
        with self._lock:
            self._entries = []
            self._seq = 0

    def _add(self, entry):
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._entries.append(entry)
            if len(self._entries) > CHAT_BUFFER_MAX:
                del self._entries[: len(self._entries) - CHAT_BUFFER_MAX]

    @staticmethod
    def _classify(msg):
        """Return (kind, player, text) for a logger message."""
        text = msg
        if text.startswith(ConsoleFeed.SYSTEM_CHAT_PREFIX):
            text = text[len(ConsoleFeed.SYSTEM_CHAT_PREFIX):]
        m = ConsoleFeed.CHAT_RE.match(text)
        if m:
            return "chat", m.group(1), m.group(2)
        return "system", None, msg

    def _parse_line(self, line):
        line = self.ANSI_RE.sub("", line.rstrip("\n"))
        if not line.strip():
            return None
        m = self.LINE_RE.match(line) or self.MODERN_RE.match(line)
        if not m:
            return None  # continuation line (stack trace, multi-line output)
        groups = m.groupdict()
        logger = groups["logger"]
        level = groups.get("level") or ""
        lm = self.LEVEL_RE.match(logger)
        if lm:
            logger, level = lm.group("name"), lm.group("level")
        kind, player, text = self._classify(m.group("msg"))
        return {"ts": m.group(1), "logger": logger, "level": level,
                "kind": kind, "player": player, "text": text}

    def _ingest(self, line):
        parsed = self._parse_line(line)
        if parsed is None:
            with self._lock:
                # Attach continuation lines to the previous entry; drop them
                # when the buffer is empty (seed cut into a mid-line).
                if self._entries:
                    self._entries[-1]["text"] += "\n" + line
            return
        self._add(parsed)

    def entries_after(self, after):
        """(entries with seq > after, highest seq issued so far)."""
        with self._lock:
            return [e for e in self._entries if e["seq"] > after], self._seq

    def run(self):
        pos = None      # None until the first stat of the file
        ident = None    # (st_dev, st_ino) of the file we are following
        pending = ""
        while True:
            try:
                st = os.stat(LOG_PATH)
            except OSError:
                time.sleep(TAIL_POLL_SECONDS)
                continue
            ident_now = (st.st_dev, st.st_ino)
            if pos is None:
                # First look: seed from the tail of whatever is already there,
                # so the UI opens with the recent history of this session.
                pos = max(0, st.st_size - SEED_BYTES)
                ident = ident_now
            elif ident_now != ident or st.st_size < pos:
                # Rotated, or truncated - Paper rewrites latest.log on each
                # start. The old lines belong to the previous session.
                self._reset()
                pos = 0
                ident = ident_now
                pending = ""
            try:
                with open(LOG_PATH, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
            except OSError:
                time.sleep(TAIL_POLL_SECONDS)
                continue
            if chunk:
                pos += len(chunk)
                pending += chunk.decode("utf-8", "replace")
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    self._ingest(line)
            time.sleep(TAIL_POLL_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Minecraft Admin</title>
<style>
:root {
  --bg: #0f1115; --card: #171a21; --border: #2a2f3a; --text: #d7dbe4;
  --dim: #8b93a3; --faint: #566072; --accent: #4f8cff;
  --green: #3fb950; --red: #f85149; --warn: #d29922;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; align-items: center; gap: 12px; padding: 12px 18px;
  border-bottom: 1px solid var(--border); }
header h1 { font-size: 16px; margin: 0; font-weight: 600; }
.pill { font-size: 12px; padding: 3px 10px; border-radius: 999px;
  border: 1px solid var(--border); color: var(--dim); }
.pill.online { color: var(--green); border-color: var(--green); }
.pill.offline { color: var(--red); border-color: var(--red); }
main { display: grid; grid-template-columns: 320px 1fr; gap: 14px;
  padding: 14px 18px; align-items: start; }
@media (max-width: 800px) { main { grid-template-columns: 1fr; } }
.card { background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; }
.card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--dim); margin: 0; padding: 10px 14px;
  border-bottom: 1px solid var(--border); }
.card .body { padding: 10px 14px; }
.players { list-style: none; margin: 0; padding: 0; }
.players li { padding: 3px 0; font-family: ui-monospace, monospace; }
.players .empty { color: var(--dim); font-family: inherit; }
#command { width: 100%; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
  font: 13px ui-monospace, monospace; }
#run { margin-top: 8px; width: 100%; padding: 8px; background: var(--accent);
  color: #fff; border: 0; border-radius: 6px; font-weight: 600; cursor: pointer; }
#run:disabled { opacity: .5; cursor: default; }
#response { margin-top: 10px; white-space: pre-wrap; min-height: 1em;
  font: 12.5px ui-monospace, monospace; color: var(--dim); }
#response.error { color: var(--red); }
.hint { margin-top: 8px; font-size: 11.5px; color: var(--faint); }
#feed { height: 72vh; overflow-y: auto; padding: 8px 14px;
  font: 12.5px/1.5 ui-monospace, monospace; }
.line .ts { color: var(--faint); margin-right: 8px; }
.line.chat .who { color: var(--accent); font-weight: 600; }
.line.system { color: var(--dim); }
.line.level-WARN .ts, .line.level-WARN .who, .line.level-WARN .what { color: var(--warn); }
.line.level-ERROR .ts, .line.level-ERROR .who, .line.level-ERROR .what { color: var(--red); }
.login { position: fixed; inset: 0; background: rgba(10, 12, 16, .9);
  display: flex; align-items: center; justify-content: center; z-index: 10; }
.login .box { background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 22px; width: 340px; }
.login h2 { margin: 0 0 6px; font-size: 15px; }
.login p { margin: 0 0 12px; color: var(--dim); font-size: 13px; }
.login input { width: 100%; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
  font: 13px ui-monospace, monospace; }
.login button { margin-top: 10px; width: 100%; padding: 8px;
  background: var(--accent); color: #fff; border: 0; border-radius: 6px;
  font-weight: 600; cursor: pointer; }
</style>
</head>
<body>
<header>
  <h1>Minecraft Admin</h1>
  <span id="status" class="pill">checking&hellip;</span>
</header>
<main>
  <div>
    <div class="card">
      <h2>Players</h2>
      <div class="body"><ul id="players" class="players"><li class="empty">&hellip;</li></ul></div>
    </div>
    <div class="card" style="margin-top: 14px;">
      <h2>Console command</h2>
      <div class="body">
        <form id="cmdform">
          <input id="command" type="text" autocomplete="off" spellcheck="false"
                 placeholder='e.g. say hello, or op Somebody'>
          <button id="run" type="submit">Run</button>
        </form>
        <div id="response"></div>
        <div class="hint">Executed on the server console through RCON.
        &uarr;/&darr; recall previous commands. The command also lands in the
        console feed below.</div>
      </div>
    </div>
  </div>
  <div class="card">
    <h2>Console &mdash; player chat &amp; system</h2>
    <div id="feed"></div>
  </div>
</main>
__LOGIN__
<script>
const TOKEN_REQUIRED = __TOKEN_REQUIRED__;
let token = TOKEN_REQUIRED ? (sessionStorage.getItem("panelToken") || "") : "";
let lastSeq = -1;
const history = [];
let historyIndex = -1;

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Panel-Token"] = token;
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
  if (!r.ok) {
    let msg = String(r.status);
    try { msg = (await r.json()).error || msg; } catch (e) { /* keep status */ }
    throw new Error(msg);
  }
  return r.json();
}

function showLogin() {
  const box = $("login");
  if (box) box.style.display = "flex";
}

async function pollState() {
  try {
    const s = await api("/api/state");
    const pill = $("status");
    pill.textContent = s.online
      ? "online \u2014 " + s.player_count + "/" + s.max_players + " players"
      : "offline";
    pill.className = "pill " + (s.online ? "online" : "offline");
    pill.title = s.last_error || (s.online ? "" : "server container stopped");
    $("run").disabled = !s.online;
    const ul = $("players");
    ul.innerHTML = "";
    if (!s.players.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = s.online ? "no players online" : "server is offline";
      ul.appendChild(li);
    } else {
      for (const name of s.players) {
        const li = document.createElement("li");
        li.textContent = name;
        ul.appendChild(li);
      }
    }
  } catch (e) {
    // Keep the last known state; the next poll retries.
  }
}

function renderEntry(e) {
  const div = document.createElement("div");
  div.className = "line " + e.kind + (e.level ? " level-" + e.level : "");
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = e.ts;
  div.appendChild(ts);
  if (e.kind === "chat" && e.player) {
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = e.player + ": ";
    div.appendChild(who);
  }
  const what = document.createElement("span");
  what.className = "what";
  what.textContent = e.text;
  div.appendChild(what);
  return div;
}

function appendEntries(entries) {
  const feed = $("feed");
  const pinned = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  for (const e of entries) feed.appendChild(renderEntry(e));
  // Keep the DOM from growing without bound between polls.
  while (feed.childElementCount > 2000) feed.removeChild(feed.firstChild);
  if (pinned) feed.scrollTop = feed.scrollHeight;
}

async function pollChat() {
  try {
    const c = await api("/api/chat?after=" + lastSeq);
    if (c.latest < lastSeq) {
      // The feed was reset (a new server session rewrote latest.log).
      $("feed").innerHTML = "";
      lastSeq = -1;
      const fresh = await api("/api/chat?after=-1");
      appendEntries(fresh.entries);
      lastSeq = fresh.latest;
    } else {
      appendEntries(c.entries);
      lastSeq = c.latest;
    }
  } catch (e) {
    // Retry on the next tick.
  }
}

$("cmdform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const cmd = $("command").value;
  if (!cmd.trim()) return;
  history.push(cmd);
  historyIndex = history.length;
  const out = $("response");
  out.className = "";
  out.textContent = "\u2026";
  try {
    const r = await api("/api/command", {
      method: "POST",
      body: JSON.stringify({ command: cmd })
    });
    out.textContent = r.response || "(no output)";
  } catch (e) {
    out.className = "error";
    out.textContent = String(e.message || e);
  }
});

$("command").addEventListener("keydown", (ev) => {
  if (ev.key === "ArrowUp") {
    if (historyIndex > 0) {
      historyIndex -= 1;
      $("command").value = history[historyIndex];
      ev.preventDefault();
    }
  } else if (ev.key === "ArrowDown") {
    if (historyIndex < history.length - 1) {
      historyIndex += 1;
      $("command").value = history[historyIndex];
    } else {
      historyIndex = history.length;
      $("command").value = "";
    }
    ev.preventDefault();
  }
});

if (TOKEN_REQUIRED) {
  if (!token) showLogin();
  $("loginform").addEventListener("submit", (ev) => {
    ev.preventDefault();
    token = $("tokeninput").value.trim();
    if (token) sessionStorage.setItem("panelToken", token);
    $("login").style.display = "none";
    pollState();
    pollChat();
  });
}

pollState();
pollChat();
setInterval(pollState, 4000);
setInterval(pollChat, 2000);
</script>
</body>
</html>
"""

LOGIN_BOX = """
<div id="login" class="login" style="display: none;">
  <form class="box" id="loginform" autocomplete="off">
    <h2>Panel token</h2>
    <p>This deployment requires a token. It is kept in your browser session
    only, and sent with every request.</p>
    <input id="tokeninput" type="password" placeholder="token">
    <button type="submit">Unlock</button>
  </form>
</div>
"""


def build_page():
    page = PAGE.replace("__TOKEN_REQUIRED__", "true" if PANEL_TOKEN else "false")
    return page.replace("__LOGIN__", LOGIN_BOX if PANEL_TOKEN else "")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mc-admin-panel/1.0"
    sys_version = ""

    state = None   # injected by main()
    feed = None
    rcon = None

    def log_message(self, fmt, *args):
        pass  # the panel's own console stays quiet

    def _authorized(self):
        if not PANEL_TOKEN:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Panel-Token", "").encode("utf-8"),
            PANEL_TOKEN.encode("utf-8"),
        )

    def _send_body(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        # Compact on purpose: no spaces, so the response greps cleanly.
        self._send_body(code, json.dumps(obj, separators=(",", ":")).encode(),
                        "application/json")

    def do_GET(self):
        parts = urlsplit(self.path)
    
        if parts.path in ("/", "/index.html"):
            return self._send_body(
                200,
                build_page().encode(),
                "text/html; charset=utf-8",
            )
    
        if not self._authorized():
            return self._send_json(401, {"error": "missing or bad X-Panel-Token"})
    
        if parts.path == "/api/state":
            return self._send_json(200, self.state.snapshot())
    
        if parts.path == "/api/chat":
            qs = parse_qs(parts.query)
            try:
                after = int(qs.get("after", ["-1"])[0])
            except ValueError:
                after = -1
            entries, latest = self.feed.entries_after(after)
            return self._send_json(200, {"entries": entries, "latest": latest})
    
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send_json(401, {"error": "missing or bad X-Panel-Token"})
        parts = urlsplit(self.path)
        if parts.path != "/api/command":
            return self._send_json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send_json(400, {"error": "body must be JSON"})
        command = body.get("command") if isinstance(body, dict) else None
        if not isinstance(command, str) or not command.strip():
            return self._send_json(400, {"error": "'command' must be a non-empty string"})
        command = command.strip()
        if len(command) > COMMAND_MAX_LENGTH:
            return self._send_json(
                400, {"error": "command too long (max %d characters)" % COMMAND_MAX_LENGTH})
        if not self.state.online:
            return self._send_json(503, {"error": "server is offline"})
        try:
            response = self.rcon.command(command)
        except Exception as exc:
            return self._send_json(503, {"error": str(exc) or "RCON failure"})
        return self._send_json(200, {"ok": True, "response": response})


def main():
    if not RCON_PASSWORD:
        print("warning: RCON_PASSWORD is empty; the panel will report the "
              "server as offline", flush=True)
    Handler.state = state = ServerState()
    Handler.feed = feed = ConsoleFeed()
    worker = RconWorker(state)
    Handler.rcon = worker.rcon

    feed.start()
    worker.start()

    httpd = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    httpd.daemon_threads = True
    print("admin panel on 0.0.0.0:%d (rcon %s:%d, log %s)"
          % (PANEL_PORT, RCON_HOST, RCON_PORT, LOG_PATH), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
