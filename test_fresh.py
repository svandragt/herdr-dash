"""Self-check for the fresh flag. Run: python3 test_fresh.py"""
import importlib.util
import os
import pathlib

os.environ["HERDR_DASH_BIND"] = "100.64.0.7"
os.environ["HERDR_SOCKET_PATH"] = "/nonexistent/herdr.sock"  # a handler test must never reach the real herdr
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

# The Host header must also be one of the board's own addresses: a DNS-rebinding page sends its own hostname.
for host in ("127.0.0.1:7655", "100.64.0.7:7655", "localhost:7655"):
    assert dash.allowed_host(host), host
for host in (None, "evil.example:7655", "127.0.0.1", "127.0.0.1:9999", "localhost", "100.64.0.8:7655"):
    assert not dash.allowed_host(host), host

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
# The cut needs Claude's input box: the first line between the last two rules starts with the prompt.
# The prompt carries typed text or a placeholder, and Claude follows it with a no-break space.
typed = f"{body}\n{RULE}\n❯\u00a0Try \"how do I log an error?\"\n{RULE}\n  7ba1a27e  Opus 5.5\n"
assert dash.clean_recap(typed) == dash.clean_recap(body), "a prompt line with text is still the input box"
# A table has rules but no input box, so its rows and its border lines (a `│` line next to them) stay.
table = "┌───┬───┐\n│ a │ b │\n├───┼───┤\n│ 1 │ 2 │\n└───┴───┘\n"
assert dash.clean_recap(table) == table.strip("\n"), dash.clean_recap(table)
# A box at the start must not empty the recap.
box = "╭──────────╮\n│ - old    │\n│ + new    │\n╰──────────╯\nthe edit is shown above\n"
assert dash.clean_recap(box) == box.strip("\n"), dash.clean_recap(box)
# A real Claude table keeps its four border lines; the lone `────` separator below it goes.
fixture = """\
Here is an example table:
┌────────────┬──────────────────────────────┬────────────┐
│   Commit   │         What it does         │   Tests    │
├────────────┼──────────────────────────────┼────────────┤
│ 6fd6c03    │ Shows sessions in dot        │ 1 new      │
│            │ worktree folders again       │ assert     │
├────────────┼──────────────────────────────┼────────────┤
│ Focus      │ Puts the cursor in the box   │ 1 assert   │
└────────────┴──────────────────────────────┴────────────┘

────────────────────────────────
Done.
"""
want = fixture.replace("└────────────┴──────────────────────────────┴────────────┘\n\n────────────────────────────────\n",
                       "└────────────┴──────────────────────────────┴────────────┘\n\n")
assert dash.clean_recap(fixture) == want.strip("\n"), dash.clean_recap(fixture)
# A permission dialog has no input box, so the question and options stay.
dialog = """\
╭──────────────────────────────╮
│ Bash command                 │
│   git push origin main       │
│ Do you want to proceed?      │
│ ❯ 1. Yes                     │
│   2. No                      │
╰──────────────────────────────╯
"""
asked_to = dash.clean_recap(dialog)
assert "Do you want to proceed?" in asked_to and "❯ 1. Yes" in asked_to and "2. No" in asked_to, asked_to
# A dialog between two rules has no prompt line first, so it is not cut either.
ruled = f"{body}\n{RULE}\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n{RULE}\n"
assert "2. No" in dash.clean_recap(ruled), dash.clean_recap(ruled)

# The recap splits into the last prompt, the reply to it without tool noise, and the older output.
sample = """\
❯ earlier question

● earlier answer

❯ please fix the parser
  and keep the tests green

● I'll start with the failing case.

● Bash(python3 test_fresh.py)
  ⎿  Traceback (most recent call last):
       File "test_fresh.py", line 9
     AssertionError

● Bash(git commit -q -F - <<'EOF'
      Fix the parser
      EOF
      )
  ⎿  [steer 1a2b3c4] Fix the parser

  Ran 2 shell commands

● The parser is fixed. Two things changed:
  - it keeps the tail
  - it trims blanks

✻ Crunched for 5m 51s · done 14:15

※ recap: the parser is fixed. Next, cut a release.
  It needs a tag.

◯ Goal not yet met… continuing
"""
split = dash.split_recap(dash.clean_recap(sample + tail))
assert split["asked"] == "please fix the parser\nand keep the tests green", split
assert split["reply"] == ("I'll start with the failing case.\n\nThe parser is fixed. Two things changed:\n"
                          "- it keeps the tail\n- it trims blanks"), split["reply"]
