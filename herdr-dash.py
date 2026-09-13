#!/usr/bin/env python3
"""A kanban board for herdr-managed agent sessions.

herdr already classifies every agent pane as idle/working/blocked/done by
scraping its terminal buffer; this just draws the result.
"""
import json
import os
import queue
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCKET = os.environ.get("HERDR_SOCKET_PATH", os.path.expanduser("~/.config/herdr/herdr.sock"))
ADDR = ("127.0.0.1", 7655)
GONE_TTL = 30  # seconds a dimmed card lingers after its session exits
RECONCILE_EVERY = 5
PUSH_EVERY = 2  # seconds; the floor on how often a card may move

panes = {}
subscribers = []
lock = threading.Lock()
dirty = threading.Event()


def connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCKET)
    return s


def send(sock, method, params, req_id="dash"):
    sock.sendall((json.dumps({"id": req_id, "method": method, "params": params}) + "\n").encode())


def call(method, params):
    """One-shot request. herdr rejects requests without an id field."""
    s = connect()
    try:
        send(s, method, params)
        return json.loads(s.makefile("r").readline())
    finally:
        s.close()


# Claude titles its terminal "claude/<uuid>: running Bash" whenever it has no
# session summary yet; the uuid is noise on a card.
UUID_PREFIX = re.compile(r"^\w+/[0-9a-f-]{36}:\s*")

# While a session works, Claude replaces its summary title with live activity, so a
# busy card would lose the only thing naming it. Keep the last real summary instead.
# ponytail: vocabulary match, swap for a herdr-supplied summary if one ever appears.
ACTIVITY = re.compile(r"^(thinking|idle|working\b|running\b|compacting\b|.*\(after .*\))", re.I)
summaries = {}


def card(pane):
    pid = pane["pane_id"]
    title = UUID_PREFIX.sub("", pane.get("terminal_title_stripped") or pane.get("terminal_title") or "")
    if title and not ACTIVITY.match(title):
        summaries[pid] = title
    return {
        "pane_id": pid,
        "status": pane.get("agent_status") or "unknown",
        "title": summaries.get(pid, title),
        "repo": os.path.basename(pane.get("cwd") or "") or "~",
        "fresh": False,
        "gone": False,
    }


def forget(pid):
    """Drop a dimmed card, unless a new agent claimed the pane in the meantime."""
    if panes.get(pid, {}).get("gone") and panes.pop(pid, None):
        # Else a recycled pane wears the dead agent's name until the new one
        # writes a summary of its own.
        summaries.pop(pid, None)
        broadcast()


def mark_gone(pid, status=None):
    """The session exited but the pane is still open. Dim the card, then drop it."""
    c = panes.get(pid)
    if not c or c["gone"]:
        return False
    panes[pid] = {**c, "gone": True, "status": status or c["status"]}
    t = threading.Timer(GONE_TTL, forget, (pid,))
    t.daemon = True
    t.start()
    return True


def push_board():
    with lock:
        board = json.dumps(sorted(panes.values(), key=lambda c: c["pane_id"]))
        targets = list(subscribers)
    for q in targets:
        q.put(board)


def broadcast():
    dirty.set()


def pusher():
    """Coalesce bursts: a status can flap several times a second, and a card that
    jumps between columns faster than you can read it is worse than a stale one."""
    while True:
        dirty.wait()
        dirty.clear()
        push_board()
        time.sleep(PUSH_EVERY)


def absorb(pane):
    if not pane.get("agent"):
        return mark_gone(pane["pane_id"])
    c = card(pane)
    pid = c["pane_id"]
    prev = panes.get(pid)
    # A changed card stays lit until you actually look at its pane. A change
    # that happens while the pane is focused was seen as it happened.
    if prev and not pane.get("focused"):
        c["fresh"] = prev["fresh"] or prev["status"] != c["status"]
    if prev == c:
        return False  # pane.updated fires on every render; only real changes matter
    panes[pid] = c
    return True


def mark_seen(pid):
    """Clicking a card is looking at it; don't wait for the focus event."""
    c = panes.get(pid)
    if c and c["fresh"]:
        panes[pid] = {**c, "fresh": False}
        broadcast()


