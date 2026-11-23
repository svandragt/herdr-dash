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

try:  # optional: the system Python's PyGObject, only for the dock badge
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
except Exception:
    bus = None

SOCKET = os.environ.get("HERDR_SOCKET_PATH", os.path.expanduser("~/.config/herdr/herdr.sock"))
REGISTRY = os.path.expanduser("~/.config/herdr-dash/remotes")
# Extra addresses besides localhost, e.g. a NetBird IP to reach the board from a phone.
BIND = ["127.0.0.1"] + os.environ.get("HERDR_DASH_BIND", "").split()
PORT = 7655
# POST is only allowed from the board's own page. Origin is checked, not Host: a DNS-rebinding
# page sends a matching Host but keeps its own domain in Origin.
ORIGINS = {"http://%s:%d" % (h, PORT) for h in BIND + ["localhost"]}
KEYS = {"1", "2", "3", "esc", "enter"}  # what the panel may send to a blocked agent
GONE_TTL = 30  # seconds a dimmed card lingers after its session exits
RECONCILE_EVERY = 5
PUSH_EVERY = 2  # seconds; the floor on how often a card may move
DESKTOP_ID = "hello-browser-herdr-board.desktop"  # the launcher AGENTS.md installs

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

# 12 and 7 share no factor, so index i gives a different pair for 84 indexes in a row.
ANIMALS = [("🦊", "fox"), ("🦉", "owl"), ("🐸", "frog"), ("🐳", "whale"),
           ("🐝", "bee"), ("🐢", "turtle"), ("🐙", "octopus"), ("🐧", "penguin"),
           ("🦔", "hedgehog"), ("🦀", "crab"), ("🐌", "snail"), ("🦜", "parrot")]
# Muted, so a background does not read as a status colour (red, orange, green borders).
COLOURS = [("blue", "#1f4e79"), ("purple", "#4b2a6b"), ("teal", "#145a5a"), ("pink", "#7a2e57"),
           ("yellow", "#6b5a14"), ("brown", "#5a3b1f"), ("grey", "#3f444c")]
icons = {}  # card key -> index into ANIMALS and COLOURS; the lowest free one, freed by forget()


def card(host, pane):
    pid = pane["pane_id"]
    key = f"{host}:{pid}"
    with lock:  # reader threads of different hosts can meet a new card at the same time
        if key not in icons:
            icons[key] = min(set(range(len(icons) + 1)) - set(icons.values()))
        i = icons[key]
    animal, colour = ANIMALS[i % len(ANIMALS)], COLOURS[i % len(COLOURS)]
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
        "icon": animal[0],
        "icon_name": f"{colour[0]} {animal[1]}",
        "icon_bg": colour[1],
    }


def forget(key):
    """Drop a dimmed card, unless a new agent claimed the pane in the meantime."""
    if panes.get(key, {}).get("gone") and panes.pop(key, None):
        # Else a recycled pane wears the dead agent's name until the new one
        # writes a summary of its own.
        summaries.pop(key, None)
        icons.pop(key, None)
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


def board():
    """A session titled ".name" is background work: show it only when it needs you."""
    return sorted((c for c in panes.values() if not c["title"].startswith(".") or c["status"] == "blocked"),
                  key=board_order)


def push_board():
    with lock:
        cards = board()
        targets = list(subscribers)
    waiting = sum(c["status"] in ("blocked", "done") and not c.get("gone") for c in cards)
    cards = json.dumps(cards)
    for q in targets:
        q.put(cards)
    set_dock_badge(waiting)


last_badge = None