assert split["earlier"] == "❯ earlier question\n\n● earlier answer", split["earlier"]
# A table in the reply stays, one row per line, with its border lines.
tabled = "❯ compare them\n\n● Here is the table:\n  ┌───┬───┐\n  │ a │ b │\n  ├───┼───┤\n  │ 1 │ 2 │\n  └───┴───┘\n  Done.\n"
assert dash.split_recap(dash.clean_recap(tabled))["reply"] == "Here is the table:\n┌───┬───┐\n│ a │ b │\n├───┼───┤\n│ 1 │ 2 │\n└───┴───┘\nDone."
# Without a prompt (another agent, a fresh session) the whole text is the reply.
assert dash.split_recap("just output\n\nmore") == {"asked": "", "reply": "just output\n\nmore", "earlier": ""}
assert dash.split_recap("") == {"asked": "", "reply": "", "earlier": ""}
# A prompt with nothing after it has an empty reply; a prompt can hold a blank line.
assert dash.split_recap("● old\n\n❯ /clear") == {"asked": "/clear", "reply": "", "earlier": "● old"}
assert dash.split_recap("❯ first part\n\n  second part\n\n● ok")["asked"] == "first part\n\nsecond part"
# A menu option is not a prompt, and a question after a tool call is not tool noise.
menu = "❯ clean up\n\n● Bash(rm -rf build)\n  ⎿  Running…\n Do you want to proceed?\n\n❯ 1. Yes\n  2. No"
got = dash.split_recap(menu)
assert got["asked"] == "clean up" and "Bash(" not in got["reply"], got
assert "Do you want to proceed?" in got["reply"] and "1. Yes" in got["reply"] and "2. No" in got["reply"], got
# Prose with a bracket is not a tool call: the name must touch the bracket.
assert dash.split_recap("❯ q\n\n● Yes (mostly), it works.")["reply"] == "Yes (mostly), it works."

# A working agent's history can't be scrolled, so herdr refuses a long read; fall back to the visible screen.
asked = []
def fake_call(host, method, params):
    asked.append(params["source"])
    if params["source"] == "recent_unwrapped":
        return {"error": {"code": "agent_not_idle", "message": "busy"}}
    return {"result": {"read": {"text": "on screen"}}}
real_call, dash.call = dash.call, fake_call
assert dash.read_pane("local", "p1") == "on screen", "a working agent shows its visible screen"
assert asked == ["recent_unwrapped", "visible"], asked
dash.call = real_call

# A request body is read before it is checked, so its declared length must be sane.
assert dash.body_length(None) == 0 and dash.body_length("12") == 12 and dash.body_length("65536") == 65536
for bad in ("-1", "65537", "x", "1e3"):
    try:
        dash.body_length(bad)
    except ValueError:
        continue
    raise AssertionError("Content-Length %r must be rejected" % bad)

# The handler: a foreign Host is a 403 before anything else, and /read answers JSON.
import http.client
import json
import threading


def fake_read(host, method, params):
    return {"result": {"read": {"text": "❯ hi\n\n● hello"}}}


dash.call = fake_read
server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def request(method, path, host="127.0.0.1:7655", origin=None):
    c = http.client.HTTPConnection("127.0.0.1", server.server_port)
    c.putrequest(method, path, skip_host=True)  # skip_host: the test sends no Host or a chosen one
    if host:
        c.putheader("Host", host)
    if origin:
        c.putheader("Origin", origin)
    if method == "POST":
        c.putheader("Content-Length", "0")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


assert request("GET", "/")[0].status == 200
for host in ("evil.example:7655", "127.0.0.1", None):
    for path in ("/", "/events"):
        assert request("GET", path, host)[0].status == 403, (host, path)
    # A matching Origin does not help a foreign Host: that is the DNS-rebinding case.
    assert request("POST", "/read/local:p1", host, "http://127.0.0.1:7655")[0].status == 403, host
assert request("POST", "/read/local:p1", origin="http://evil.example:7655")[0].status == 403
r, body = request("POST", "/read/local:p1", "localhost:7655", "http://localhost:7655")
assert r.status == 200 and r.getheader("Content-Type") == "application/json", (r.status, r.getheaders())
assert json.loads(body) == {"asked": "hi", "reply": "hello", "earlier": ""}, body
server.shutdown()
dash.call = real_call

# Opening a card puts the cursor in the prompt box, except on touch screens where the keyboard would cover the reply.
assert "matchMedia('(pointer: fine)').matches) $('ptext').focus()" in dash.PAGE

# A busy pane's screen often has no prompt left on it; its tool calls and status lines still go.
busy = dash.split_recap("I corrected the Test plan.\n\n✻ Waiting for 1 background agent to finish\n\n"
                        "› Message from @qa (ctrl+o to expand)\n\n● Updating PR body\n  ⎿ \u00a0$ gh pr edit 1209\n"
                        "     --body-file x.md\n\n✢ Orbiting… (19s)")
assert busy == {"asked": "", "reply": "I corrected the Test plan.", "earlier": ""}, busy

print("ok")
