# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## Running

```sh
./herdr-dash.py     # serves http://127.0.0.1:7655
```

Stdlib only — no venv, no dependencies, no build step. There are no tests or
linters; verify by running it against a live herdr and watching the board.

`HERDR_SOCKET_PATH` overrides the local socket (default `~/.config/herdr/herdr.sock`).
herdr doesn't need to be running yet — `reader()` retries on a failed connect,
so it just picks the board up once herdr starts.

## Architecture

One file, ~250 lines. It is a read-mostly bridge: herdr already classifies each
agent pane as idle/working/blocked/done, so this process owns no logic about
agent state — only about *presentation timing*.

**The herdr protocol** is newline-delimited JSON over a unix socket. Every
request needs an `id` field or herdr rejects it. Two shapes:

- One-shot (`call()`): connect, send, read one line, close. herdr closes the
  connection after answering, so a subscription needs its own fresh socket.
- Subscription (`reader()`): `events.subscribe` to `pane.updated` and
  `pane.agent_detected`, then read lines forever.

**Two sources of truth, deliberately.** Events carry status changes promptly but
never announce a pane *disappearing*; `agent.list` is authoritative about who
exists. So `reconcile()` polls it every 5s and dims anything missing. Neither
alone is sufficient — don't collapse them.

**Threads** (all daemons, joined only by process exit):

| Thread | Job |
|---|---|
| `reader` | one per host; seeds from `agent.list`, then streams events, reconnecting on failure |
| `reconcile` | one, all hosts; 5s poll for vanished panes, plus the remote roster (tunnels, reader threads) |
| `pusher` | coalesces `dirty` into an SSE push at most every 2s |
| HTTP | `ThreadingHTTPServer` — `/events` blocks forever, one thread deadlocks |

`broadcast()` only sets the `dirty` event; `pusher` decides when the board
actually moves. That rate limit is a UX decision (a card flapping between
columns faster than you can read it is worse than a stale one), not a
performance one.

`lock` guards `panes` and `subscribers` only at serialisation/subscribe points.
Mutations to `panes` happen unlocked from `reader` and `reconcile` — safe by
CPython dict atomicity, not by design.

**Card lifecycle:** live → `gone` (dimmed, 30s `threading.Timer`) → forgotten.
`forget()` re-checks `gone` because a new agent may have claimed the pane.

**Title handling** is the one piece of real cleverness. Claude overwrites its
session summary with live activity ("thinking", "running Bash") while working,
so a busy card would lose its only name. `summaries` keeps the last non-activity
title per pane; `ACTIVITY` is a vocabulary regex and will need extending as
Claude's status words change.

## Remotes

`herdr --remote wyse` talks to a wholly separate herdr server on that machine, with
its own `agent.list`. The board treats a registered remote as just another host:

```sh
./herdr-board register wyse      # append to ~/.config/herdr-dash/remotes
./herdr-board unregister wyse    # remove it
./herdr-board list                # show what's registered
```

`reconcile()` re-reads that file every tick; it doesn't need the board restarted.
For each new host it opens the ssh tunnel and starts that host's `reader`; for a
removed one it kills the tunnel and dims its cards.

The board owns the tunnel (`ssh -N -L <local-sock>:<remote-sock> <target>`) rather
than expecting one to already exist. The local end is a short path,
`/tmp/herdr-dash-<target>.sock` — a long forwarding spec gets rejected by ssh, which
is also why the registry line is `target` or `target:/remote/socket/path` rather than
the local path.

Cards are keyed `<host>:<pane_id>`, not the bare pane id: two independent herdr
servers mint ids from their own counters, so `wyse` and `local` can both hand out
`p1` for unrelated panes. `agent.focus` still wants the raw id, so the key is split
back into host and pane id at the point of use (`/focus/<key>`, `reconcile`'s
vanished-pane check).

If a tunnel dies, `reconcile` notices (`Popen.poll()`) and respawns it — that's the
reconnect for a dropped ssh, the same way `reader`'s retry loop is the reconnect for
a dropped herdr.

## Frontend

`PAGE` is a single string constant — inline CSS and JS, no build, no framework.
The SSE handler rebuilds the whole board on every push: one strip of four
columns per host, local first. With more than one host, a tab bar picks the
visible strip; the choice lives in `localStorage`, and an inactive tab shows how
many of its cards are blocked so a remote that needs you still surfaces.
Card titles come from herdr and are interpolated unescaped; that's acceptable
only because the server binds to `127.0.0.1` and the data is your own terminal
titles. Clicking a card POSTs `/focus/<key>`, which calls `agent.focus`
and clears the card's `fresh` flag. The violet outline means *this card
changed status and you haven't looked at the pane since* — it clears either
on that click or when herdr reports the pane focused, so focusing a pane
directly in your terminal counts as seeing it.

## Desktop launcher

`herdr-board` is the entry point for the apps menu: it starts `herdr-dash.py` if
the port isn't already answering, waits for it, then opens the board in its own
hello-browser window (`~/dev/vala/hello-browser`) rather than a browser tab.
The wait loop matters — a first GET that fails leaves a blank error page, and
`EventSource` never gets the chance to reconnect.

The `.desktop` entry isn't in the repo because `Exec` needs an absolute path.
Recreate it with:

```sh
cat > ~/.local/share/applications/hello-browser-herdr-board.desktop <<EOF
[Desktop Entry]
Type=Application
Name=herdr board
Comment=Kanban board for herdr-managed agent sessions
Exec=$PWD/herdr-board
Icon=utilities-system-monitor
Categories=System;Monitor;
Terminal=false
StartupNotify=true
StartupWMClass=com.github.svandragt.hello-browser.herdr-board
EOF
update-desktop-database ~/.local/share/applications
```

`StartupWMClass` must match the `--class` the wrapper passes, or GNOME won't
group the window under this launcher's icon.

A launcher runs under the systemd user environment, not your shell's, so test
changes to `herdr-board` that way rather than from a terminal:

```sh
env -i HOME="$HOME" DISPLAY="$DISPLAY" \
  PATH="$(systemctl --user show-environment | sed -n 's/^PATH=//p')" \
  ./herdr-board
```

That PATH has no `~/bin`, which is why the wrapper resolves the hello-browser
binary itself instead of trusting `make link`.
