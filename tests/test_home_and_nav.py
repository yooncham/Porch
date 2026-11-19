"""The home page at / and the Pages menu every page shares."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

DASHBOARD = Path(__file__).parents[1] / "porch_hub" / "dashboard"
PAGES = ["/dashboard", "/digital-twin", "/home-3d", "/porch", "/flow", "/live", "/stage"]


def client():
    return TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings()))


def test_home_page_is_served_at_the_root():
    with client() as c:
        home = c.get("/", follow_redirects=False)
    assert home.status_code == 200 and home.headers["cache-control"] == "no-store"
    assert "<title>Porch Home</title>" in home.text and "<porch-nav>" in home.text


def test_the_menu_script_is_served_and_lists_only_pages_that_exist():
    script = (DASHBOARD / "nav.js").read_text()
    hrefs = re.findall(r'href: "(/[^"]*)"', script)
    assert hrefs == ["/", *PAGES, "/docs"]
    with client() as c:
        served = c.get("/nav.js")
        statuses = {href: c.get(href).status_code for href in hrefs}
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/javascript")
    assert all(code == 200 for code in statuses.values()), statuses


def test_every_page_carries_the_menu():
    with client() as c:
        for path in PAGES:
            page = c.get(path).text
            assert '<script src="/nav.js" defer></script>' in page, path
            assert page.count("<porch-nav></porch-nav>") == 1, path


def test_the_menu_shows_family_protection_from_the_hub():
    script = (DASHBOARD / "nav.js").read_text()
    assert 'fetch("/protection"' in script
    assert "FAMILY PROTECTION ON" in script and "CONTROL WINDOW OPEN" in script
    # Device names come from the hub: they are placed as text, not built into markup.
    assert "pill.innerHTML" not in script
    with client() as c:
        assert c.get("/protection").json()["enforced"] is False  # the demo hub: the menu shows nothing


def test_the_menu_lets_the_owner_open_change_and_close_a_control_window():
    script = (DASHBOARD / "nav.js").read_text()
    for text in ['"/protection/window"', '"/protection/close"', 'pin.type = "password"', "Open control window",
                 "Close now and protect", "Change devices or time", "openProtectionPanel(deviceIds = [])"]:
        assert text in script, text
    # The PIN leaves the page as soon as it is read, and it is never kept anywhere.
    assert 'pinInput.value = "";' in script
    assert "localStorage" not in script and "sessionStorage" not in script


def test_the_menu_says_which_devices_the_owner_always_allows():
    """A standing choice must be visible to everyone at home: on the label and in its panel."""
    script = (DASHBOARD / "nav.js").read_text()
    assert "FAMILY PROTECTION ON · ${always.length} ALWAYS ALLOWED" in script
    assert "Always allowed, with no window:" in script and "The owner sets this on the hub's computer." in script
    assert script.count("this.alwaysNote()") >= 2  # in the panel with a window open, and without one
    # The page only shows it. Nothing in the page can change it.
    assert "/protection/always" not in script


def test_the_window_panel_stays_in_view_on_a_scrolled_page_and_names_its_commands():
    script = (DASHBOARD / "nav.js").read_text()
    # A page scrolled past the label (a device's refusal, lower down) still gets the panel on
    # screen, and closing it does not jump the page back to the top.
    assert "Math.max(12, at.bottom + 8)" in script
    assert "this.opener = document.activeElement" in script and "opener.focus({ preventScroll: true })" in script
    # A window for a lamp and a TV covers four commands: they read as a list, not "and ... and".
    assert "listed(state.commands)" in script and "listed(this.protectionState.commands)" in script
    assert '.join(" and ")' not in script


@pytest.mark.parametrize("page", ["index.html", "digital-twin.html", "home-3d.html", "stage.html"])
def test_every_page_that_sends_or_shows_commands_offers_the_owners_window_on_a_refusal(page):
    text = (DASHBOARD / page).read_text()
    assert "Family protection" in text and "openProtectionPanel" in text


def test_the_home_page_follows_the_homes_layout_and_family_protection():
    home = (DASHBOARD / "home.html").read_text()
    assert 'fetch("/home/layout")' in home and "porch:protection" in home
    assert 'id="protection-step" hidden' in home  # shown only on a hub that enforces family protection
    assert "Google Home and Ring" in home


def test_the_how_porch_works_page_shows_agents_rules_people_and_devices():
    page = (DASHBOARD / "porch.html").read_text()
    script = page.split("<script>")[-1]
    for source in ('"/household/agents"', '"/household"', '"/household/presence"', '"/household/approvals"',
                   '"/home/places"', '"/protection"', '"/online"', "/ws/events", "8000/ws/stage"):
        assert source in script, source
    # The rules in the order Porch checks them, the room rule among them, in words a visitor reads.
    assert page.index("Who is asking") < page.index("What it may control") < page.index("Who is in the room") \
        < page.index("Family protection.") < page.index("Read back")
    assert "Set by hand" in page and "that person's PIN, or the owner's" in page
    # PINs leave the page as soon as they are read, and nothing from the hub is written as markup.
    assert script.count('.value = "";') >= 2 and "innerHTML" not in script
