"""/live?record: a clean frame for recording the demo video.

No menu, title or view links: a slim PORCH bar with the family protection and "waiting for a yes"
labels, the 3D home (or the floor plan) and the Live flow without their own headers and captions,
and one large caption across the bottom that says, sentence by sentence, what the Live flow says.
T shows a title card, E an end card, F full screen.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

PAGES = Path(__file__).parents[1] / "porch_hub" / "dashboard"
LIVE = (PAGES / "live.html").read_text()
FLOW = (PAGES / "flow.html").read_text()
HOME_3D = (PAGES / "home-3d.html").read_text()
TWIN = (PAGES / "digital-twin.html").read_text()
NAV = (PAGES / "nav.js").read_text()


def test_the_live_page_has_a_recording_mode_with_one_large_caption_and_cards():
    script = LIVE.split("<script>")[-1]
    assert 'const record = params.has("record");' in script and 'document.body.classList.add("record")' in script
    assert 'id="strip"' in LIVE and 'id="title-card"' in LIVE and 'id="end-card"' in LIVE
    assert 'if (key === "t") toggleCard("title-card")' in script and 'else if (key === "e") toggleCard("end-card")' in script
    assert "requestFullscreen" in script
    assert "Agents ask. Porch checks. You decide." in script
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script  # agents' words are text only


def test_the_caption_takes_only_the_flows_own_words_from_this_page():
    script = LIVE.split("<script>")[-1]
    assert "event.origin !== location.origin || event.source !== $(\"flow\").contentWindow" in script
    assert 'data.type === "porch-caption"' in script
    tell = FLOW.split("function tellLive(", 1)[1].split("\n}", 1)[0]
    assert "if (!RECORD || window.parent === window) return;" in tell
    assert "location.origin);" in tell  # never sent to another site


def test_while_recording_the_frames_drop_their_own_headers_captions_and_keys():
    assert 'html.record header.top, html.record nav.recent, html.record section.caption' in FLOW
    assert "if (RECORD) return;  // the Live page around it owns the keys while recording" in FLOW
    assert ".filter((a) => !RECORD || a.client)" in FLOW  # only the agents people use
    assert "html.record header, html.record #porch-caption { display: none !important; }" in HOME_3D
    assert "html.record header.top{display:none}" in TWIN
    for page in (FLOW, HOME_3D, TWIN):
        assert 'has("record")' in page
    assert 'this.recording = new URLSearchParams(location.search).has("record");' in NAV
    assert "if (this.recording) this.button.hidden = true;" in NAV


def test_the_home_plays_slower_while_recording_so_it_keeps_pace_with_the_flow():
    assert 'if (record) homePass.set("pace", String(Number(params.get("pace")) * 1.8));' in LIVE
    assert 'if (record && !params.has("pace")) params.set("pace", "1.5");' in LIVE


def test_the_recording_page_is_served():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings())) as client:
        page = client.get("/live?record")
    assert page.status_code == 200 and 'id="strip"' in page.text and "/agents.js" in page.text


def test_a_recording_shows_no_device_response_times():
    """In recording mode the pages say a device was verified, not how many milliseconds it took: those
    times change as Porch improves. Every place a live page prints one is behind that switch, and
    outside recording mode nothing changes."""
    import re

    agents = (PAGES / "agents.js").read_text()
    pages = {"agents.js": agents, "flow.html": FLOW, "home-3d.html": HOME_3D, "digital-twin.html": TWIN,
             "nav.js": NAV}
    guards = ("SHOW_TIMES", "inMs(", "RECORD ?", "this.recording")
    printed = 0
    for name, text in pages.items():
        for line in text.splitlines():
            if re.search(r"latency(_ms)?\} ms", line):
                printed += 1
                assert any(g in line for g in guards), f"{name} prints a time outside the switch: {line.strip()}"
    assert printed >= 8  # the check found the places it is about
    assert 'const SHOW_TIMES = !new URLSearchParams(location.search).has("record");' in agents
    assert 'const inMs = (ms) => (!RECORD && ms != null ? ` in ${ms} ms` : "");' in FLOW
    for page in (HOME_3D, TWIN):
        assert 'const SHOW_TIMES = !document.documentElement.classList.contains("record");' in page
    assert 'this.recording = new URLSearchParams(location.search).has("record");' in NAV
