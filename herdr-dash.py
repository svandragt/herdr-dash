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
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCKET = os.environ.get("HERDR_SOCKET_PATH", os.path.expanduser("~/.config/herdr/herdr.sock"))
REGISTRY = os.path.expanduser("~/.config/herdr-dash/remotes")
ADDR = ("127.0.0.1", 7655)
GONE_TTL = 30  # seconds a dimmed card lingers after its session exits
RECONCILE_EVERY = 5
PUSH_EVERY = 2  # seconds; the floor on how often a card may move

hosts = {"local": SOCKET}  # host name -> socket path; remotes added by reconcile()
tunnels = {}  # host name -> ssh Popen, remotes only
panes = {}
subscribers = []
lock = threading.Lock()
dirty = threading.Event()


def connect(host):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(hosts[host])
    return s


def send(sock, method, params, req_id="dash"):
    sock.sendall((json.dumps({"id": req_id, "method": method, "params": params}) + "\n").encode())


def call(host, method, params):
    """One-shot request. herdr rejects requests without an id field."""
    s = connect(host)
    try:
        send(s, method, params)
        return json.loads(s.makefile("r").readline())
    finally:
        s.close()


def read_registry():
    """target -> remote socket path, from one `target` or `target:/remote/sock` per line."""
    registry = {}
    try:
        with open(REGISTRY) as f:
            for line in f:
                target, _, remote_sock = line.strip().partition(":")
                if target:
                    registry[target] = remote_sock or SOCKET
    except FileNotFoundError:
        pass
    return registry


def spawn_tunnel(target, remote_sock):
    """Forward a short-lived local socket to the remote one over ssh -L. Short path:
    long forwarding specs get rejected by ssh."""
    sock = f"/tmp/herdr-dash-{target}.sock"
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass
    proc = subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                              "-L", f"{sock}:{remote_sock}", target])
    return sock, proc


# Claude titles its terminal "claude/<uuid>: running Bash" whenever it has no
# session summary yet; the uuid is noise on a card.
UUID_PREFIX = re.compile(r"^\w+/[0-9a-f-]{36}:\s*")

# While a session works, Claude replaces its summary title with live activity, so a
# busy card would lose the only thing naming it. Keep the last real summary instead.
# ponytail: vocabulary match, swap for a herdr-supplied summary if one ever appears.
ACTIVITY = re.compile(r"^(thinking|idle|working\b|running\b|compacting\b|.*\(after .*\))", re.I)
summaries = {}


def card(host, pane):
    pid = pane["pane_id"]
    key = f"{host}:{pid}"
    title = UUID_PREFIX.sub("", pane.get("terminal_title_stripped") or pane.get("terminal_title") or "")
    if title and not ACTIVITY.match(title):
        summaries[key] = title
    return {
        "key": key,
        "host": host,
        "pane_id": pid,
        "status": pane.get("agent_status") or "unknown",
        "title": summaries.get(key, title),
        "repo": os.path.basename(pane.get("cwd") or "") or "~",
        "fresh": False,
        "gone": False,
    }


def forget(key):
    """Drop a dimmed card, unless a new agent claimed the pane in the meantime."""
    if panes.get(key, {}).get("gone") and panes.pop(key, None):
        # Else a recycled pane wears the dead agent's name until the new one
        # writes a summary of its own.
        summaries.pop(key, None)
        broadcast()


def mark_gone(key, status=None):
    """The session exited but the pane is still open. Dim the card, then drop it."""
    c = panes.get(key)
    if not c or c["gone"]:
        return False
    panes[key] = {**c, "gone": True, "status": status or c["status"]}
    t = threading.Timer(GONE_TTL, forget, (key,))
    t.daemon = True
    t.start()
    return True


def board_order(c):
    return (c["host"] != "local", c["host"], c["pane_id"])


def push_board():
    with lock:
        board = json.dumps(sorted(panes.values(), key=board_order))
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


def absorb(host, pane):
    if not pane.get("agent"):
        return mark_gone(f"{host}:{pane['pane_id']}")
    c = card(host, pane)
    key = c["key"]
    prev = panes.get(key)
    # A changed card stays lit until you actually look at its pane. A change
    # that happens while the pane is focused was seen as it happened.
    if prev and not pane.get("focused"):
        c["fresh"] = prev["fresh"] or prev["status"] != c["status"]
    if prev == c:
        return False  # pane.updated fires on every render; only real changes matter
    panes[key] = c
    return True


def mark_seen(key):
    """Clicking a card is looking at it; don't wait for the focus event."""
    c = panes.get(key)
    if c and c["fresh"]:
        panes[key] = {**c, "fresh": False}
        broadcast()


def reader(host):
    """Same loop for local and remote: a dropped connection (herdr restart, a
    tunnel that hasn't come up yet) just retries, re-seeding from agent.list."""
    while True:
        try:
            for pane in call(host, "agent.list", {})["result"]["agents"]:
                absorb(host, pane)
            broadcast()
            # A fresh socket: herdr closes the connection after answering a one-shot request.
            s = connect(host)
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
                    changed = absorb(host, data["pane"])
                elif data.get("released"):
                    changed = mark_gone(f"{host}:{data.get('pane_id')}", data.get("final_status"))
                else:
                    continue
                if changed:
                    broadcast()
        except (OSError, ValueError, KeyError):
            if host not in hosts:
                return  # unregistered while we were connected
            time.sleep(RECONCILE_EVERY)