def set_dock_badge(count):
    """Unity LauncherEntry: Plank and the Ubuntu dock draw it on the launcher icon.
    The dock drops a badge when its sender disconnects, so a one-shot `gdbus emit`
    can't hold one; this keeps the bus connection for the life of the server."""
    global last_badge
    if count == last_badge or bus is None:
        return
    last_badge = count
    bus.emit_signal(None, "/herdr_dash", "com.canonical.Unity.LauncherEntry", "Update",
                    GLib.Variant("(sa{sv})", (f"application://{DESKTOP_ID}", {
                        "count": GLib.Variant("x", count),
                        "count-visible": GLib.Variant("b", count > 0)})))
    bus.flush_sync()


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
<meta name=viewport content="width=device-width,initial-scale=1">
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
 .tab .n{color:#f85149;margin-left:6px}
 .strip:not(.on){display:none}
 .card{background:#1e2128;border-left:3px solid #3a3f48;border-radius:4px;padding:10px;
       margin-bottom:8px;cursor:pointer;display:flex;gap:8px;align-items:flex-start}
 .card>div{min-width:0}
 .ic{flex:none;width:24px;height:24px;border-radius:6px;display:flex;align-items:center;
     justify-content:center;font-size:15px}
 .card:hover{background:#262a32}
 .card .t{font-weight:500;margin-bottom:3px}
 .card .r{font-size:12px;color:#8b939c;font-family:ui-monospace,monospace}
 .blocked .card{border-left-color:#f85149;background:#2a1618}
 .working .card{border-left-color:#f0883e}
 .done .card{border-left-color:#3fb950}
 .fresh{outline:1px solid #bc8cff}
 .gone{opacity:.4}
 #panel{position:fixed;inset:0;display:flex;flex-direction:column;background:#14161a}
 #panel[hidden]{display:none}
 body.steering{overflow:hidden}
 #panel .ph{display:flex;gap:8px;align-items:center;padding:10px 16px;background:#1e2128;border-bottom:1px solid #2a2e36}
 #panel .ph span:last-of-type{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #recap{flex:1;overflow:auto;overscroll-behavior:contain;padding:12px 16px;white-space:pre-wrap;
        word-break:break-word;line-height:1.5;color:#b4bcc6}
 #panel .pf{padding:10px 16px;background:#1e2128;border-top:1px solid #2a2e36}
 #panel .in{display:flex;gap:6px;flex-wrap:wrap}
 #ptext{flex:1;min-width:140px;font-size:16px;background:#14161a;color:inherit;border:1px solid #3a3f48;
        border-radius:4px;padding:6px 8px}
 #panel button{font:inherit;background:#262a32;color:inherit;border:1px solid #3a3f48;border-radius:4px;
               padding:6px 12px;cursor:pointer}
 #perr{color:#f85149;font-size:12px;min-height:0}
</style>
<div id=board></div>
<div id=panel hidden>
 <div class=ph><button id=pclose aria-label=Back title=Back>&larr;</button><span class=ic id=pic></span><b id=pname></b><span id=ptitle></span></div>
 <div id=recap></div>
 <div class=pf><div class=in><input id=ptext placeholder="Send a prompt" autocomplete=off>
  <span id=pkeys><button data-k=1>1</button> <button data-k=2>2</button> <button data-k=3>3</button>
  <button data-k=esc>esc</button> <button data-k=enter>enter</button></span></div>
  <div id=perr></div></div>
</div>
<script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const COLS = {blocked:'blocked', working:'working', idle:'idle', unknown:'idle', done:'done'};
const TITLES = {blocked:'Waiting for you', working:'In progress', idle:'Idle', done:'Done'};
let active = localStorage.getItem('host');
// latest cards by key; target is the open view's card, last the card opened most recently;
// pushed is set while the open view owns a history entry
let latest = {}, target = null, last = null, pushed = false, timer = null;
// A stream that silently stops (sleep, network change) leaves a stale board; the server pings every 5s.
let heard = Date.now();
const es = new EventSource('/events');
es.addEventListener('ping', () => heard = Date.now());
setInterval(() => {
  if (Date.now() - heard > 15000) fetch('/').then(r => r.ok && location.reload(), () => {});
}, 5000);
es.onmessage = e => {
  heard = Date.now();
  const cards = JSON.parse(e.data), hosts = {};
  latest = Object.fromEntries(cards.map(c => [c.key, c]));
  for (const c of cards) (hosts[c.host] ||= []).push(c);
  const names = Object.keys(hosts);  // server sorts local first
  const waiting = cards.filter(c => (c.status == 'blocked' || c.status == 'done') && !c.gone).length;
  document.title = (waiting ? `(${waiting}) ` : '') + 'herdr board';
  if (!names.includes(active)) active = names[0];
  let html = '';
  if (names.length > 1) html += '<div class=tabs>' + names.map(h => {
    const n = hosts[h].filter(c => c.status == 'blocked' && !c.gone).length;
    return `<div class="tab${h == active ? ' on' : ''}" data-host="${esc(h)}">${esc(h)}${n ? `<span class=n>${n}</span>` : ''}</div>`;
  }).join('') + '</div>';
  for (const host of names) {
    const out = {blocked:'', working:'', idle:'', done:''};
    for (const c of hosts[host]) out[COLS[c.status] || 'idle'] +=
      `<div class="card${c.fresh ? ' fresh' : ''}${c.gone ? ' gone' : ''}" data-id="${esc(c.key)}">` +
      `<span class=ic style="background:${esc(c.icon_bg)}" title="${esc(c.icon_name)}">${esc(c.icon)}</span>` +
      `<div><div class=t>${esc(c.title || c.pane_id)}</div><div class=r>${esc(c.repo)}</div></div></div>`;
    html += `<div class="strip${host == active ? ' on' : ''}" data-host="${esc(host)}"><div class=cols>` +
      Object.keys(out).map(k => `<div class="col ${k}"><h2>${TITLES[k]}</h2>${out[k]}</div>`).join('') +
      '</div></div>';
  }
  document.getElementById('board').innerHTML = html;
  if (target) head();
};
// The panel lives outside #board because every push replaces #board's innerHTML, which would wipe typed text.
const $ = id => document.getElementById(id);
const head = () => {
  const c = latest[target];
  if (!c) return;  // the card is gone or hidden; keep its last header
  $('pic').textContent = c.icon;
  $('pic').style.background = c.icon_bg;
  $('pic').title = c.icon_name;
  $('pname').textContent = c.icon_name;
  $('ptitle').textContent = c.title || c.pane_id;
};
async function act(route, body) {
  const k = target;
  try {
    const r = await fetch('/' + route + '/' + k, {method: 'POST', body});
    if (k != target) return;
    $('perr').textContent = r.ok ? '' : `${route} failed (${r.status})`;
    return r;
  } catch (x) {
    if (k == target) $('perr').textContent = route + ' failed: no answer';
  }
}
async function refresh() {
  const k = target, r = await act('read', '');
  if (!r?.ok) return;
  const t = await r.text();
  if (k != target || t == $('recap').textContent) return;  // unchanged: keep the reader's selection
  const box = $('recap'), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  box.textContent = t;  // text, never HTML: a terminal can print anything
  if (atBottom) box.scrollTop = box.scrollHeight;  // reading further up must not be yanked down
}
function openPanel(key) {
  if (key != last) {  // never carry typed text over to another agent
    $('recap').textContent = $('perr').textContent = $('ptext').value = '';
    last = key;
  }
  history.pushState({card: key}, '');  // the back button or gesture closes the view
  pushed = true;
  target = key;
  head();
  $('panel').hidden = false;
  document.body.classList.add('steering');
  $('recap').scrollTop = $('recap').scrollHeight;  // a hidden element forgets its scroll position
  refresh();
  clearInterval(timer);
  timer = setInterval(refresh, 5000);
}
function closePanel() {
  target = null;
  pushed = false;
  clearInterval(timer);
  $('panel').hidden = true;
  document.body.classList.remove('steering');
}
// popstate does the closing, so the history entry openPanel pushed is always popped.
const back = () => pushed ? (pushed = false, history.back()) : closePanel();
$('pclose').onclick = back;
addEventListener('popstate', closePanel);
document.addEventListener('keydown', e => e.key == 'Escape' && target && !e.isComposing && back());
$('pkeys').onclick = e => e.target.dataset.k && act('keys', e.target.dataset.k);
$('ptext').onkeydown = async e => {
  const v = $('ptext').value;
  if (e.key != 'Enter' || e.isComposing || !v.trim()) return;
  const r = await act('prompt', v);
  if (r?.status == 204 && $('ptext').value == v) $('ptext').value = '';
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
  if (el) {
    fetch('/focus/' + el.dataset.id, {method: 'POST'});
    openPanel(el.dataset.id);
  }
});
</script>"""


# A rule line is only box-drawing characters; Claude can also put the session name in the input box's top rule.
RULE = re.compile(r"[\u2500-\u257f]+|\u2500+ .+ \u2500+")


def clean_recap(text):
    """Plain recap of a pane: no rule lines, and no Claude input box or status footer below them."""
    lines = text.split("\n")
    rules = [i for i, l in enumerate(lines) if RULE.fullmatch(l.strip())]
    if len(rules) >= 2:  # the input box sits between the last two rules
        lines = lines[:rules[-2]]
    out = []
    for l in lines:
        if RULE.fullmatch(l.strip()):
            continue
        if l.strip():
            out.append(l)
        elif out and out[-1]:
            out.append("")
    return "\n".join(out).strip("\n")


def read_pane(host, pane_id):
    """Recent output as plain text. herdr reads a working agent's history only when idle, so use its screen then."""
    reply = call(host, "agent.read", {"target": pane_id, "source": "recent_unwrapped", "lines": 60})
    if reply.get("error", {}).get("code") == "agent_not_idle":
        reply = call(host, "agent.read", {"target": pane_id, "source": "visible"})
    return clean_recap(reply["result"]["read"]["text"])


def allowed_origin(origin):
    return origin in ORIGINS


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
            cards = json.dumps(board())
        try:
            q.put(cards)
            while True:
                try:
                    msg = "data: " + q.get(timeout=5) + "\n\n"
                except queue.Empty:
                    msg = "event: ping\ndata:\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with lock:
                subscribers.remove(q)

    def do_POST(self):
        if not allowed_origin(self.headers.get("Origin")):
            self.send_error(403)
            return
        route, _, key = self.path[1:].partition("/")
        host, _, pane_id = key.partition(":")
        if route not in ("focus", "prompt", "keys", "read") or not (host and pane_id):
            self.send_error(404)
            return
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        except ValueError:  # a bad length or non-UTF-8 text
            self.send_error(400)
            return
        keys = body.split()
        if (route == "prompt" and not body.strip()) or (route == "keys" and not (keys and set(keys) <= KEYS)):
            self.send_error(400)
            return
        method, params = {
            "focus": ("agent.focus", {"target": pane_id}),
            "prompt": ("agent.prompt", {"target": pane_id, "text": body.strip()}),
            "keys": ("agent.send_keys", {"target": pane_id, "keys": keys}),
        }.get(route, (None, None))  # /read goes through read_pane()
        try:
            # An error response has no "result", so it lands in the KeyError branch too.
            if route == "read":
                text = read_pane(host, pane_id).encode()
            else:
                call(host, method, params)["result"]
                text = b""
        except (OSError, ValueError, KeyError):
            self.send_error(502)
            return
        if route == "focus":
            mark_seen(key)
        self.send_response(200 if route == "read" else 204)
        if route == "read":
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)


if __name__ == "__main__":
    threading.Thread(target=reader, args=("local",), daemon=True).start()
    threading.Thread(target=reconcile, daemon=True).start()
    threading.Thread(target=pusher, daemon=True).start()
    for host in BIND[1:]:
        try:
            # ThreadingHTTPServer: /events blocks forever, so one thread would deadlock.
            server = ThreadingHTTPServer((host, PORT), Handler)
        except OSError as e:
            # shortcut: an address that isn't up yet (VPN down) stays skipped until a restart
            print("skipping %s: %s" % (host, e))
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("http://%s:%d" % (host, PORT))
    print("http://%s:%d" % (BIND[0], PORT))
    ThreadingHTTPServer((BIND[0], PORT), Handler).serve_forever()
