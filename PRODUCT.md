# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is the author. They run many coding agents at once in
herdr, often in git worktrees whose sessions look alike, and keep the board
open beside their work. Other herdr users can run the same tool from the
public repository, but their needs do not drive design decisions.

## Product Purpose

herdr-dash shows every herdr agent pane as a card on a kanban board with four
columns: waiting for you, in progress, idle and done. It exists so the user
does not lose track of agents across many terminal panes.

Success means two things when the user opens the board:

1. The user sees at a glance which agents are waiting on them (blocked or done).
2. The user tells similar threads apart, by title, folder and a per-card
   animal-and-colour icon.

## Operating Context

- The user views the board in hello-browser, opened by the `herdr-board`
  launcher, often as a narrow side pane, and on a phone over a private network
  (NetBird or Tailscale) when `HERDR_DASH_BIND` is set.
- The board reads herdr's unix socket and pushes updates to the page over
  server-sent events. It can also show agents on remote hosts.
- Clicking a card focuses its pane in herdr and opens a full-screen card view
  with the agent's last prompt, its latest reply and older output, plus a
  prompt box, `esc` and `enter` buttons, and one button per option when the
  agent shows a menu.

## Capabilities and Constraints

- `herdr-dash.py` stays one Python file that uses only the standard library,
  with no install or build step.
- The page makes no external requests: no web fonts, no CDNs. Fonts come from
  the system (Inter, with system fallbacks).
- The board has no login. Anyone who can reach a bound address may read output
  and steer agents. Protection is the Host and Origin checks only, which stop
  other websites, not other programs.
- Answering an agent from the board and reading its output comfortably are
  supported capabilities, but the user did not name them as the board's
  primary success measures.
- Phone width and narrow-pane layouts exist today; the user did not confirm
  them as binding constraints.

## Evidence on Hand

- Screenshots of the board: `docs/board-narrow.png`, `docs/board-wide.png`.
- No testimonials, user counts or benchmarks exist. Do not invent them.

## Product Principles

1. Waiting agents come first: anything that hides or delays "who needs me"
   is a regression.
2. Each card must be distinguishable from its neighbours without reading it in
   full.
3. Small and self-contained beats featureful: a change that needs a
   dependency, a build step or an external request does not ship.
4. The terminal stays the source of truth; the board summarises and steers it,
   and never pretends to be a full terminal.