def reconcile():
    """Events carry status promptly but can be missed; agent.list is the truth.
    Also owns the remote roster: re-reads the registry each tick, and respawns any
    ssh tunnel that has died so a dropped remote reconnects on its own."""
    while True:
        time.sleep(RECONCILE_EVERY)
        registry = read_registry()
        for target, remote_sock in registry.items():
            if target not in hosts:
                sock, proc = spawn_tunnel(target, remote_sock)
                hosts[target] = sock
                tunnels[target] = proc
                threading.Thread(target=reader, args=(target,), daemon=True).start()
        for target in [t for t in hosts if t != "local" and t not in registry]:
            tunnels.pop(target).terminate()
            for key in [k for k in panes if k.startswith(f"{target}:")]:
                mark_gone(key)
            del hosts[target]
        for target, proc in list(tunnels.items()):
            if proc.poll() is not None and target in hosts:
                _, tunnels[target] = spawn_tunnel(target, registry[target])
        for host in list(hosts):
            try:
                agents = call(host, "agent.list", {})["result"]["agents"]
            except Exception:
                continue  # herdr (or the tunnel) is down — keep the last board rather than wiping it
            live = {f"{host}:{p['pane_id']}" for p in agents}
            gone = [k for k in panes if k.startswith(f"{host}:") and k not in live]
            # A missed pane.updated (reconnect gap) would otherwise pin a stale status forever.
            if any([absorb(host, p) for p in agents] + [mark_gone(k) for k in gone]):
                broadcast()


PAGE = """<!doctype html><meta charset=utf-8><title>herdr board</title>
<style>
 body{margin:0;font:14px system-ui;background:#14161a;color:#e6e6e6}
 /* rtl flips the grid so the most actionable column sits on the right, nearest
    the pane you just came from; .col puts text back the right way round. */
 .cols{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:16px;direction:rtl}
 .col{direction:ltr}
 /* Narrow: one stack. Column order is already priority order, so the actionable
    cards land at the top for free. */
 @media (max-width:700px){
  .cols{grid-template-columns:1fr;gap:4px}
  .col:not(:has(.card)){display:none}
 }
 .col h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#8b939c;margin:0 0 8px}
 .tabs{display:flex;gap:4px;padding:12px 16px 0;border-bottom:1px solid #2a2e36}
 .tab{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#8b939c;
      padding:6px 12px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
 .tab.on{color:#e6e6e6;border-bottom-color:#4a9eff}
 .tab .n{color:#f0883e;margin-left:6px}
 .strip:not(.on){display:none}
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
<div id=board></div>
<script>
const COLS = {blocked:'blocked', working:'working', idle:'idle', unknown:'idle', done:'done'};
const TITLES = {blocked:'Waiting for you', working:'In progress', idle:'Idle', done:'Done'};
let active = localStorage.getItem('host');
new EventSource('/events').onmessage = e => {
  const cards = JSON.parse(e.data), hosts = {};
  for (const c of cards) (hosts[c.host] ||= []).push(c);
  const names = Object.keys(hosts);  // server sorts local first
  const waiting = cards.filter(c => c.status == 'blocked' && !c.gone).length;
  document.title = (waiting ? `(${waiting}) ` : '') + 'herdr board';
  if (!names.includes(active)) active = names[0];
  let html = '';
  if (names.length > 1) html += '<div class=tabs>' + names.map(h => {
    const n = hosts[h].filter(c => c.status == 'blocked' && !c.gone).length;
    return `<div class="tab${h == active ? ' on' : ''}" data-host="${h}">${h}${n ? `<span class=n>${n}</span>` : ''}</div>`;
  }).join('') + '</div>';
  for (const host of names) {
    const out = {blocked:'', working:'', idle:'', done:''};
    for (const c of hosts[host]) out[COLS[c.status] || 'idle'] +=
      `<div class="card${c.fresh ? ' fresh' : ''}${c.gone ? ' gone' : ''}" data-id="${c.key}">` +
      `<div class=t>${c.title || c.pane_id}</div><div class=r>${c.repo}</div></div>`;
    html += `<div class="strip${host == active ? ' on' : ''}" data-host="${host}"><div class=cols>` +
      Object.keys(out).map(k => `<div class="col ${k}"><h2>${TITLES[k]}</h2>${out[k]}</div>`).join('') +
      '</div></div>';
  }
  document.getElementById('board').innerHTML = html;
};
document.addEventListener('click', e => {
  const tab = e.target.closest('.tab');
  if (tab) {
    active = tab.dataset.host;
    localStorage.setItem('host', active);
    for (const el of document.querySelectorAll('.tab,.strip')) el.classList.toggle('on', el.dataset.host == active);
    return;
  }
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
            board = json.dumps(sorted(panes.values(), key=board_order))
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
        key = self.path[len("/focus/"):]
        host, pane_id = key.split(":", 1)
        call(host, "agent.focus", {"target": pane_id})
        mark_seen(key)
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    threading.Thread(target=reader, args=("local",), daemon=True).start()
    threading.Thread(target=reconcile, daemon=True).start()
    threading.Thread(target=pusher, daemon=True).start()
    print("http://%s:%d" % ADDR)
    # ThreadingHTTPServer: /events blocks forever, so one thread would deadlock.
    ThreadingHTTPServer(ADDR, Handler).serve_forever()
