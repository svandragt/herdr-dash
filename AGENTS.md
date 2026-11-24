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
| `/keys/<key>` | space-separated key names from `KEYS` (the digits `1`–`9`, `esc`, `enter`) | `agent.send_keys` with `target` and `keys` |
| `/read/<key>` | none | `agent.read` with `source: recent_unwrapped` and `lines: 60`, or `source: visible` when herdr answers `agent_not_idle` (a working agent's history can be read only while it is idle); answers `split_recap(clean_recap(text))` as `application/json`: `{"asked", "reply", "earlier", "options"}` |

An empty prompt, a key outside `KEYS`, or a `Content-Length` that is not a number,
is negative or is over 64 KiB (`MAX_BODY`) is a 400. `body_length()` checks the length
before the handler reads the body, because `rfile.read(-1)` and a length larger than
the data both block until the client closes the connection. A herdr call that raises or
answers with an error is a 502. `/read` is a POST, so it needs a matching `Origin`
as well as `Host`; the `Host` check alone already stops a DNS-rebinding page from
reading terminal output. The parameter
names come from `herdr api schema --json` (`AgentPromptParams`,
`AgentSendKeysParams`, `AgentReadParams`); the read text is
`result.read.text` in the response.

**`clean_recap(text)`** turns the pane text into a readable recap. A rule line
is a line that is only box-drawing characters (U+2500 to U+257F), or a run of
`─` with a label after it: Claude puts the session name in the input box's top
rule, so that rule has letters. The function cuts only when the last two rule
lines enclose Claude's input box: the first non-blank line between them starts
with `❯`, the prompt, which can carry typed text or a placeholder. It then cuts
everything from the second-to-last rule, which removes the box and the status
footer below it. Any other pair of rules is not cut: a table, a `╭──╮` box such
as a diff, or another agent's box keeps its rows. A permission dialog box has
no prompt line, so its question and options stay, and so does the footer below it. In
every case the function drops the remaining rule lines, except a rule line whose
nearest non-blank line above or below is a table row: it starts with `│` (after
spaces) and has a second `│` or `┃`. That rule is a table's `┌─┬─┐`, `├─┼─┤` or
`└─┴─┘` border, and it stays. Claude's menu question has one `│` in its margin,
so a rule beside it goes. It then collapses runs of
blank lines to one and trims blank lines at both ends.
`recent_unwrapped` joins the terminal's soft wraps, so the page wraps lines to
its own width.

**`split_recap(text)`** splits the cleaned text for the page. `asked` is the last
Claude prompt: a line starting with `❯` and text, plus the indented lines that
follow it, blank ones included, minus the `❯`. A `❯ 1. Yes` line is a menu
option, not a prompt. `reply` is everything after it without the lines that are
not the assistant's words:

- A `● Name(` line, or a `● ` line that a `⎿` line follows directly, is a tool
  call. It and the indented lines under it (the arguments and the `⎿` results)
  are dropped, until a line that is not indented by at least two spaces.
- A line that starts with a spinner or status mark (`✻ ✶ ✢ ✳ ✽ * ·`, `✦` tips,
  `※` recaps, `◯` goal notices, `›` message notices) is dropped with its
  indented lines.
- A summary such as `Ran 3 shell commands` or `Read 2 files` is dropped.
- The rest stays: `● ` is removed from the first line of each block, and up to
  two spaces of indent from the other lines, so tables and lists line up.

`earlier` is the cleaned text before that prompt, unchanged. Without a prompt
(another agent, a fresh session, or a busy pane whose prompt has scrolled off
its visible screen) `asked` and `earlier` are empty and `reply` is the whole
text, filtered the same way by `reply_text()`. The split keeps a line when in doubt: a tool result line indented
by less than two spaces comes through as text, and so does a `● ` title that has
no `Name(` and no `⎿` line directly under it.

**Host and Origin checks.** `do_GET` and `do_POST` first compare the `Host`
header with `HOSTS`, `<address>:7655` for each `BIND` address plus `localhost`,
and answer 403 on a missing or different value. This blocks DNS rebinding: the
attacker's domain resolves to the board's address, but the browser sends the
attacker's hostname in `Host`, and that is not in the list. `do_POST` then
compares the `Origin` header with `ORIGINS`, `http://` plus each entry of
`HOSTS`, and answers 403 on a missing or different value. This blocks a page on
another site that reaches the real address: the browser sends that page's own
site as `Origin`. A hostname such as a tailnet name is allowed only when it is
listed in `HERDR_DASH_BIND`. Browsers send `Origin` on POSTs, but a client that
omits it (curl, a launcher) gets 403. The client sets both headers, so anything
that can reach the port can send them: the checks stop other websites, not other
programs.

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
input and buttons that POST `/keys`. Enter sends the text to `/prompt`. It clears
the input before the request, so a second Enter has nothing to send, and restores
the text if the request fails (not a 204, or no answer) while the input is still
empty and the same card is the last one opened. A `sending` flag also blocks any
Enter while a `/prompt` is pending, and a held-down Enter (`e.repeat`) is ignored.
Opening a card focuses the prompt input only when `matchMedia("(pointer: fine)")` matches, because on a touch screen the keyboard would cover the reply. The recap is fetched from `/read` on open and every 5s while open. It shows
"You asked" and "Latest reply" on top and an "Earlier output (N lines)"
`<details>` below; a part that is empty is hidden. The three elements are in the
page from the start and a refresh only replaces the text inside them, so the
`<details>` keeps its open state. A refresh whose JSON equals the last one does
nothing, which keeps the reader's text selection. Each text part is cut into
groups in `tableLines()`.
A table row starts with `│ ┌ ├ └ ╭ ╰ ┃` (after leading
spaces) and has a second `│` or `┃`; a line made only of box-drawing characters
is a table line when a row sits directly above or below it. So a wrapped prose
line that starts with `┃).` and Claude's one-bar menu question stay prose.
Consecutive table lines become one `<pre class=tbl>` (`white-space: pre`,
scrolls sideways, 14px monospace). The other lines become prose at 16px, line
height 1.6 and at most 70ch wide. Your prompt is muted (`#9aa3ad`), the latest
reply is brightest (`#d0d6dd`), and earlier output is 14px grey, so the reply
reads first. The "Earlier output" toggle uses the same 11px label style as
"You asked". Each prose line is one block, with its leading spaces as `ch` padding, so
an indented line that wraps continues under its own start. `color-scheme: dark`
gives the sideways scrollbar a dark track. The recap scrolls to the bottom on a refresh only if it was already at
the bottom, so you can read further up.
`#panel[hidden]` needs its own `display:none` rule, because `display:flex` on
`#panel` would otherwise override the `hidden` attribute. While the view is
open `body` gets `overflow:hidden` so the board does not scroll behind it.

**Menus.** `options` lists the option numbers of a Claude menu at the end of the reply
(`menu_options()`): numbering restarts at each `1.` line, and the run counts
only if one option carries Claude's `❯` selection mark, so a numbered list in
prose gives `[]`. The page builds one button per option (digits up to 9)
between `esc` and `enter`, and `optionLines()` makes the menu's own option lines
tappable, starting from the last `1.` line. A click that ends a text selection
sends nothing. A key or prompt that herdr accepts gets the `sent` class for
700 ms.

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
no view is open and makes in-flight `/read`, `/keys` and `/prompt` responses for a
closed view get dropped.

**The error line** (`#perr`) shows the last failed request. `act()` tags it with the
route that wrote it. A successful `/keys` or `/prompt` clears it. A successful `/read`
clears it only when a failed `/read` wrote it, so the 5s refresh never wipes a
prompt's error.

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
