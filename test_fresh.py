"""Self-check for the fresh flag. Run: python3 test_fresh.py"""
import importlib.util
import os
import pathlib

os.environ["HERDR_DASH_BIND"] = "100.64.0.7"
spec = importlib.util.spec_from_file_location(
    "dash", pathlib.Path(__file__).parent / "herdr-dash.py")
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

# Without it iOS lays the page out at 980px and the narrow layout never applies.
assert '<meta name=viewport content="width=device-width,initial-scale=1">' in dash.PAGE
assert dash.BIND == ["127.0.0.1", "100.64.0.7"], "localhost always, plus HERDR_DASH_BIND"


def pane(status, focused=False):
    return {"pane_id": "p1", "agent": "claude", "agent_status": status,
            "terminal_title_stripped": "fix the parser", "cwd": "/x/repo",
            "focused": focused}


def is_fresh():
    return dash.panes["local:p1"]["fresh"]


dash.absorb("local", pane("working"))
assert not is_fresh(), "a first sighting is not a change"
dash.absorb("local", pane("blocked"))
assert is_fresh(), "a status change lights the card"
dash.absorb("local", pane("blocked"))
assert is_fresh(), "stays lit while nothing changes"
dash.absorb("local", pane("blocked", focused=True))
assert not is_fresh(), "focusing the pane clears it"
dash.absorb("local", pane("done", focused=True))
assert not is_fresh(), "a change while focused was already seen"
dash.absorb("local", pane("idle"))
assert is_fresh()
dash.mark_seen("local:p1")
assert not is_fresh(), "clicking the card clears it"
# A pane that outlives its agent must not lend its title to the next one.
dash.mark_gone("local:p1")
dash.forget("local:p1")
assert "local:p1" not in dash.summaries, "forget() must drop the cached summary too"

# Two hosts can mint the same pane_id independently; the key must keep them apart.
dash.absorb("local", pane("working"))
dash.absorb("wyse", pane("working"))
assert {"local:p1", "wyse:p1"} <= dash.panes.keys(), "same pane_id on two hosts is two cards"

# A session titled with a leading dot stays off the board until it needs you.
hidden = {**pane("idle"), "pane_id": "p2", "terminal_title_stripped": ".diff"}
dash.absorb("local", hidden)
assert "local:p2" not in [c["key"] for c in dash.board()], "dot sessions are hidden"
dash.absorb("local", {**hidden, "agent_status": "blocked"})
assert "local:p2" in [c["key"] for c in dash.board()], "unless they are blocked"

# A worktree folder with a leading dot is normal work, not hidden: worktrees are created that way.
wt = {**pane("idle"), "pane_id": "p3", "terminal_title_stripped": "BH-1 thing", "cwd": "/x/.bh-1-thing"}
dash.absorb("local", wt)
assert "local:p3" in [c["key"] for c in dash.board()], "dot worktree folders show"

# Only the board's own addresses may POST; any other page in the browser must get a 403.
for origin in ("http://127.0.0.1:7655", "http://100.64.0.7:7655", "http://localhost:7655"):
    assert dash.allowed_origin(origin), origin
for origin in (None, "http://evil.example:7655", "http://127.0.0.1:9999", "null"):
    assert not dash.allowed_origin(origin), origin

# Herdr data goes into innerHTML, so the page must escape it.
assert "const esc" in dash.PAGE and "esc(c.title" in dash.PAGE

# Every live card gets its own animal and colour; a forgotten card frees its pair.
dash.panes.clear()
dash.icons.clear()
for n in range(7):
    dash.absorb("local", {**pane("idle"), "pane_id": f"q{n}"})
first = [dash.panes[f"local:q{n}"] for n in range(7)]
assert len({c["icon"] for c in first}) == 7, "the first 7 cards differ in animal"
assert len({c["icon_bg"] for c in first}) == 7, "the first 7 cards differ in colour"
assert all(c["icon_name"] for c in first), "a card carries its hover name"
dash.absorb("local", {**pane("working"), "pane_id": "q0"})
assert dash.panes["local:q0"]["icon"] == first[0]["icon"], "a card keeps its icon while it lives"
dash.mark_gone("local:q3")
dash.forget("local:q3")
dash.absorb("local", {**pane("idle"), "pane_id": "q9"})
assert dash.panes["local:q9"]["icon_name"] == first[3]["icon_name"], "the next card reuses the freed pair"

# The recap drops Claude's input box and status footer, which sit below the last two rule lines.
RULE = "─" * 32
tail = f"""\
✻ Crunched for 5m 51s · done 14:15

{RULE}
❯
{RULE}
  7ba1a27e  ⎇ steer-sessions  Opus 5.5 (medium)  ctx:133k/1000k (13%)
  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent
"""
body = "Done: the parser is fixed.\n\n  - it keeps the tail\n"
cleaned = dash.clean_recap(body + tail)
assert cleaned.endswith("✻ Crunched for 5m 51s · done 14:15"), cleaned
assert "❯" not in cleaned and "ctx:" not in cleaned and "auto mode" not in cleaned
assert cleaned.startswith("Done: the parser is fixed."), "text above the box stays"
# Claude puts the session name in the top rule, so that rule is not made of box characters only.
named = f"{body}\n{RULE} session name ─\n❯\n{RULE}\n  7ba1a27e  Opus 5.5\n"
assert dash.clean_recap(named) == dash.clean_recap(body), "a named top rule starts the cut too"
# With one rule line there is no box to cut: the rule goes, the rest stays.
assert dash.clean_recap(f"one\n{RULE}\ntwo\n") == "one\ntwo"
# Another agent's text is not cut when it has no rule lines.
assert dash.clean_recap("\n\nfirst\n\n\n   \nsecond\n\n") == "first\n\nsecond"
assert dash.clean_recap("") == ""
assert dash.clean_recap(RULE) == ""

print("ok")
