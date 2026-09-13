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
    return dash.panes["p1"]["fresh"]


dash.absorb(pane("working"))
assert not is_fresh(), "a first sighting is not a change"
dash.absorb(pane("blocked"))
assert is_fresh(), "a status change lights the card"
dash.absorb(pane("blocked"))
assert is_fresh(), "stays lit while nothing changes"
dash.absorb(pane("blocked", focused=True))
assert not is_fresh(), "focusing the pane clears it"
dash.absorb(pane("done", focused=True))
assert not is_fresh(), "a change while focused was already seen"
dash.absorb(pane("idle"))
assert is_fresh()
dash.mark_seen("p1")
assert not is_fresh(), "clicking the card clears it"
print("ok")
