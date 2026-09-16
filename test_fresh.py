"""Self-check for the fresh flag. Run: python3 test_fresh.py"""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "dash", pathlib.Path(__file__).parent / "herdr-dash.py")
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)


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

print("ok")
