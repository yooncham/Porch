"""The home page at / and the Pages menu every page shares."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

DASHBOARD = Path(__file__).parents[1] / "porch_hub" / "dashboard"
PAGES = ["/dashboard", "/digital-twin", "/home-3d", "/stage"]


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
