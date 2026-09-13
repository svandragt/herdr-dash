# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## Running

```sh
./herdr-dash.py     # serves http://127.0.0.1:7655
```

Stdlib only — no venv, no dependencies, no build step. There are no tests or
linters; verify by running it against a live herdr and watching the board.

`HERDR_SOCKET_PATH` overrides the socket (default `~/.config/herdr/herdr.sock`).
herdr must be running, or `reader()` dies on connect at startup.

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
| `reader` | seeds from `agent.list`, then streams events |
| `reconcile` | 5s poll for vanished panes |
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

## Frontend

`PAGE` is a single string constant — inline CSS and JS, no build, no framework.
The SSE handler replaces four columns' `innerHTML` wholesale on every push.
Card titles come from herdr and are interpolated unescaped; that's acceptable
only because the server binds to `127.0.0.1` and the data is your own terminal
titles. Clicking a card POSTs `/focus/<pane_id>`, which calls `agent.focus`
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
