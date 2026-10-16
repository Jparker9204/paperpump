"""Cache-busting headers, versioned assets, embedded feed on the Trade page, phone nav labels."""
import re

import pytest
from fastapi.testclient import TestClient

from paperpump import web
from paperpump.db import DB
from tests.test_web import FakePrices, cfg, nosleep


@pytest.fixture
def client():
    return TestClient(web.create_app(cfg(), DB(), FakePrices(), sleep=nosleep), base_url="https://testserver")


@pytest.mark.parametrize("path", ["/", "/discover", "/portfolio", "/history", "/leaderboard"])
def test_html_is_no_cache_and_assets_are_versioned(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "no-cache" in r.headers["cache-control"]
    refs = re.findall(r'/static/[\w.-]+\.(?:js|css)[^"]*', r.text)
    assert refs and all(ref.endswith("?v=" + web.ASSET_VERSION) for ref in refs)


def test_static_cache_headers(client):
    assert "immutable" in client.get(f"/static/app.js?v={web.ASSET_VERSION}").headers["cache-control"]
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js?v=old").headers["cache-control"] == "no-cache"


def test_version_changes_with_content(tmp_path, monkeypatch):
    (tmp_path / "a.js").write_text("1")
    monkeypatch.setattr(web, "STATIC", tmp_path)
    v1 = web.asset_version()
    (tmp_path / "a.js").write_text("2")
    assert web.asset_version() != v1


def test_trade_page_embeds_feed_with_back_button(client):
    html = client.get("/").text
    for needle in ('id="feed"', "/static/discover.js", "mountDiscover(", 'id="backFeed"', "nocoin"):
        assert needle in html
    js = client.get("/static/discover.js").text
    assert "/api/discover" in js and "root.hidden" in js  # no polling while collapsed


def test_nav_has_discover_and_short_phone_labels(client):
    js = client.get("/static/app.js").text
    assert '["/discover", "Discover"]' in js and "Ranks" in js
    css = client.get("/static/app.css").text
    assert "max-width:480px" in css and ".links .sn" in css


def nav_links(html):
    nav = re.search(r'<div class="links">(.*?)</div>', html, re.S).group(1)
    return re.findall(r'<a href="([^"]+)"', nav), re.findall(r'<a href="([^"]+)" class="on"', nav)


def test_every_page_has_the_same_server_rendered_nav(client):
    seen = {}
    for path in web.PAGES:
        html = client.get(path).text
        assert html.count('class="nav"') == 1 and "<!--NAV-->" not in html
        links, active = nav_links(html)
        seen[path] = links
        assert active == [path]  # exactly one highlighted tab: this page
        assert "Ranks" in html and "Trades" in html and "/discover" in links
    assert len({tuple(v) for v in seen.values()}) == 1
    assert seen["/"] == [h for h, _, _ in web.NAV_LINKS]
