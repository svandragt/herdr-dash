# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## Running

```sh
./herdr-dash.py     # serves http://127.0.0.1:7655
```

Stdlib only — no venv, no dependencies, no build step. The one exception is the
dock badge, which uses the system Python's PyGObject if it imports and is skipped
if not. `python3 test_fresh.py` is an assert-based self-check of the card
logic; beyond that, verify by running against a live herdr and watching the board.
To cut a release, follow `RELEASING.md`.

`HERDR_SOCKET_PATH` overrides the local socket (default `~/.config/herdr/herdr.sock`).
herdr doesn't need to be running yet — `reader()` retries on a failed connect,
so it just picks the board up once herdr starts.

`HERDR_DASH_BIND` adds space-separated listen addresses beside `127.0.0.1`, one
`ThreadingHTTPServer` each. An address that fails to bind is skipped, so a VPN
that is down never stops the localhost board the launcher waits for.

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

`lock` guards `panes` and `subscribers` only at serialisation/subscribe points,
and the assignment of a new card's index in `icons`.
Mutations to `panes` happen unlocked from `reader` and `reconcile` — safe by
CPython dict atomicity, not by design.

**Card lifecycle:** live → `gone` (dimmed, 30s `threading.Timer`) → forgotten.
`forget()` re-checks `gone` because a new agent may have claimed the pane.

**Icons:** each card shows an animal on a coloured background to tell similar
sessions apart. `card()` gives a new key the lowest index not used in `icons`, and
`forget()` frees it, so a card keeps its icon while it lives (also while dimmed)
and the next new card reuses a freed one. Index `i` maps to `ANIMALS[i % 12]` and
`COLOURS[i % 7]`. 12 and 7 share no factor, so 84 consecutive indexes give 84
different pairs and the first 7 cards differ in both animal and colour. The
lowest free index is used, not a hash of the key, because a hash often gives two
cards the same pair. Hidden cards stay in `panes`, so they hold an index too. The
card JSON carries `icon`, `icon_name` ("blue fox") and `icon_bg`. Icons reset
when the board restarts.

**Title handling** is the one piece of real cleverness. Claude overwrites its
session summary with live activity ("thinking", "running Bash") while working,
so a busy card would lose its only name. `summaries` keeps the last non-activity
title per pane; `ACTIVITY` is a vocabulary regex and will need extending as
Claude's status words change.

**Hidden sessions:** `board()` drops cards whose title starts with `.` unless
they are blocked. It filters on the server, so the page title, tab counts and
dock badge all ignore hidden cards. The card stays in `panes`, which keeps its
`fresh` flag and title history. A worktree folder that starts with `.` does not
hide a card, because herdr creates every worktree that way.

## Steering

The page can send input to an agent. Every POST route splits the key
`<host>:<pane_id>` and calls herdr through `call()`:

| Route | Body | herdr call |
|---|---|---|
| `/focus/<key>` | none | `agent.focus`, then `mark_seen()` |
| `/prompt/<key>` | the text | `agent.prompt` with `target` and `text` |
| `/keys/<key>` | space-separated key names from `KEYS` (`1 2 3 esc enter`) | `agent.send_keys` with `target` and `keys` |
| `/read/<key>` | none | `agent.read` with `source: recent_unwrapped` and `lines: 60`, or `source: visible` when herdr answers `agent_not_idle` (a working agent's history can be read only while it is idle); answers `clean_recap()` of the text as `text/plain` |

An empty prompt or a key outside `KEYS` is a 400. A herdr call that raises or
answers with an error is a 502. `/read` is a POST, not a GET, so the `Origin`
check covers it: a GET would let a DNS-rebinding page read terminal output. The parameter
names come from `herdr api schema --json` (`AgentPromptParams`,
`AgentSendKeysParams`, `AgentReadParams`); the read text is
`result.read.text` in the response.

**`clean_recap(text)`** turns the pane text into a readable recap. A rule line
is a line that is only box-drawing characters (U+2500 to U+257F), or a run of
`─` with a label after it: Claude puts the session name in the input box's top
rule, so that rule has letters. If the text has at least two rule lines, the
function cuts everything from the second-to-last one, which removes Claude's
input box and the status footer below it. It then drops the remaining rule
lines, collapses runs of blank lines to one and trims blank lines at both ends.
Only Claude draws that box. Another agent's pane has no rule lines, so it keeps
its footer text. A table border above the box is also a rule line, but it is
only dropped: the box's own two rules are the last two, so they start the cut.
`recent_unwrapped` joins the terminal's soft wraps, so the page wraps lines to
its own width.

**Origin check.** `do_POST` first compares the `Origin` header with `ORIGINS`,
`http://<address>:7655` for each `BIND` address plus `localhost`, and answers
403 on a missing or different value. This stops other web pages in your browser
from steering sessions. `Host` is not used: with DNS rebinding the attacker's
domain resolves to the board's address, the browser sends a matching `Host`, and
the check would pass. `Origin` still names the attacker's domain. A hostname such
as a tailnet name is allowed only when it is listed in `HERDR_DASH_BIND`.
Browsers send `Origin` on POSTs, but a client that omits it (curl, a launcher)
gets 403.

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
Card titles, folder names, hosts and keys come from herdr and the page puts them
in `innerHTML` through `esc()`. A script injected through a pane title would
pass the `Origin` check and could steer every session, so every server value
goes through `esc()`, including the icon fields. The panel and its recap
use `textContent`.
Clicking a card POSTs `/focus/<key>`, which calls `agent.focus` and clears the card's `fresh` flag, and opens the full view for that card. The violet outline means *this card
changed status and you haven't looked at the pane since* — it clears either
on that click or when herdr reports the pane focused, so focusing a pane
directly in your terminal counts as seeing it.

**The full view** (`#panel`) is a sibling of `#board`, not part of it, and covers
it with `position:fixed; inset:0`. The SSE handler replaces `#board`'s
`innerHTML` on every push, so anything inside would lose the text you are
typing. The view is a flex column: a header (back button, the target's icon,
name and title, updated on each push from the new card data without touching
the input or the recap), the recap filling the middle, and a footer with a text
input (Enter sends to `/prompt` and clears on a 204) and buttons that POST
`/keys`. The recap is a `<div>` in the page font with `white-space: pre-wrap`,
fetched from `/read` on open and every 5s while open. It scrolls to the bottom
on a refresh only if it was already at the bottom, so you can read further up.
`#panel[hidden]` needs its own `display:none` rule, because `display:flex` on
`#panel` would otherwise override the `hidden` attribute. While the view is
open `body` gets `overflow:hidden` so the board does not scroll behind it.

**History.** Opening a view calls `history.pushState({card: key}, '')`, so the
browser back button and the phone back gesture close it. `popstate` does the
closing. The back arrow and the keyboard Esc key call `history.back()` while
`pushed` is set and clear `pushed` first, so a second press before `popstate`
fires cannot pop an entry that belongs to the previous page. Esc closes the view
even when the text input has focus, unless `e.isComposing` is set, because
an IME uses Esc to cancel a composition. The on-screen `esc` button still
sends Esc to the agent. Pressing the browser's forward button after going back
does not reopen the view.

Opening a card other than the last one opened clears the input and recap, so
text typed for one agent is never sent to another. Reopening the same card keeps
the text: `last` remembers the card across a close, while `target` is null when
no view is open and makes in-flight `/read` and `/keys` responses for a closed
view get dropped.

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
