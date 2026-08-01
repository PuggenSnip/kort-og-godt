"""Unit tests for Kort og Godt parsers, trend math and the verdict engine.

Parsers are tested against saved HTML/JSON fixtures in fixtures/synthetic/.
Real responses recorded by the app land in fixtures/live/ and get a smoke
test automatically once they exist.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import scanner
from scanner import Observation, ParsedPrice

FIX = Path(__file__).parent / "fixtures" / "synthetic"
LIVE = Path(__file__).parent / "fixtures" / "live"

SETTINGS = dict(scanner.DEFAULT_SETTINGS)


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Danish / USD price parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("1.999,95", 1999.95),
    ("1.999,95 DKK", 1999.95),
    ("1.879,95DKK", 1879.95),
    ("kr 634,95", 634.95),
    ("634,95 kr.", 634.95),
    ("599,-", 599.0),
    ("4.199,95", 4199.95),
    ("1.599", 1599.0),
    ("1097.99", 1097.99),
    ("52,95", 52.95),
    ("", None),
    ("ingen pris her", None),
])
def test_parse_danish_price(raw, expected):
    assert scanner.parse_danish_price(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("$1,234.56", 1234.56),
    ("$255.00", 255.0),
    ("155", 155.0),
    ("", None),
])
def test_parse_usd_price(raw, expected):
    assert scanner.parse_usd_price(raw) == expected


def test_extract_dk_price_ignores_pack_counts():
    text = "Pokemon Booster Box (36 packs) 1.879,95DKK På lager"
    assert scanner.extract_dk_price(text) == 1879.95


def test_extract_dk_price_kr_prefix():
    assert scanner.extract_dk_price("Pris: kr 1.499,95") == 1499.95


def test_fmt_dkk():
    assert scanner.fmt_dkk(1999.95) == "1.999,95 kr"
    assert scanner.fmt_dkk(1450, 0) == "1.450 kr"
    assert scanner.fmt_dkk(None) == "–"


def test_fx():
    assert scanner._to_dkk(100, "EUR", SETTINGS) == pytest.approx(746.0)
    assert scanner._to_dkk(100, "USD", SETTINGS) == pytest.approx(640.0)
    assert scanner._to_dkk(100, "DKK", SETTINGS) == 100


# ---------------------------------------------------------------------------
# Parsers vs fixtures
# ---------------------------------------------------------------------------

def test_shopify_product_js():
    p = scanner.parse_shopify_product_js(load("shopify_product.js.json"))
    assert p.error is None
    assert p.price == pytest.approx(1599.0)   # 159900 øre
    assert p.in_stock is True
    assert "Pitch Black" in p.title


def test_shopify_product_js_soldout():
    p = scanner.parse_shopify_product_js(load("shopify_product_soldout.js.json"))
    assert p.error is None
    assert p.price == pytest.approx(1299.0)
    assert p.in_stock is False


def test_shopify_product_js_garbage():
    assert scanner.parse_shopify_product_js("<html>not json</html>").error


def test_shopify_products_json_search():
    p = scanner.parse_shopify_products_json(
        load("shopify_products.json"),
        ["ascended", "heroes", "elite", "trainer"],
        exclude_words=["pack", "japansk"])
    assert p.error is None
    assert p.title == "Pokémon Ascended Heroes Elite Trainer Box"
    assert p.price == pytest.approx(1499.0)
    assert p.in_stock is True


def test_shopify_products_json_handle():
    p = scanner.parse_shopify_products_json(
        load("shopify_products.json"), [], handle="pitch-black-booster-box")
    assert p.error is None
    assert p.price == pytest.approx(1799.0)


def test_shopify_products_json_no_match():
    p = scanner.parse_shopify_products_json(
        load("shopify_products.json"), ["does", "not", "exist"])
    assert p.error


def test_kelz0r_search_display():
    p = scanner.parse_kelz0r(load("kelz0r_search.html"),
                             ["pitch", "black", "display"],
                             ["elite trainer", "sleeved"])
    assert p.error is None
    assert p.price == pytest.approx(1879.95)
    assert p.in_stock is True
    assert p.allocation_risk is True          # "Risiko for allokering"
    assert "Booster Display" in p.title


def test_kelz0r_search_etb_soldout():
    p = scanner.parse_kelz0r(load("kelz0r_search.html"),
                             ["pitch", "black", "elite", "trainer"])
    assert p.error is None
    assert p.price == pytest.approx(634.95)
    assert p.in_stock is False
    assert p.allocation_risk is False


def test_kelz0r_search_no_match():
    p = scanner.parse_kelz0r(load("kelz0r_search.html"), ["riftbound"])
    assert p.error


def test_kelz0r_product_page():
    p = scanner.parse_kelz0r_product(load("kelz0r_product.html"),
                                     ["riftbound", "origins"])
    assert p.error is None
    assert p.price == pytest.approx(1499.95)  # itemprop content, not nav 59,95
    assert p.in_stock is True                 # schema.org/InStock
    assert "Riftbound" in p.title


def test_kelz0r_product_soldout_allocation():
    p = scanner.parse_kelz0r_product(load("kelz0r_product_soldout.html"))
    assert p.error is None
    assert p.price == pytest.approx(1097.99)
    assert p.in_stock is False                # schema.org/OutOfStock
    assert p.allocation_risk is True


def test_kelz0r_product_wrong_page():
    p = scanner.parse_kelz0r_product(load("kelz0r_product.html"),
                                     ["pitch", "black"])
    assert p.error                            # title mismatch -> UNVERIFIED


def test_kelz0r_search_redirected_to_product_page():
    # A search URL that lands on a single product page still parses via
    # the microdata fallback.
    p = scanner.parse_kelz0r(load("kelz0r_product.html"),
                             ["riftbound", "origins"])
    assert p.error is None
    assert p.price == pytest.approx(1499.95)


def test_epicpanda():
    p = scanner.parse_epicpanda(load("epicpanda_product.html"))
    assert p.error is None
    assert p.price == pytest.approx(1999.95)
    assert p.in_stock is True
    assert "Pitch Black" in p.title


def test_epicpanda_itemprop_soldout():
    p = scanner.parse_epicpanda(load("epicpanda_itemprop.html"))
    assert p.error is None
    assert p.price == pytest.approx(1699.95)  # from itemprop content attr
    assert p.in_stock is False


def test_epicpanda_frontpage_redirect_is_error():
    # Unknown product URLs get HTTP 200 + the frontpage (basket says
    # '0,00 DKK') — must be an error, never a verified price of 0.
    p = scanner.parse_epicpanda(load("epicpanda_frontpage.html"))
    assert p.error
    assert p.price is None


class _RecordingTransport(httpx.BaseTransport):
    """MockTransport that records every request URL (per redirect hop)."""

    def __init__(self, routes):
        self.routes = routes            # dict: url -> (status, headers, body)
        self.calls: list[str] = []

    def handle_request(self, request):
        url = str(request.url)
        self.calls.append(url)
        status, headers, body = self.routes.get(url, (404, {}, "not found"))
        return httpx.Response(status, headers=headers, text=body)


def _fetcher(conn, routes, **over):
    settings = dict(scanner.DEFAULT_SETTINGS, min_request_interval_seconds=0,
                    **over)
    t = _RecordingTransport(routes)
    return scanner.PoliteFetcher(settings, conn, transport=t), t


def test_fetcher_redirect_is_robots_checked_and_throttled(conn):
    routes = {
        "https://shop.dk/robots.txt": (200, {}, "User-agent: *\nDisallow: /secret/\n"),
        "https://shop.dk/old": (301, {"location": "/secret/new"}, ""),
        "https://shop.dk/secret/new": (200, {}, "should never be fetched"),
    }
    f, t = _fetcher(conn, routes)
    res = f.get("https://shop.dk/old")
    f.close()
    # Redirect target is robots-disallowed -> blocked, and never fetched.
    assert "blocked by robots.txt" in (res.error or "")
    assert "https://shop.dk/secret/new" not in t.calls


def test_fetcher_redirect_followed_when_allowed(conn):
    routes = {
        "https://shop.dk/robots.txt": (200, {}, "User-agent: *\nDisallow: /admin/\n"),
        "https://shop.dk/a": (301, {"location": "/b"}, ""),
        "https://shop.dk/b": (200, {}, "final"),
    }
    f, t = _fetcher(conn, routes)
    res = f.get("https://shop.dk/a")
    f.close()
    assert res.ok and res.body == "final"


def test_fetcher_robots_5xx_not_cached_as_allow(conn):
    routes = {
        "https://shop.dk/robots.txt": (503, {}, "temporarily down"),
        "https://shop.dk/x": (200, {}, "data"),
    }
    f, t = _fetcher(conn, routes)
    res = f.get("https://shop.dk/x")
    f.close()
    # 5xx robots => fail closed (disallow) and NOT written to http_cache.
    assert "robots.txt" in (res.error or "")
    cached = conn.execute(
        "SELECT 1 FROM http_cache WHERE url = 'https://shop.dk/robots.txt'"
    ).fetchone()
    assert cached is None


def test_fetcher_robots_404_allows_and_is_cached(conn):
    routes = {
        "https://shop.dk/robots.txt": (404, {}, "nope"),
        "https://shop.dk/x": (200, {}, "data"),
    }
    f, t = _fetcher(conn, routes)
    res = f.get("https://shop.dk/x")
    f.close()
    assert res.ok and res.body == "data"


def test_shopify_search_keeps_no_match_error_over_html_page(conn):
    # Out-of-range page served as HTML (200) must not overwrite the honest
    # "no product matching" error from the real JSON pages.
    page1 = json.dumps({"products": [
        {"title": "Some Other Product", "handle": "x",
         "variants": [{"price": "100.00", "available": True}]}]})
    routes = {
        "https://shop.dk/robots.txt": (404, {}, ""),
        "https://shop.dk/products.json?limit=250&page=1": (200, {}, page1),
        "https://shop.dk/products.json?limit=250&page=2": (200, {}, "<html>x</html>"),
    }
    f, _ = _fetcher(conn, routes)
    product = {"id": "x", "name": "X"}
    source = {"shop": "shop.dk", "method": "shopify_search",
              "url": "https://shop.dk/", "query": "nonexistent widget"}
    obs = scanner.scan_source(product, source, f, dict(SETTINGS))
    f.close()
    assert obs.status != "ok"
    assert "no product matching" in (obs.error or "")
    assert "invalid JSON" not in (obs.error or "")


def test_fetcher_bad_currency_does_not_abort_scan(conn):
    body = ('<html><span content="10.00" itemprop="price">10,00</span>'
            "<h1>Thing</h1></html>")
    routes = {
        "https://shop.dk/robots.txt": (404, {}, ""),
        "https://shop.dk/p": (200, {}, body),
    }
    f, _ = _fetcher(conn, routes)
    product = {"id": "x", "name": "X"}
    source = {"shop": "shop.dk", "method": "epicpanda",
              "url": "https://shop.dk/p", "currency": "GBP"}
    obs = scanner.scan_source(product, source, f, dict(SETTINGS))
    f.close()
    assert obs.status != "ok"                # bad currency -> this source errors
    assert "GBP" in (obs.error or "")


class _StubFetcher:
    def __init__(self, body):
        self._body = body

    def get(self, url):
        return scanner.FetchResult(url=url, status=200, body=self._body,
                                   fetched_at=scanner.now_iso())


def test_scan_source_rejects_nonpositive_price():
    # Even if a parser slips, scan_source refuses price <= 0.
    body = ('<html><link rel="canonical" href="https://x.dk/shop/a.html"/>'
            "<h1>Weird page</h1><p>0,00 DKK</p></html>")
    product = {"id": "x", "name": "X"}
    source = {"shop": "epicpanda.dk", "method": "epicpanda", "url": "http://x"}
    settings = dict(SETTINGS, record_fixtures=False)
    obs = scanner.scan_source(product, source, _StubFetcher(body), settings)
    assert obs.status != "ok"
    assert "implausible" in (obs.error or "")


def test_pricecharting():
    p = scanner.parse_pricecharting(load("pricecharting_product.html"), "new")
    assert p.error is None
    assert p.price == pytest.approx(255.0)
    p_used = scanner.parse_pricecharting(load("pricecharting_product.html"),
                                         "used")
    assert p_used.price == pytest.approx(210.50)


def test_pricecharting_sealed_real_markup():
    # Real pricecharting sealed page: only the Loose (used) column is filled;
    # 'new'/'complete' are '-'. The parser must read Loose and, when asked for
    # an empty column, fall back to it rather than returning nothing.
    body = load("pricecharting_sealed.html")
    assert scanner.parse_pricecharting(body, "used").price == pytest.approx(161.09)
    p_new = scanner.parse_pricecharting(body, "new")
    assert p_new.error is None and p_new.price == pytest.approx(161.09)


def test_riporflip():
    p = scanner.parse_riporflip(load("riporflip.html"), "Riftbound Unleashed")
    assert p.error is None
    assert p.price == pytest.approx(108.3)


def test_riporflip_unknown_set():
    assert scanner.parse_riporflip(load("riporflip.html"), "Nonexistium").error


@pytest.mark.parametrize(
    "path", sorted(LIVE.glob("*")) if LIVE.exists() else [],
    ids=lambda p: p.name[:60])
def test_live_fixture_smoke(path):
    """Recorded real responses must parse correctly (or error honestly)."""
    body = path.read_text(encoding="utf-8")
    name = path.name
    if "br-dk" in name or "andcards" in name:      # JSON-LD product pages
        p = scanner.parse_jsonld_product(body)
        assert p.error is None and p.price > 0
    elif "faraos" in name:
        p = scanner.parse_faraos(body)
        assert p.error is None and p.price > 0
    elif "kelz0r" in name and "-p-" in name:
        p = scanner.parse_kelz0r_product(body)
        assert p.error is None and p.price > 0
    elif "kelz0r" in name:
        p = scanner.parse_kelz0r(body)
    elif "epicpanda" in name:
        p = scanner.parse_epicpanda(body)
        if "riftbound" in name:               # known frontpage redirect
            assert p.error and p.price is None
        else:
            assert p.error is None and p.price > 0
    elif "pricecharting" in name:
        p = scanner.parse_pricecharting(body, "used")
        assert p.error is None and p.price > 0
    elif "products-json" in name:
        p = scanner.parse_shopify_products_json(body, ["zzz", "nomatch"])
    elif name.endswith(".json"):
        p = scanner.parse_shopify_product_js(body)
        assert p.error is None and p.price > 0
    else:
        p = scanner.parse_kelz0r(body)
    assert isinstance(p, ParsedPrice)         # parse never raises


# ---------------------------------------------------------------------------
# Verdict engine — temp DB helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    c = scanner.get_db(tmp_path / "test.db")
    yield c
    c.close()


def make_product(**over) -> dict:
    p = {
        "id": "test-product",
        "name": "Test Product",
        "sources": [{"shop": "shopA", "method": "epicpanda", "url": "http://a"}],
        "cardmarket": {"url": "http://cm"},
        "triggers": {"buy_below_dkk": 1450},
    }
    p.update(over)
    return p


def put(conn, shop="shopA", method="epicpanda", price=None, in_stock=True,
        status="ok", days_ago=0, error=None, product_id="test-product",
        source_kind="html", reference_only=False, currency="DKK",
        price_native=None, allocation_risk=False):
    when = (datetime.now() - timedelta(days=days_ago)).isoformat(
        timespec="seconds")
    obs = Observation(
        product_id=product_id, shop=shop, url=f"http://{shop}", method=method,
        observed_at=when, title="t", price_native=price_native or price,
        currency=currency, price_dkk=price, landed_dkk=price,
        in_stock=in_stock if status == "ok" else None,
        status=status, error=error, allocation_risk=allocation_risk,
        reference_only=reference_only, source_kind=source_kind)
    scanner.insert_observation(conn, obs, scan_id=1)
    return obs


def test_verdict_buy_dk(conn):
    put(conn, price=1400)
    v = scanner.product_verdict(conn, make_product(), SETTINGS)
    assert v.code == "BUY"
    assert v.shop == "shopA"
    assert v.cheapest_dkk == 1400


def test_verdict_unverified_beats_buy(conn):
    put(conn, shop="shopA", price=1300)                 # below trigger!
    put(conn, shop="shopB", method="kelz0r_search", price=1500, days_ago=1)
    put(conn, shop="shopB", method="kelz0r_search", status="error",
        error="timeout", price=None)
    p = make_product(sources=[
        {"shop": "shopA", "method": "epicpanda", "url": "http://a"},
        {"shop": "shopB", "method": "kelz0r_search", "url": "http://b"}])
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "UNVERIFIED"
    assert any("last good" in f for f in v.failures)


def test_verdict_no_buy_when_out_of_stock(conn):
    put(conn, price=1300, in_stock=False)
    v = scanner.product_verdict(conn, make_product(), SETTINGS)
    assert v.code == "HOLD"
    assert "no verified in-stock" in v.reason


def test_verdict_avoid(conn):
    put(conn, price=1400)
    p = make_product(triggers={"buy_below_dkk": 1220, "avoid_above_dkk": 1350})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "AVOID"


def test_verdict_falling_beats_avoid(conn):
    for d in (1, 2, 3):
        put(conn, price=1500, days_ago=d)
    put(conn, price=1400)
    p = make_product(triggers={"buy_below_dkk": 1220, "avoid_above_dkk": 1350})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "WAIT_FALLING"           # spec priority: falling < avoid
    assert "7d avg" in v.reason


def test_verdict_hold_distance(conn):
    put(conn, price=1600)
    v = scanner.product_verdict(conn, make_product(), SETTINGS)
    assert v.code == "HOLD"
    assert v.vs_trigger_dkk == pytest.approx(150)


def test_verdict_manual_flag(conn):
    put(conn, status="error", error="down", price=None)
    p = make_product(triggers={"buy_below_dkk": 1250,
                               "manual_buy_flag": "riot_eol"},
                     flags={"riot_eol": True})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "BUY"
    assert "riot_eol" in v.reason


def test_verdict_cm_buy_without_stability_window(conn):
    put(conn, shop="cardmarket", method="cardmarket_manual",
        source_kind="manual", price=190 * scanner.EUR_DKK,
        price_native=190, currency="EUR")
    p = make_product(sources=[],
                     triggers={"cardmarket_buy_below_eur": 195})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "BUY"
    assert "Cardmarket" in v.reason


def test_verdict_cm_stability_insufficient(conn):
    put(conn, shop="cardmarket", method="cardmarket_manual",
        source_kind="manual", price=180 * scanner.EUR_DKK,
        price_native=180, currency="EUR")
    p = make_product(sources=[],
                     triggers={"cardmarket_buy_below_eur": 185,
                               "cardmarket_stable_days": 14})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code != "BUY"
    assert any("not yet stable" in n for n in v.notes)


def test_verdict_cm_stability_met(conn):
    for days_ago in (15, 7, 0):
        put(conn, shop="cardmarket", method="cardmarket_manual",
            source_kind="manual", price=180 * scanner.EUR_DKK,
            price_native=180, currency="EUR", days_ago=days_ago)
    p = make_product(sources=[],
                     triggers={"cardmarket_buy_below_eur": 185,
                               "cardmarket_stable_days": 14})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "BUY"
    assert "stable" in v.reason


def test_verdict_cm_stability_broken_by_spike(conn):
    for days_ago, eur in ((15, 180), (7, 190), (0, 180)):
        put(conn, shop="cardmarket", method="cardmarket_manual",
            source_kind="manual", price=eur * scanner.EUR_DKK,
            price_native=eur, currency="EUR", days_ago=days_ago)
    p = make_product(sources=[],
                     triggers={"cardmarket_buy_below_eur": 185,
                               "cardmarket_stable_days": 14})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code != "BUY"


def test_verdict_cm_stability_prewindow_spike_ignored(conn):
    # €250 just before the window, €180 inside it — NOT stable for 14 days.
    for days_ago, eur in ((20, 250), (6, 180), (0, 180)):
        put(conn, shop="cardmarket", method="cardmarket_manual",
            source_kind="manual", price=eur * scanner.EUR_DKK,
            price_native=eur, currency="EUR", days_ago=days_ago)
    p = make_product(sources=[],
                     triggers={"cardmarket_buy_below_eur": 185,
                               "cardmarket_stable_days": 14})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code != "BUY"
    assert any("start of the" in n for n in v.notes)


def test_verdict_cm_entry_expires(conn):
    # A 40-day-old manual entry must not keep driving BUY.
    put(conn, shop="cardmarket", method="cardmarket_manual",
        source_kind="manual", price=190 * scanner.EUR_DKK,
        price_native=190, currency="EUR", days_ago=40)
    p = make_product(sources=[], triggers={"cardmarket_buy_below_eur": 195})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code != "BUY"
    assert any("stale" in n for n in v.notes)


def test_verdict_ignores_removed_source_stale_buy(conn):
    # 'old' shop was removed from config; its cheap in-stock row must not BUY.
    put(conn, shop="old", method="epicpanda", price=1200, days_ago=90)
    put(conn, shop="live", method="epicpanda", price=1600)
    p = make_product(sources=[{"shop": "live", "method": "epicpanda",
                               "url": "http://live"}])
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code != "BUY"
    assert v.cheapest_shop == "live"


def test_verdict_ignores_removed_source_stale_failure(conn):
    # A removed source's last error must not wedge the product in UNVERIFIED.
    put(conn, shop="old", method="kelz0r_search", status="error",
        error="gone", price=None, days_ago=30)
    put(conn, shop="live", method="epicpanda", price=1600)
    p = make_product(sources=[{"shop": "live", "method": "epicpanda",
                               "url": "http://live"}])
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code != "UNVERIFIED"
    assert v.failures == []


def test_verdict_preorder_note(conn):
    put(conn, price=1050)
    future = (date.today() + timedelta(days=10)).isoformat()
    p = make_product(triggers={"buy_below_dkk": 1100, "not_before": future})
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "BUY"
    assert any("preorder" in n for n in v.notes)


def test_verdict_allocation_note(conn):
    put(conn, price=1600, allocation_risk=True)
    v = scanner.product_verdict(conn, make_product(), SETTINGS)
    assert any("allokering" in n.lower() for n in v.notes)


def test_verdict_no_data(conn):
    v = scanner.product_verdict(conn, make_product(sources=[]), SETTINGS)
    assert v.code == "UNVERIFIED"


def test_verdict_robots_skip_does_not_block_verdict(conn):
    # A robots-blocked source must not force UNVERIFIED when another shop is OK.
    put(conn, shop="live", method="epicpanda", price=1600)
    put(conn, shop="kelz0r.dk", method="kelz0r_search", status="skipped",
        error="blocked by robots.txt", price=None)
    p = make_product(sources=[
        {"shop": "live", "method": "epicpanda", "url": "http://live"},
        {"shop": "kelz0r.dk", "method": "kelz0r_search", "url": "http://k"}])
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "HOLD"                     # 1600 > 1450 trigger
    assert any("not checked" in n for n in v.notes)
    assert v.failures == []


def test_verdict_all_sources_skipped_is_unverified(conn):
    put(conn, shop="kelz0r.dk", method="kelz0r_search", status="skipped",
        error="blocked by robots.txt", price=None)
    p = make_product(sources=[{"shop": "kelz0r.dk", "method": "kelz0r_search",
                               "url": "http://k"}])
    v = scanner.product_verdict(conn, p, SETTINGS)
    assert v.code == "UNVERIFIED"
    assert "robots.txt" in v.reason


# ---------------------------------------------------------------------------
# Trend math
# ---------------------------------------------------------------------------

def test_trend_insufficient_history(conn):
    put(conn, price=1500, days_ago=1)
    put(conn, price=1400)
    t = scanner.compute_trend(conn, "test-product", SETTINGS)
    assert t.avg7 is None                     # 1 prior day < min 3 — no fake trend
    assert t.avg30 is None
    assert t.today == 1400


def test_trend_7d(conn):
    for d in (1, 2, 3):
        put(conn, price=1500, days_ago=d)
    put(conn, price=1400)
    t = scanner.compute_trend(conn, "test-product", SETTINGS)
    assert t.avg7 == pytest.approx(1500)
    assert t.today == 1400
    assert t.pct_vs(t.avg7) == pytest.approx(-6.67, abs=0.01)


def test_trend_excludes_manual_cardmarket(conn):
    # A sporadic manual CM entry must not enter the per-day cheapest series
    # (would fabricate a WAIT_FALLING trend).
    put(conn, shop="live", method="epicpanda", price=1600, days_ago=1)
    put(conn, shop="cardmarket", method="cardmarket_manual",
        source_kind="manual", price=1400, price_native=190, currency="EUR",
        days_ago=1)
    series = dict(scanner.daily_cheapest_series(conn, "test-product"))
    day = (date.today() - timedelta(days=1)).isoformat()
    assert series[day] == pytest.approx(1600)   # manual 1400 excluded


def test_trend_ignores_reference_and_failures(conn):
    for d in (1, 2, 3):
        put(conn, price=1500, days_ago=d)
        put(conn, shop="ref", price=99, days_ago=d, reference_only=True)
        put(conn, shop="bad", price=None, days_ago=d, status="error",
            error="x")
    t = scanner.compute_trend(conn, "test-product", SETTINGS)
    assert t.avg7 == pytest.approx(1500)      # reference rows never mix in


# ---------------------------------------------------------------------------
# Manual Cardmarket entry + export
# ---------------------------------------------------------------------------

def test_add_manual_cm_entry(conn):
    p = make_product()
    obs = scanner.add_manual_cardmarket_entry(conn, p, 185.0, SETTINGS)
    assert obs.price_dkk == pytest.approx(185 * 7.46)
    row = scanner._latest_cm_entry(conn, "test-product")
    assert row["price_native"] == 185.0
    assert row["source_kind"] == "manual"


def test_export_markdown(conn, tmp_path):
    put(conn, price=1400)
    cfg = {"settings": SETTINGS, "products": [make_product()]}
    md = scanner.export_markdown(cfg, conn)
    assert "Kort og Godt" in md
    assert "Test Product" in md
    assert "BUY" in md
    assert "insufficient history" in md
    assert "http://shopA" in md               # traceability: URL in report


def _cfg_with(*products):
    return {"settings": SETTINGS, "products": list(products)}


def test_current_value_replacement_picks_cheapest_instock(conn):
    put(conn, shop="shopA", method="epicpanda", price=1400)
    put(conn, shop="shopB", method="kelz0r_product", price=1300)
    p = make_product(sources=[
        {"shop": "shopA", "method": "epicpanda", "url": "http://a"},
        {"shop": "shopB", "method": "kelz0r_product", "url": "http://b"}])
    cv = scanner.current_value(conn, p, SETTINGS, "replacement")
    assert cv is not None and cv[0] == pytest.approx(1300) and cv[1] == "shopB"


def test_current_value_none_when_out_of_stock(conn):
    put(conn, price=1300, in_stock=False)
    assert scanner.current_value(conn, make_product(), SETTINGS,
                                 "replacement") is None


def test_current_value_cardmarket_basis(conn):
    put(conn, shop="cardmarket", method="cardmarket_manual",
        source_kind="manual", price=190 * scanner.EUR_DKK, price_native=190,
        currency="EUR")
    cv = scanner.current_value(conn, make_product(), SETTINGS, "cardmarket")
    assert cv is not None and cv[0] == pytest.approx(190 * scanner.EUR_DKK)


def test_current_value_cardmarket_stale_is_none(conn):
    put(conn, shop="cardmarket", method="cardmarket_manual",
        source_kind="manual", price=190 * scanner.EUR_DKK, price_native=190,
        currency="EUR", days_ago=60)
    assert scanner.current_value(conn, make_product(), SETTINGS,
                                 "cardmarket") is None


def test_value_collection_pl_and_unverified(conn):
    put(conn, shop="shopA", method="epicpanda", price=1600)
    cfg = _cfg_with(make_product())
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "h1", "name": "Test Product", "product_id": "test-product",
         "quantity": 2, "unit_cost_dkk": 1500},
        {"id": "h2", "name": "A single", "product_id": None, "quantity": 1,
         "unit_cost_dkk": 50, "manual_value_dkk": 80},
        {"id": "h3", "name": "Mystery box", "product_id": None, "quantity": 1,
         "unit_cost_dkk": 100},                     # no value -> unverified
    ]}
    v = scanner.value_collection(conn, cfg, collection)
    assert (v["n_items"], v["n_valued"], v["n_unverified"]) == (3, 2, 1)
    assert v["total_value"] == pytest.approx(1600 * 2 + 80)      # 3280
    assert v["total_cost"] == pytest.approx(1500 * 2 + 50)       # 3050 (priced)
    assert v["unrealized_pl"] == pytest.approx(230)
    assert v["n_valued_no_cost"] == 0


def test_value_collection_no_cost_valued_excluded_from_pl(conn):
    # A valued holding with NO recorded cost must count toward market value
    # but NOT inflate P/L (its cost is unknown, not zero).
    put(conn, shop="shopA", method="epicpanda", price=1600)
    cfg = _cfg_with(make_product())
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "h1", "name": "Costed", "product_id": "test-product",
         "quantity": 1, "unit_cost_dkk": 1500},          # +100 P/L
        {"id": "h2", "name": "Gift box", "product_id": "test-product",
         "quantity": 1},                                 # valued 1600, no cost
    ]}
    v = scanner.value_collection(conn, cfg, collection)
    assert v["n_valued"] == 2
    assert v["total_value"] == pytest.approx(3200)       # both counted as worth
    assert v["total_cost"] == pytest.approx(1500)        # only the costed one
    assert v["unrealized_pl"] == pytest.approx(100)      # NOT 1700
    assert v["n_valued_no_cost"] == 1
    assert v["value_no_cost"] == pytest.approx(1600)


def test_snapshot_is_idempotent_per_day_and_series(conn):
    put(conn, shop="shopA", method="epicpanda", price=1600)
    cfg = _cfg_with(make_product())
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "h1", "name": "Test Product", "product_id": "test-product",
         "quantity": 1, "unit_cost_dkk": 1500}]}
    scanner.snapshot_collection(conn, cfg, collection)
    scanner.snapshot_collection(conn, cfg, collection)   # same day -> overwrite
    series = scanner.collection_value_series(conn)
    assert len(series) == 1
    assert series[0]["total_value_dkk"] == pytest.approx(1600)


def test_value_collection_realized_pl(conn):
    put(conn, shop="shopA", method="epicpanda", price=1600)
    cfg = _cfg_with(make_product())
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "h1", "name": "Held", "product_id": "test-product",
         "quantity": 1, "unit_cost_dkk": 1500},
        {"id": "h2", "name": "Flipped box", "product_id": None, "quantity": 2,
         "unit_cost_dkk": 1000, "sold_price_dkk": 1300,
         "sold_date": "2026-07-20"},
    ]}
    v = scanner.value_collection(conn, cfg, collection)
    # Held: 1 item (the sold one is excluded from current totals).
    assert v["n_items"] == 1 and v["n_valued"] == 1
    assert v["total_value"] == pytest.approx(1600)
    # Realized: (1300 - 1000) * 2 = 600
    assert v["n_sold"] == 1
    assert v["realized_proceeds"] == pytest.approx(2600)
    assert v["realized_cost"] == pytest.approx(2000)
    assert v["realized_pl"] == pytest.approx(600)


def test_sold_holding_excluded_from_snapshot_value(conn):
    put(conn, shop="shopA", method="epicpanda", price=1600)
    cfg = _cfg_with(make_product())
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "h1", "name": "Held", "product_id": "test-product",
         "quantity": 1, "unit_cost_dkk": 1500},
        {"id": "h2", "name": "Sold", "product_id": "test-product",
         "quantity": 1, "unit_cost_dkk": 1000, "sold_price_dkk": 1300},
    ]}
    scanner.snapshot_collection(conn, cfg, collection)
    s = scanner.collection_value_series(conn)[-1]
    assert s["total_value_dkk"] == pytest.approx(1600)   # only the held one


def test_load_collection_missing_file(tmp_path):
    col = scanner.load_collection(tmp_path / "nope.json")
    assert col["holdings"] == []
    assert col["settings"]["valuation_basis"] == "replacement"


def test_config_in_db_roundtrip(conn):
    cfg = scanner.get_config(conn)                 # seeds from watchlist.json
    assert cfg["products"], "seeded products expected"
    assert "usd_dkk" in cfg["settings"]            # settings merged w/ defaults
    cfg["products"][0].setdefault("triggers", {})["buy_below_dkk"] = 111
    scanner.put_config(conn, cfg)
    again = scanner.get_config(conn)               # now reads from the DB
    assert again["products"][0]["triggers"]["buy_below_dkk"] == 111


def test_collection_in_db_roundtrip(conn):
    col = scanner.get_collection(conn)             # seeds from collection.json
    col["holdings"].append({"id": "t", "name": "ZZZ", "quantity": 1,
                            "unit_cost_dkk": 10})
    scanner.put_collection(conn, col)
    again = scanner.get_collection(conn)
    assert any(h["name"] == "ZZZ" for h in again["holdings"])
    assert again["settings"]["valuation_basis"] in scanner.VALUATION_BASES


def test_export_markdown_no_crash_when_avg_but_no_today(conn):
    # Regression: avg7 exists (>=3 prior days) but no in-stock price today ->
    # pct_vs returns None; export must not TypeError on the format string.
    for d in (1, 2, 3):
        put(conn, price=1500, days_ago=d)
    cfg = {"settings": SETTINGS, "products": [make_product()]}
    md = scanner.export_markdown(cfg, conn)          # must not raise
    assert "no verified in-stock price today" in md


# ---------------------------------------------------------------------------
# v0.2 — per-person attribution, migration, CM history, realized reporting,
# feedback, and Discord alerts
# ---------------------------------------------------------------------------

def test_manual_cm_entry_records_added_by(conn):
    p = make_product()
    scanner.add_manual_cardmarket_entry(conn, p, 100.0, SETTINGS, added_by="Bo")
    row = scanner._latest_cm_entry(conn, p["id"])
    assert row["added_by"] == "Bo"
    assert row["price_native"] == 100.0


def test_ensure_columns_retrofits_added_by(tmp_path):
    # Simulate a pre-v0.2 DB whose observations table lacks `added_by`.
    import sqlite3
    p = tmp_path / "old.db"
    raw = sqlite3.connect(str(p))
    raw.execute(
        "CREATE TABLE observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, "
        "product_id TEXT NOT NULL, shop TEXT NOT NULL, url TEXT, method TEXT, "
        "title TEXT, observed_at TEXT NOT NULL, price_native FLOAT, "
        "currency TEXT, price_dkk FLOAT, landed_dkk FLOAT, in_stock INTEGER, "
        "status TEXT NOT NULL, error TEXT, allocation_risk INTEGER DEFAULT 0, "
        "reference_only INTEGER DEFAULT 0, source_kind TEXT)")
    raw.commit()
    raw.close()

    c = scanner.get_db(p)   # connect(): create_all skips it, _ensure_columns adds col
    cols = {r["name"] for r in c.execute(
        "PRAGMA table_info(observations)").fetchall()}
    assert "added_by" in cols
    # End-to-end: a manual CM insert now writes added_by on the retrofitted table.
    scanner.add_manual_cardmarket_entry(
        c, {"id": "x", "name": "X", "cardmarket": {"url": "u"}}, 10.0,
        SETTINGS, added_by="Anna")
    got = c.execute(
        "SELECT added_by FROM observations WHERE product_id = 'x'").fetchone()
    assert got["added_by"] == "Anna"
    c.close()
    # Idempotent: reconnecting neither errors nor duplicates the column.
    c2 = scanner.get_db(p)
    cols2 = [r["name"] for r in c2.execute(
        "PRAGMA table_info(observations)").fetchall()]
    assert cols2.count("added_by") == 1
    c2.close()


def test_value_collection_per_person(conn):
    put(conn, shop="shopA", method="epicpanda", price=1600)
    cfg = {"settings": SETTINGS, "products": [make_product()]}
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "h1", "name": "Held-Anna", "product_id": "test-product",
         "quantity": 1, "unit_cost_dkk": 1500, "added_by": "Anna"},
        {"id": "h2", "name": "Sold-Bo", "product_id": None, "quantity": 2,
         "unit_cost_dkk": 1000, "sold_price_dkk": 1300,
         "sold_date": "2026-07-20", "added_by": "Bo"},
    ]}
    v = scanner.value_collection(conn, cfg, collection)
    bp = v["by_person"]
    assert set(bp) == {"Anna", "Bo"}
    assert bp["Anna"]["market_value"] == pytest.approx(1600)
    assert bp["Anna"]["unrealized_pl"] == pytest.approx(100)   # 1600 - 1500
    assert bp["Anna"]["n_sold"] == 0
    assert bp["Bo"]["n_items"] == 0            # Bo's only holding is sold
    assert bp["Bo"]["realized_proceeds"] == pytest.approx(2600)
    assert bp["Bo"]["realized_pl"] == pytest.approx(600)       # (1300-1000)*2


def test_cardmarket_series_manual_only(conn):
    put(conn, source_kind="manual", currency="EUR", price_native=120,
        price=895, days_ago=10)
    put(conn, source_kind="manual", currency="EUR", price_native=100,
        price=746, days_ago=0)
    put(conn, shop="shopA", method="epicpanda", price=1500)   # scraped: excluded
    s = scanner.cardmarket_series(conn, "test-product")
    assert len(s) == 2
    assert [p for _, p in s] == [120, 100]     # observed_at order, EUR native


def test_realized_roi_and_by_year(conn):
    cfg = {"settings": SETTINGS, "products": []}
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "a", "name": "A", "quantity": 1, "unit_cost_dkk": 100,
         "sold_price_dkk": 150, "sold_date": "2025-03-01"},
        {"id": "b", "name": "B", "quantity": 2, "unit_cost_dkk": 100,
         "sold_price_dkk": 120, "sold_date": "sold 2026-01-10 to a mate"},
    ]}
    v = scanner.value_collection(conn, cfg, collection)
    by_name = {r["name"]: r for r in v["sold_rows"]}
    assert by_name["A"]["roi_pct"] == pytest.approx(50.0)   # (150-100)/100
    assert by_name["A"]["year"] == 2025
    assert by_name["B"]["roi_pct"] == pytest.approx(20.0)   # (240-200)/200
    assert by_name["B"]["year"] == 2026                     # parsed from free text
    rby = v["realized_by_year"]
    assert set(rby) == {2025, 2026}
    assert rby[2025]["pl"] == pytest.approx(50)
    assert rby[2026]["pl"] == pytest.approx(40)             # (120-100)*2


def test_sold_year_parsing():
    assert scanner._sold_year("2025-03-01") == 2025
    assert scanner._sold_year("solgt 12/2026") == 2026
    assert scanner._sold_year("") is None
    assert scanner._sold_year("last week") is None
    assert scanner._sold_year("9999") is None              # out of sane range


def test_export_sales_csv_escapes_and_formats(conn):
    cfg = {"settings": SETTINGS, "products": []}
    collection = {"settings": {"valuation_basis": "replacement"}, "holdings": [
        {"id": "a", "name": "Booster, Box", "quantity": 1, "unit_cost_dkk": 100,
         "sold_price_dkk": 150, "sold_date": "2025-03-01", "added_by": "Anna"},
    ]}
    v = scanner.value_collection(conn, cfg, collection)
    csv = scanner.export_sales_csv(v)
    lines = csv.strip().splitlines()
    assert lines[0].startswith("name,quantity,")
    assert '"Booster, Box"' in lines[1]        # comma-in-name → quoted field
    assert "Anna" in lines[1]
    assert "50.00" in lines[1]                 # roi_pct formatted to 2 decimals


def test_feedback_roundtrip(conn):
    scanner.add_feedback(conn, "Anna", "Prices look off", "0.2",
                         product_id="test-product")
    fb = scanner.list_feedback(conn)
    assert len(fb) == 1
    assert fb[0]["person"] == "Anna"
    assert fb[0]["message"] == "Prices look off"
    assert fb[0]["app_version"] == "0.2"
    assert fb[0]["resolved"] == 0


def test_newly_buy_ids_diff():
    assert scanner.newly_buy_ids(["a", "b", "c"], ["b"]) == ["a", "c"]
    assert scanner.newly_buy_ids(["a"], ["a"]) == []
    assert scanner.newly_buy_ids([], ["a"]) == []


def test_build_discord_payload():
    assert scanner.build_discord_payload([]) == {"content": ""}
    p = scanner.build_discord_payload(
        [{"name": "Box A", "reason": "cheap now", "url": "http://x"}])
    assert "Box A" in p["content"]
    assert "cheap now" in p["content"]
    assert "http://x" in p["content"]


# ---------------------------------------------------------------------------
# v0.3 — landed-cost honesty + config source migration
# ---------------------------------------------------------------------------

def test_resolve_shipping_source_override_wins():
    settings = {"shop_shipping_dkk": {"shopA": 45}}
    assert scanner.resolve_shipping_dkk(
        {"shop": "shopA", "shipping_dkk": 29}, settings) == 29
    assert scanner.resolve_shipping_dkk({"shop": "shopA"}, settings) == 45
    assert scanner.resolve_shipping_dkk({"shop": "unknown.dk"}, settings) == 0
    assert scanner.resolve_shipping_dkk({"shop": "shopA"}, {}) == 0


def test_shipping_known_shops():
    cfg = {
        "settings": {"shop_shipping_dkk": {"a.dk": 45, "free.dk": 0}},
        "products": [{"id": "p", "name": "P", "sources": [
            {"shop": "b.dk", "method": "epicpanda", "url": "u",
             "shipping_dkk": 39},
            {"shop": "c.dk", "method": "epicpanda", "url": "u"},
        ]}],
    }
    known = scanner.shipping_known_shops(cfg)
    assert known == {"a.dk", "free.dk", "b.dk"}    # 0 is a KNOWN value (free)
    assert "c.dk" not in known


def test_scan_source_landed_uses_shop_default(conn):
    # scan_source applies the per-shop estimate when the source has none.
    settings = dict(SETTINGS, record_fixtures=False)
    settings["shop_shipping_dkk"] = {"shopA": 50}
    body = load("epicpanda_product.html")
    fetcher = _StubFetcher(body)
    src = {"shop": "shopA", "method": "epicpanda", "url": "http://a"}
    obs = scanner.scan_source(make_product(), src, fetcher, settings)
    assert obs.status == "ok"
    assert obs.landed_dkk == pytest.approx(obs.price_dkk + 50)
    # A per-source value beats the shop default.
    obs2 = scanner.scan_source(
        make_product(), {**src, "shipping_dkk": 10}, fetcher, settings)
    assert obs2.landed_dkk == pytest.approx(obs2.price_dkk + 10)


def test_migrate_config_adds_sources_preserves_edits():
    # A stored config from v0.1/0.2: no version, dead kelz0r search source,
    # user-edited trigger that must survive.
    stored = {
        "settings": dict(scanner.DEFAULT_SETTINGS),
        "products": [{
            "id": "pitch-black-booster-box-en",
            "name": "Pokémon Pitch Black Booster Box EN",
            "sources": [
                {"shop": "symbizon.dk", "method": "shopify_handle",
                 "url": "https://symbizon.dk/products/x"},
                {"shop": "kelz0r.dk", "method": "kelz0r_search",
                 "url": "https://www.kelz0r.dk/magic/advanced_search_result.php?x=1",
                 "query": "pitch black display"},
            ],
            "triggers": {"buy_below_dkk": 1234},     # user-edited
        }],
    }
    stored["settings"].pop("config_version", None)
    assert scanner._migrate_config(stored) is True
    p = stored["products"][0]
    shops = {(s["shop"], s["method"]) for s in p["sources"]}
    # Dead robots-blocked search source dropped; allowed replacement added.
    assert ("kelz0r.dk", "kelz0r_search") not in shops
    assert ("kelz0r.dk", "kelz0r_product") in shops
    # New v0.3 shops merged in from the seed.
    assert ("flinamania.dk", "shopify_handle") in shops
    # User's symbizon source and edited trigger untouched.
    assert p["triggers"]["buy_below_dkk"] == 1234
    assert p["sources"][0]["url"] == "https://symbizon.dk/products/x"
    # Seed products the stored config lacked are appended whole.
    assert any(q["id"] == "mtg-hobbit-play-booster-box"
               for q in stored["products"])
    # Shipping estimates fill in; version stamped; second run is a no-op.
    assert stored["settings"]["shop_shipping_dkk"]["kelz0r.dk"] == 45
    assert stored["settings"]["config_version"] == scanner.SEED_CONFIG_VERSION
    assert scanner._migrate_config(stored) is False


def test_migrate_config_respects_user_shipping():
    stored = {"settings": dict(scanner.DEFAULT_SETTINGS), "products": []}
    stored["settings"].pop("config_version", None)
    stored["settings"]["shop_shipping_dkk"] = {"kelz0r.dk": 99}   # user's own
    scanner._migrate_config(stored)
    assert stored["settings"]["shop_shipping_dkk"]["kelz0r.dk"] == 99
    assert stored["settings"]["shop_shipping_dkk"]["flinamania.dk"] == 45


def test_get_config_migrates_stored_db_config(conn):
    old = {"settings": {"usd_dkk": 6.4},
           "products": [{"id": "pitch-black-etb-en", "name": "PB ETB",
                         "sources": [], "triggers": {"buy_below_dkk": 599}}]}
    scanner.put_config(conn, old)
    cfg = scanner.get_config(conn)
    p = next(q for q in cfg["products"] if q["id"] == "pitch-black-etb-en")
    assert any(s["shop"] == "cardx.dk" for s in p["sources"])   # merged in
    assert p["triggers"]["buy_below_dkk"] == 599                # preserved
    # Persisted: reading again straight from the DB shows the stamp.
    again = scanner.get_config(conn)
    assert again["settings"]["config_version"] == scanner.SEED_CONFIG_VERSION


# ---------------------------------------------------------------------------
# v0.4 — plausibility band, headless scan, config v5
# ---------------------------------------------------------------------------

def test_plausibility_band_rejects_outliers(conn):
    settings = dict(SETTINGS, record_fixtures=False)
    body = load("epicpanda_product.html")     # parses to a real price
    src = {"shop": "shopA", "method": "epicpanda", "url": "http://a"}
    # Inside the band → recorded normally.
    prod = make_product(triggers={"plausible_min_dkk": 100,
                                  "plausible_max_dkk": 99999})
    obs = scanner.scan_source(prod, src, _StubFetcher(body), settings)
    assert obs.status == "ok" and obs.landed_dkk > 0
    # Above the band → refused, no price recorded, honest error.
    prod = make_product(triggers={"plausible_min_dkk": 100,
                                  "plausible_max_dkk": 500})
    obs = scanner.scan_source(prod, src, _StubFetcher(body), settings)
    assert obs.status == "error"
    assert "implausible" in obs.error and "band" in obs.error
    assert obs.landed_dkk is None and obs.price_dkk is None
    # Below the band → refused too.
    prod = make_product(triggers={"plausible_min_dkk": 90000,
                                  "plausible_max_dkk": 99999})
    obs = scanner.scan_source(prod, src, _StubFetcher(body), settings)
    assert obs.status == "error" and "implausible" in obs.error
    # No band configured → unchanged behavior.
    obs = scanner.scan_source(make_product(), src, _StubFetcher(body), settings)
    assert obs.status == "ok"


def test_run_headless_no_webhook_leaves_buy_state(conn):
    # Headless run without a webhook must complete, snapshot, and leave the
    # BUY-diff state EXACTLY as it was — a later app-side manual scan still
    # gets to diff and ping (running the diff with nowhere to send it would
    # silently eat those alerts).
    # config_version = current, else get_config() would migrate the seed's
    # real sources into this test config and run_scan would hit the network.
    cfg = {"settings": dict(SETTINGS, record_fixtures=False,
                            config_version=scanner.SEED_CONFIG_VERSION),
           "products": [make_product(sources=[])]}
    scanner.put_config(conn, cfg)
    scanner.put_last_buy_set(conn, ["sentinel"])     # pre-existing app state
    summary = scanner.run_headless(conn, webhook=None)
    assert summary["scan_id"] > 0
    assert summary["n_sources"] == 0            # no fetchable sources
    assert summary["new_buy_ids"] is None       # diff skipped…
    assert scanner.get_last_buy_set(conn) == {"sentinel"}   # …state untouched
    row = conn.execute("SELECT finished_at FROM scans WHERE id = :i",
                       {"i": summary["scan_id"]}).fetchone()
    assert row and row["finished_at"]           # scan row completed
    snap = conn.execute(
        "SELECT COUNT(*) AS n FROM collection_snapshots").fetchone()
    assert snap["n"] >= 1                       # snapshot recorded


def test_geo_priced_source_skipped_from_non_dk_vantage(conn):
    # A requires_dk_ip source (flinamania geo-prices USD to non-DK IPs) is a
    # policy SKIP from CI — never fetched, never a failure — and behaves
    # normally when scanned from Denmark.
    settings = dict(SETTINGS, record_fixtures=False)
    body = load("epicpanda_product.html")
    src = {"shop": "flinamania.dk", "method": "epicpanda", "url": "http://f",
           "requires_dk_ip": True}
    # DK vantage (default): scans normally.
    obs = scanner.scan_source(make_product(), src, _StubFetcher(body), settings)
    assert obs.status == "ok"
    # Non-DK vantage: policy skip, no fetch, honest note.
    settings["non_dk_vantage"] = True
    obs = scanner.scan_source(make_product(), src, _StubFetcher(body), settings)
    assert obs.status == "skipped"
    assert "geo-priced" in obs.error
    # And a skipped geo row doesn't wedge the product: verdict treats it as a
    # note, exactly like a robots skip.
    scanner.insert_observation(conn, obs, scan_id=1)
    put(conn, shop="shopA", price=1400)
    product = make_product(sources=[
        {"shop": "shopA", "method": "epicpanda", "url": "http://a"},
        src,
    ])
    v = scanner.product_verdict(conn, product, settings)
    assert v.code == "BUY"
    assert any("geo-priced" in n for n in v.notes)


def test_migrate_config_v6_stamps_geo_flag():
    stored = {
        "settings": dict(scanner.DEFAULT_SETTINGS, config_version=5),
        "products": [{
            "id": "pitch-black-etb-en", "name": "PB ETB",
            "sources": [{"shop": "flinamania.dk", "method": "shopify_handle",
                         "url": "https://flinamania.dk/products/x"}],
            "triggers": {},
        }],
    }
    assert scanner._migrate_config(stored) is True
    s = stored["products"][0]["sources"][0]
    assert s["requires_dk_ip"] is True
    assert stored["settings"]["config_version"] == scanner.SEED_CONFIG_VERSION


def test_migrate_config_v5_adds_kelz0r_etbs_and_bands():
    stored = {
        "settings": dict(scanner.DEFAULT_SETTINGS, config_version=4),
        "products": [{
            "id": "pitch-black-etb-en",
            "name": "Pokémon Pitch Black ETB EN",
            "sources": [{"shop": "symbizon.dk", "method": "shopify_search",
                         "url": "https://symbizon.dk/"}],
            "triggers": {"buy_below_dkk": 599, "plausible_max_dkk": 7777},
        }],
    }
    assert scanner._migrate_config(stored) is True
    p = next(q for q in stored["products"] if q["id"] == "pitch-black-etb-en")
    assert any(s["shop"] == "kelz0r.dk" and s["method"] == "kelz0r_product"
               for s in p["sources"])
    # Band keys: absent one adopted from seed; user-set one NOT overwritten.
    assert p["triggers"]["plausible_min_dkk"] == 300
    assert p["triggers"]["plausible_max_dkk"] == 7777
    assert p["triggers"]["buy_below_dkk"] == 599
    assert stored["settings"]["config_version"] == scanner.SEED_CONFIG_VERSION


# ---------------------------------------------------------------------------
# v0.3 — br.dk: JSON-LD parser + preferred-shop tie-break
# ---------------------------------------------------------------------------

def test_parse_jsonld_product():
    p = scanner.parse_jsonld_product(load("jsonld_product.html"),
                                     ["pitch", "black", "elite", "trainer"])
    assert p.error is None
    assert p.price == 599
    assert p.in_stock is True
    assert "Pitch Black Elite Trainer Box" in p.title


def test_parse_faraos_live():
    p = scanner.parse_faraos(
        load("faraos_product.html"), "spider-man collector".split(),
        requested_url="https://www.faraos.dk/games/x/test-product")
    assert p.error is None
    assert p.price == pytest.approx(3979.0)     # data-price, not the 1279 cross-sell
    assert p.in_stock is True
    assert "Spider-Man Collector" in p.title


def test_parse_faraos_soft404_and_guards():
    req = "https://www.faraos.dk/games/x/test-product"
    # Dead slug → category (og:url mismatch): refuse, never a stray price.
    p = scanner.parse_faraos(load("faraos_soft404.html"), None, requested_url=req)
    assert p.error and p.price is None and "soft-404" in p.error
    # Wrong query on the live page → refuse.
    p = scanner.parse_faraos(load("faraos_product.html"),
                             "final fantasy".split(), requested_url=req)
    assert p.error and "does not match" in p.error
    # Smoke-test mode (no requested_url): still parses the real page.
    p = scanner.parse_faraos(load("faraos_product.html"))
    assert p.error is None and p.price == pytest.approx(3979.0)


def test_jsonld_pricespecification_sale_over_list():
    # WooCommerce (andcards.dk) nests price in offers[].priceSpecification[]
    # with a sale UnitPriceSpecification + a struck-through ListPrice. We must
    # read the SALE price, never the ListPrice, and honor the currency there.
    body = ('<script type="application/ld+json">{"@type": "Product", '
            '"name": "X Booster Box", "offers": [{"@type": "Offer", '
            '"availability": "https://schema.org/InStock", '
            '"priceSpecification": ['
            '{"@type": "UnitPriceSpecification", "price": "599.00", '
            '"priceCurrency": "DKK"}, '
            '{"@type": "UnitPriceSpecification", "price": "649.00", '
            '"priceCurrency": "DKK", '
            '"priceType": "https://schema.org/ListPrice"}]}]}</script>')
    p = scanner.parse_jsonld_product(body, ["booster", "box"])
    assert p.error is None
    assert p.price == pytest.approx(599.0)     # sale, not the 649 ListPrice
    assert p.in_stock is True


def test_parse_jsonld_product_guards():
    body = load("jsonld_product.html")
    # Wrong product → title guard refuses (never a wrong price).
    p = scanner.parse_jsonld_product(body, ["ascended", "heroes"])
    assert p.error and p.price is None
    # Currency mismatch → refuses to mis-label.
    p = scanner.parse_jsonld_product(body, None, expected_currency="EUR")
    assert p.error and "priceCurrency" in p.error
    # Page with no Product data (BR serves an empty shell for dead URLs).
    p = scanner.parse_jsonld_product("<html><body>BR.dk</body></html>")
    assert p.error and p.price is None
    # OutOfStock maps honestly; @graph + offers-as-list shapes parse too.
    graph = ('<script type="application/ld+json">{"@graph": [{"@type": '
             '"Product", "name": "X Box", "offers": [{"price": "123.45", '
             '"priceCurrency": "DKK", "availability": '
             '"http://schema.org/OutOfStock"}]}]}</script>')
    p = scanner.parse_jsonld_product(graph)
    assert p.error is None
    assert p.price == pytest.approx(123.45)
    assert p.in_stock is False


def test_cheapest_tiebreak_prefers_shop(conn):
    # Same landed price at both shops → the preferred shop wins the pick and
    # the verdict explains why; a genuinely lower price still beats preference.
    put(conn, shop="symbizon.dk", price=1400)
    put(conn, shop="br.dk", method="jsonld_product", price=1400)
    settings = dict(SETTINGS)
    settings["preferred_shops"] = {"br.dk": "1 års ombytning på uåbnede varer"}
    product = make_product(sources=[
        {"shop": "symbizon.dk", "method": "epicpanda", "url": "http://a"},
        {"shop": "br.dk", "method": "jsonld_product", "url": "http://b"},
    ])
    v = scanner.product_verdict(conn, product, settings)
    assert v.code == "BUY"
    assert v.cheapest_shop == "br.dk"
    assert any("br.dk" in n and "prislighed" in n for n in v.notes)
    # Now symbizon undercuts by 1 kr → preference must NOT override price.
    put(conn, shop="symbizon.dk", price=1399)
    v2 = scanner.product_verdict(conn, product, settings)
    assert v2.cheapest_shop == "symbizon.dk"
    assert v2.cheapest_dkk == 1399


def test_migrate_config_v4_adds_br_and_preferred():
    # A DB config already migrated to v3 (e.g. by the v0.3 rollout) must pick
    # up the v4 additions: br.dk source on the ETB + the preference setting.
    stored = {
        "settings": dict(scanner.DEFAULT_SETTINGS,
                         config_version=3,
                         shop_shipping_dkk={"kelz0r.dk": 45},
                         preferred_shops={"myshop.dk": "custom reason"}),
        "products": [{
            "id": "pitch-black-etb-en",
            "name": "Pokémon Pitch Black ETB EN",
            "sources": [{"shop": "symbizon.dk", "method": "shopify_search",
                         "url": "https://symbizon.dk/"}],
            "triggers": {"buy_below_dkk": 599},
        }],
    }
    assert scanner._migrate_config(stored) is True
    p = next(q for q in stored["products"] if q["id"] == "pitch-black-etb-en")
    assert any(s["shop"] == "br.dk" and s["method"] == "jsonld_product"
               for s in p["sources"])
    assert stored["settings"]["preferred_shops"]["br.dk"] \
        == "1 års ombytning på uåbnede varer"
    assert stored["settings"]["preferred_shops"]["myshop.dk"] \
        == "custom reason"                     # user's own entry untouched
    assert stored["settings"]["shop_shipping_dkk"]["br.dk"] == 0
    assert stored["settings"]["config_version"] == scanner.SEED_CONFIG_VERSION


def test_notify_new_buys_persists_and_diffs(conn):
    buys = [{"id": "p1", "name": "One"}, {"id": "p2", "name": "Two"}]
    # Empty webhook -> no network; still diffs against the stored set + persists.
    assert scanner.notify_new_buys(conn, "", buys) == ["p1", "p2"]
    assert scanner.notify_new_buys(conn, "", buys) == []        # nothing new
    buys2 = buys + [{"id": "p3", "name": "Three"}]
    assert scanner.notify_new_buys(conn, "", buys2) == ["p3"]   # only the new one
    # Dropping out of BUY then back doesn't spuriously resurface others.
    assert scanner.notify_new_buys(conn, "", [{"id": "p1", "name": "One"}]) == []
    assert scanner.notify_new_buys(conn, "", buys2) == ["p2", "p3"]