def reader():
    for pane in call("agent.list", {})["result"]["agents"]:
        absorb(pane)
    broadcast()
    # A fresh socket: herdr closes the connection after answering a one-shot request.
    s = connect()
    send(s, "events.subscribe", {"subscriptions": [
        {"type": "pane.updated"}, {"type": "pane.agent_detected"},
    ]}, "sub")
    f = s.makefile("r")
    for line in f:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        data = msg.get("data")
        if not isinstance(data, dict):
            continue
        if "pane" in data:
            changed = absorb(data["pane"])
        elif data.get("released"):
            changed = mark_gone(data.get("pane_id"), data.get("final_status"))
        else:
            continue
        if changed:
            broadcast()


def reconcile():
    """Events carry status promptly but not disappearance; agent.list is the truth."""
    while True:
        time.sleep(RECONCILE_EVERY)
        try:
            live = {p["pane_id"] for p in call("agent.list", {})["result"]["agents"]}
        except Exception:
            continue  # herdr restarting — keep the last board rather than wiping it
        if any([mark_gone(pid) for pid in list(panes) if pid not in live]):
            broadcast()


PAGE = """<!doctype html><meta charset=utf-8><title>herdr board</title>
<style>
 body{margin:0;font:14px system-ui;background:#14161a;color:#e6e6e6}
 h1{font-size:13px;font-weight:600;padding:12px 16px;margin:0;color:#8b939c}
 .cols{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:0 16px 16px}
 .col h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#8b939c;margin:0 0 8px}
 .card{background:#1e2128;border-left:3px solid #3a3f48;border-radius:4px;padding:10px;
       margin-bottom:8px;cursor:pointer}
 .card:hover{background:#262a32}
 .card .t{font-weight:500;margin-bottom:3px}
 .card .r{font-size:12px;color:#8b939c;font-family:ui-monospace,monospace}
 .blocked .card{border-left-color:#f0883e;background:#2a1f16}
 .working .card{border-left-color:#4a9eff}
 .done .card{border-left-color:#3fb950}
 .fresh{outline:1px solid #bc8cff}
 .gone{opacity:.4}
</style>
<h1>herdr board</h1>
<div class=cols>
 <div class="col blocked"><h2>Waiting for you</h2><div id=blocked></div></div>
 <div class="col working"><h2>In progress</h2><div id=working></div></div>
 <div class="col idle"><h2>Idle</h2><div id=idle></div></div>
 <div class="col done"><h2>Done</h2><div id=done></div></div>
</div>
<script>
const COLS = {blocked:'blocked', working:'working', idle:'idle', unknown:'idle', done:'done'};
new EventSource('/events').onmessage = e => {
  const cards = JSON.parse(e.data), out = {blocked:'', working:'', idle:'', done:''};
  for (const c of cards) {
    out[COLS[c.status] || 'idle'] +=
      `<div class="card${c.fresh ? ' fresh' : ''}${c.gone ? ' gone' : ''}" data-id="${c.pane_id}">` +
      `<div class=t>${c.title || c.pane_id}</div><div class=r>${c.repo}</div></div>`;
  }
  for (const k in out) document.getElementById(k).innerHTML = out[k];
};
document.addEventListener('click', e => {
  const el = e.target.closest('.card');
  if (el) fetch('/focus/' + el.dataset.id, {method: 'POST'});
});
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/events":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue()
        with lock:
            subscribers.append(q)
            board = json.dumps(sorted(panes.values(), key=lambda c: c["pane_id"]))
        try:
            q.put(board)
            while True:
                self.wfile.write(("data: " + q.get() + "\n\n").encode())
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with lock:
                subscribers.remove(q)

    def do_POST(self):
        if not self.path.startswith("/focus/"):
            self.send_error(404)
            return
        target = self.path[len("/focus/"):]
        call("agent.focus", {"target": target})
        mark_seen(target)
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=reconcile, daemon=True).start()
    threading.Thread(target=pusher, daemon=True).start()
    print("http://%s:%d" % ADDR)
    # ThreadingHTTPServer: /events blocks forever, so one thread would deadlock.
    ThreadingHTTPServer(ADDR, Handler).serve_forever()
