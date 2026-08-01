"""Kort og Godt — core scanning logic.

Importable, Streamlit-free module: config, polite HTTP fetching, per-shop
parsers, SQLite history, trend math, verdict engine and markdown export.

Read-only tool: it never purchases anything, never logs in, never bypasses
bot protection. Cardmarket is deliberately NOT scraped (Cloudflare + ToS);
prices there are entered manually in the UI and stored like scraped data.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from selectolax.parser import HTMLParser

import db
from db import Database

# EUR/DKK is pegged (ERM II band): hardcoded. USD/DKK floats: config value.
EUR_DKK = 7.46

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "watchlist.json"
COLLECTION_PATH = APP_DIR / "collection.json"
DB_PATH = APP_DIR / "kortoggodt.db"
FIXTURES_LIVE_DIR = APP_DIR / "fixtures" / "live"

VALUATION_BASES = ("replacement", "cardmarket")
DEFAULT_COLLECTION = {"settings": {"valuation_basis": "replacement"},
                      "holdings": []}

DEFAULT_SETTINGS = {
    "user_agent": "KortOgGodtScanner/1.0 (personal price watchlist; low volume; not a crawler)",
    "timeout_seconds": 15,
    "cache_ttl_seconds": 3600,
    "min_request_interval_seconds": 1.0,
    "usd_dkk": 6.40,
    "trend_min_days_7d": 3,
    "trend_min_days_30d": 10,
    "record_fixtures": True,
    # A manually-entered Cardmarket price older than this is treated as stale:
    # it stops driving a BUY and is shown as a note instead (manual entries,
    # unlike scraped ones, never auto-refresh to UNVERIFIED).
    "cardmarket_max_age_days": 30,
    # Roster of people sharing this deployment. Names are added from the UI's
    # "who are you?" step and attributed onto holdings and Cardmarket entries.
    "people": [],
    # Flat per-shop shipping estimates (DKK) used when a source doesn't set its
    # own shipping_dkk. Editable estimates — landed prices are only as honest
    # as these numbers. A shop absent here counts 0 and the UI marks its
    # landed price with * (shelf price, shipping unknown).
    "shop_shipping_dkk": {},
    # Shops preferred when landed prices TIE exactly (map shop -> short why).
    # E.g. br.dk gives 1 year of returns on unopened products, so at the same
    # price it is strictly the better venue. Never beats a lower price.
    "preferred_shops": {},
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    settings = dict(DEFAULT_SETTINGS)
    settings.update(cfg.get("settings", {}))
    cfg["settings"] = settings
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_collection(path: Path = COLLECTION_PATH) -> dict:
    """Your holdings (what you own), for value tracking. Missing file is fine
    — returns an empty collection with default settings."""
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_COLLECTION))
    with open(path, encoding="utf-8") as f:
        col = json.load(f)
    col.setdefault("settings", {}).setdefault("valuation_basis", "replacement")
    if col["settings"]["valuation_basis"] not in VALUATION_BASES:
        col["settings"]["valuation_basis"] = "replacement"
    col.setdefault("holdings", [])
    return col


def save_collection(col: dict, path: Path = COLLECTION_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(col, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _normalize_settings(cfg: dict) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    settings.update(cfg.get("settings", {}))
    cfg["settings"] = settings
    return cfg


def _normalize_collection(col: dict) -> dict:
    col.setdefault("settings", {}).setdefault("valuation_basis", "replacement")
    if col["settings"]["valuation_basis"] not in VALUATION_BASES:
        col["settings"]["valuation_basis"] = "replacement"
    col.setdefault("holdings", [])
    return col


def _put_kv(conn, key: str, obj: dict) -> None:
    value = json.dumps(obj, ensure_ascii=False)
    conn.execute("DELETE FROM app_config WHERE key = :k", {"k": key})
    conn.execute("INSERT INTO app_config (key, value) VALUES (:k, :v)",
                 {"k": key, "v": value})


SEED_CONFIG_VERSION = 9    # bump when watchlist.json ships sources/settings
                           # that existing DB configs should absorb


def _migrate_config(cfg: dict) -> bool:
    """Bring a stored (DB) config up to the bundled seed's version.

    The DB config is the source of truth after first run, so new sources added
    to watchlist.json in a release would otherwise never reach existing
    deployments. This merge is deliberately conservative:
      * ADD-only for products and sources — user-edited triggers, flags and
        notes are never touched; an existing (shop, method) source is kept
        as the user has it.
      * The single exception: sources pointing at kelz0r's robots-blocked
        search endpoint are dropped — they are permanently dead (policy skip)
        and were replaced by allowed product-page sources in v0.3.
      * Per-shop shipping estimates fill in only where the user hasn't set one.
    Returns True when a migration ran (caller persists — the version stamp
    itself must be saved so the seed file isn't re-read every load).
    """
    stored_ver = int(cfg.get("settings", {}).get("config_version") or 0)
    if stored_ver >= SEED_CONFIG_VERSION:
        return False
    try:
        seed = load_config()
    except (OSError, ValueError):       # no bundled file (unusual) — skip
        return False
    by_id = {p.get("id"): p for p in cfg.get("products", [])}
    for sp in seed.get("products", []):
        p = by_id.get(sp.get("id"))
        if p is None:
            cfg.setdefault("products", []).append(sp)   # brand-new product
            continue
        have = {(s.get("shop"), s.get("method"))
                for s in p.get("sources", [])}
        for ss in sp.get("sources", []):
            if (ss.get("shop"), ss.get("method")) not in have:
                p.setdefault("sources", []).append(ss)
        # Plausibility bands: adopt seed values only where the user has not
        # set their own. Strictly these two keys — buy/avoid triggers are
        # user territory and are never touched.
        seed_t = sp.get("triggers", {})
        mine_t = p.setdefault("triggers", {})
        for band_key in ("plausible_min_dkk", "plausible_max_dkk"):
            if band_key in seed_t and band_key not in mine_t:
                mine_t[band_key] = seed_t[band_key]
    for p in cfg.get("products", []):
        srcs = p.get("sources", [])
        kept = [s for s in srcs
                if "advanced_search_result.php" not in (s.get("url") or "")]
        if len(kept) != len(srcs):
            p["sources"] = kept
        # v6: geo-priced shops must carry requires_dk_ip so the scheduled
        # (non-DK) scan skips them instead of recording foreign-currency
        # prices. Stamp known geo-priced shops on already-stored sources.
        for s in p.get("sources", []):
            if s.get("shop") == "flinamania.dk":
                s.setdefault("requires_dk_ip", True)
    # Adopt seed values for per-shop maps only where the user hasn't set one.
    for map_key in ("shop_shipping_dkk", "preferred_shops"):
        seed_map = seed.get("settings", {}).get(map_key, {})
        mine = cfg.setdefault("settings", {}).setdefault(map_key, {})
        for shop, val in seed_map.items():
            if shop not in mine:
                mine[shop] = val
    # Always stamp + persist the version (even if nothing merged) so the
    # migration — and its seed-file read — doesn't re-run on every load.
    cfg["settings"]["config_version"] = SEED_CONFIG_VERSION
    return True


def get_config(conn) -> dict:
    """Shared watchlist config from the database (the source of truth when the
    app runs against a shared DB). Seeded once from watchlist.json; release
    additions are merged in by _migrate_config (add-only, versioned)."""
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = 'watchlist'").fetchone()
    if row and row["value"]:
        cfg = _normalize_settings(json.loads(row["value"]))
        if _migrate_config(cfg):
            put_config(conn, cfg)
        return cfg
    cfg = load_config()                 # seed from the bundled file
    put_config(conn, cfg)
    return _normalize_settings(cfg)


def put_config(conn, cfg: dict) -> None:
    _put_kv(conn, "watchlist", cfg)


def get_collection(conn) -> dict:
    """Shared collection (holdings) from the database. Seeded once from
    collection.json."""
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = 'collection'").fetchone()
    if row and row["value"]:
        return _normalize_collection(json.loads(row["value"]))
    col = load_collection()
    put_collection(conn, col)
    return _normalize_collection(col)


def put_collection(conn, col: dict) -> None:
    _put_kv(conn, "collection", col)


# ---------------------------------------------------------------------------
# Price parsing / formatting
# ---------------------------------------------------------------------------

# Matches Danish-formatted amounts inside arbitrary text: "1.999,95", "599,-",
# "1.879,95 DKK", "kr 634,95" — and bare "1097.99" (dot decimal).
_DK_PRICE_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+\.\d{2}|\d+(?:,\d{2})?)\s*(?:,-)?"
)


def parse_danish_price(text: str) -> Optional[float]:
    """Parse a Danish-formatted price string into a float.

    '1.999,95' -> 1999.95   '599,-' -> 599.0   '1097.99' -> 1097.99
    Returns None when no amount can be found.
    """
    if not text:
        return None
    t = text.replace("\xa0", " ").replace("&nbsp;", " ")
    t = re.sub(r"(?i)(DKK|kr\.?)", " ", t).strip()
    m = _DK_PRICE_RE.search(t)
    if not m:
        return None
    token = m.group(1)
    if re.fullmatch(r"\d+\.\d{2}", token):
        # "1097.99": a dot followed by exactly two digits is a decimal point,
        # never a Danish thousands separator (those group three digits).
        return float(token)
    return float(token.replace(".", "").replace(",", "."))


def parse_usd_price(text: str) -> Optional[float]:
    """Parse a US-formatted price: '$1,234.56' -> 1234.56."""
    if not text:
        return None
    m = re.search(r"\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)", text)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def fmt_dkk(x: Optional[float], decimals: int = 2) -> str:
    if x is None:
        return "–"
    s = f"{x:,.{decimals}f}"
    s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"{s} kr"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db(url=None) -> Database:
    """Open the database. Defaults to a local SQLite file; pass a Postgres
    URL (or set DATABASE_URL) to share one database across several machines.
    The schema (defined in db.py) is created on first connect."""
    return db.connect(url)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Polite fetcher: rate limit, cache, robots.txt, custom UA, timeout
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    status: Optional[int] = None
    body: Optional[str] = None
    error: Optional[str] = None
    from_cache: bool = False
    fetched_at: str = ""
    blocked: bool = False           # declined by robots.txt (a policy skip)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200 and self.body is not None


class PoliteFetcher:
    """GET-only fetcher enforcing the politeness rules:

    max 1 request/second/domain, identifying User-Agent, 15 s timeout,
    1 h response cache (SQLite), robots.txt respected per domain.
    """

    def __init__(self, settings: dict, conn: Database,
                 transport: Optional[httpx.BaseTransport] = None):
        self.settings = settings
        self.conn = conn
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, object] = {}
        # follow_redirects=False: we follow hops manually so the throttle and
        # robots.txt check apply to EVERY hop, not just the first URL.
        self._client = httpx.Client(
            headers={"User-Agent": settings["user_agent"],
                     "Accept-Language": "da, en;q=0.8"},
            timeout=settings["timeout_seconds"],
            follow_redirects=False,
            transport=transport,
        )
        self._max_redirects = 5

    def close(self) -> None:
        self._client.close()

    # -- internals ----------------------------------------------------------

    def _cache_get(self, url: str) -> Optional[FetchResult]:
        ttl = self.settings["cache_ttl_seconds"]
        row = self.conn.execute(
            "SELECT fetched_at, status, body FROM http_cache WHERE url = :url",
            {"url": url}).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        if datetime.now() - fetched > timedelta(seconds=ttl):
            return None
        return FetchResult(url=url, status=row["status"], body=row["body"],
                           from_cache=True, fetched_at=row["fetched_at"])

    def _cache_put(self, url: str, status: int, body: str) -> None:
        # Portable upsert: delete-then-insert (no dialect-specific ON CONFLICT).
        self.conn.execute("DELETE FROM http_cache WHERE url = :url",
                          {"url": url})
        self.conn.execute(
            "INSERT INTO http_cache (url, fetched_at, status, body) "
            "VALUES (:url, :fetched_at, :status, :body)",
            {"url": url, "fetched_at": now_iso(), "status": status,
             "body": body})

    def _throttle(self, domain: str) -> None:
        interval = self.settings["min_request_interval_seconds"]
        last = self._last_request.get(domain)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request[domain] = time.monotonic()

    def _single_get(self, url: str) -> httpx.Response:
        """One throttled GET, no redirect following. Raises on transport error."""
        domain = urllib.parse.urlsplit(url).netloc
        self._throttle(domain)
        return self._client.get(url)

    def _fetch_robots(self, robots_url: str) -> tuple[Optional[int], str]:
        """Fetch robots.txt (following redirects politely). Returns (status, body).
        status is None on a transport error."""
        current = robots_url
        for _ in range(self._max_redirects + 1):
            try:
                resp = self._single_get(current)
            except Exception as e:  # noqa: BLE001 — surface as "unreachable"
                return None, f"{type(e).__name__}: {e}"
            if resp.is_redirect and resp.headers.get("location"):
                current = str(resp.url.join(resp.headers["location"]))
                continue
            return resp.status_code, resp.text
        return None, "too many redirects fetching robots.txt"

    def _robots_allows(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        decision = self._robots.get(origin)
        if decision is None:
            robots_url = origin + "/robots.txt"
            cached = self._cache_get(robots_url)
            if cached is not None:
                status, body = cached.status, cached.body or ""
            else:
                status, body = self._fetch_robots(robots_url)
                # Cache ONLY definitive answers (RFC 9309): 200 = rules,
                # 4xx = no rules. NEVER cache a 5xx/transport error as
                # allow-all — a transient hiccup must not open the domain
                # for a full hour.
                if status == 200 or (status is not None and 400 <= status < 500):
                    self._cache_put(robots_url, status, body)
            rp = urllib.robotparser.RobotFileParser()
            if status == 200 and body:
                rp.parse(body.splitlines())
                decision = rp
            elif status is not None and 400 <= status < 500:
                rp.parse([])            # no robots.txt → crawl allowed
                decision = rp
            else:
                # 5xx or unreachable → treat as disallowed for now (fail
                # closed); a later scan (new fetcher) will retry.
                decision = "unreachable"
            self._robots[origin] = decision
        if decision == "unreachable":
            return False
        return decision.can_fetch(self.settings["user_agent"], url)

    # -- public -------------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        """Fetch url, following redirects with throttle + robots on every hop."""
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        current = url
        for _ in range(self._max_redirects + 1):
            try:
                urllib.parse.urlsplit(current)
            except ValueError as e:
                return FetchResult(url=current, error=f"malformed URL: {e}",
                                   fetched_at=now_iso())
            if not self._robots_allows(current):
                where = "" if current == url else f" (redirect target {current})"
                return FetchResult(url=url, blocked=True,
                                   error=f"blocked by robots.txt{where}",
                                   fetched_at=now_iso())
            try:
                resp = self._single_get(current)
            except Exception as e:  # noqa: BLE001 — config/transport problem
                return FetchResult(url=current, error=f"{type(e).__name__}: {e}",
                                   fetched_at=now_iso())
            if resp.is_redirect and resp.headers.get("location"):
                current = str(resp.url.join(resp.headers["location"]))
                continue
            result = FetchResult(url=str(resp.url), status=resp.status_code,
                                 body=resp.text, fetched_at=now_iso())
            if result.ok:
                self._cache_put(url, result.status, result.body)
            return result
        return FetchResult(url=url, error="too many redirects",
                           fetched_at=now_iso())


# ---------------------------------------------------------------------------
# Parsers — each takes raw text and returns plain data (unit-testable)
# ---------------------------------------------------------------------------

@dataclass
class ParsedPrice:
    title: str = ""
    price: Optional[float] = None      # in the shop's native currency
    in_stock: Optional[bool] = None    # None = unknown
    allocation_risk: bool = False
    error: Optional[str] = None


def parse_shopify_product_js(body: str) -> ParsedPrice:
    """Parse a Shopify /products/<handle>.js payload (price in øre)."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return ParsedPrice(error=f"invalid JSON: {e}")
    variants = data.get("variants") or []
    available_prices = [v["price"] for v in variants if v.get("available")]
    prices = available_prices or [v["price"] for v in variants if "price" in v]
    if not prices and "price" not in data:
        return ParsedPrice(title=data.get("title", ""), error="no price in payload")
    minor = min(prices) if prices else data["price"]
    if variants:
        in_stock: Optional[bool] = bool(available_prices)
    else:
        in_stock = bool(data["available"]) if "available" in data else None
    return ParsedPrice(title=data.get("title", ""), price=minor / 100.0,
                       in_stock=in_stock)


def parse_shopify_products_json(body: str, query_words: list[str],
                                exclude_words: list[str] | None = None,
                                handle: str | None = None) -> ParsedPrice:
    """Find the best title match in a /products.json?limit=250 payload."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return ParsedPrice(error=f"invalid JSON: {e}")
    products = data.get("products") or []
    exclude_words = [w.lower() for w in (exclude_words or [])]
    query_words = [w.lower() for w in query_words]

    best: Optional[dict] = None
    best_score = 0.0
    for p in products:
        title = (p.get("title") or "").lower()
        if handle and p.get("handle") == handle:
            best = p
            break
        if not all(w in title for w in query_words):
            continue
        if any(w in title for w in exclude_words):
            continue
        score = sum(len(w) for w in query_words) / max(len(title), 1)
        if score > best_score:
            best, best_score = p, score
    if best is None:
        return ParsedPrice(error=f"no product matching {query_words!r}")

    variants = best.get("variants") or []
    avail = [float(v["price"]) for v in variants if v.get("available")]
    all_prices = [float(v["price"]) for v in variants if "price" in v]
    if not all_prices:
        return ParsedPrice(title=best.get("title", ""), error="no variant price")
    return ParsedPrice(
        title=best.get("title", ""),
        price=min(avail) if avail else min(all_prices),
        in_stock=bool(avail),
    )


def extract_dk_price(text: str) -> Optional[float]:
    """Find a Danish price inside arbitrary text, preferring amounts anchored
    to a currency marker so counts like '(36 packs)' are never mistaken for
    a price."""
    if not text:
        return None
    t = text.replace("\xa0", " ")
    m = re.search(
        r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:DKK|kr\.?)(?![A-Za-zæøåÆØÅ])",
        t, re.IGNORECASE)
    if not m:
        m = re.search(
            r"(?<![A-Za-zæøåÆØÅ])(?:DKK|kr\.?)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)",
            t, re.IGNORECASE)
    if m:
        return parse_danish_price(m.group(1))
    m = re.search(r"\d{1,3}(?:\.\d{3})*,\d{2}", t)  # comma decimals = a price
    if m:
        return parse_danish_price(m.group(0))
    return None


_KELZ0R_STOCK_IN = re.compile(r"(?i)på lager")
_KELZ0R_STOCK_OUT = re.compile(r"(?i)(udsolgt|ikke på lager|ikke paa lager|sold out)")
_KELZ0R_ALLOC = re.compile(r"(?i)risiko for allokering")


def parse_kelz0r_product(body: str,
                         query_words: list[str] | None = None) -> ParsedPrice:
    """Parse a kelz0r.dk product page (…-p-NNN.html).

    Real markup uses schema.org microdata:
      <h1><span itemprop="name">…</span></h1>
      <span itemprop="price" content="1499.95">1.499,95 kr</span>
      <link itemprop="availability" href="http://schema.org/InStock" />
    """
    tree = HTMLParser(body)
    query_words = [w.lower() for w in (query_words or [])]
    h1 = tree.css_first("h1")
    title = h1.text(strip=True) if h1 else ""
    if query_words and not all(w in title.lower() for w in query_words):
        return ParsedPrice(title=title,
                           error=f"page title {title!r} does not match "
                                 f"{query_words!r} — wrong/redirected page?")

    price: Optional[float] = None
    node = tree.css_first('[itemprop="price"]')
    if node is not None:
        raw = node.attributes.get("content") or node.text(strip=True)
        try:
            price = float(str(raw))
        except ValueError:
            price = parse_danish_price(str(raw))
    if price is None:
        for span in tree.css(".proinfoprice"):
            price = extract_dk_price(span.text(separator=" "))
            if price is not None:
                break
    if price is None:
        return ParsedPrice(title=title, error="no price found in product page")

    in_stock: Optional[bool] = None
    avail = tree.css_first('[itemprop="availability"]')
    href = (avail.attributes.get("href") or "") if avail is not None else ""
    if "InStock" in href:
        in_stock = True
    elif "OutOfStock" in href or "SoldOut" in href:
        in_stock = False
    ctx = tree.text(separator=" ")
    return ParsedPrice(title=title, price=price, in_stock=in_stock,
                       allocation_risk=bool(_KELZ0R_ALLOC.search(ctx)))


def parse_kelz0r(body: str, query_words: list[str] | None = None,
                 exclude_words: list[str] | None = None) -> ParsedPrice:
    """Parse a kelz0r.dk (osCommerce) search-result / listing page.

    Collects candidate blocks around product links (-p-NNN.html) and picks
    the first query match whose context contains a price — nav menus and
    breadcrumbs also link to products but carry no price, so they're skipped.
    """
    tree = HTMLParser(body)
    query_words = [w.lower() for w in (query_words or [])]
    exclude_words = [w.lower() for w in (exclude_words or [])]

    candidates: list[tuple[str, str]] = []  # (title, context text)
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        if not re.search(r"-p-\d+\.html", href):
            continue
        title = a.text(strip=True)
        if not title:
            continue
        # Context = the nearest table row / list item containing the link.
        node = a
        for _ in range(6):
            if node.parent is None:
                break
            node = node.parent
            if node.tag in ("tr", "li", "div") and len(node.text()) > len(title):
                break
        candidates.append((title, node.text(separator=" ")))

    def matches(title: str) -> bool:
        t = title.lower()
        return (all(w in t for w in query_words)
                and not any(w in t for w in exclude_words))

    matched = [(t, ctx) for t, ctx in candidates if matches(t)] if query_words \
        else candidates

    for title, ctx in matched:
        price = extract_dk_price(ctx)
        if price is None:
            continue
        in_stock: Optional[bool] = None
        if _KELZ0R_STOCK_OUT.search(ctx):
            in_stock = False
        elif _KELZ0R_STOCK_IN.search(ctx):
            in_stock = True
        return ParsedPrice(title=title, price=price, in_stock=in_stock,
                           allocation_risk=bool(_KELZ0R_ALLOC.search(ctx)))

    # Maybe the URL landed on a single product page (redirect): microdata.
    if tree.css_first('[itemprop="price"]') is not None:
        return parse_kelz0r_product(body, query_words)
    if matched:
        return ParsedPrice(title=matched[0][0], error="no price found in page")
    return ParsedPrice(error=f"no kelz0r product matching {query_words!r}")


def parse_epicpanda(body: str) -> ParsedPrice:
    """Parse an epicpanda.dk (DanDomain) product page ('1.999,95 DKK').

    The shop answers unknown product URLs with HTTP 200 + the frontpage
    (canonical rel link → frontpage.html) — that must parse as an error,
    never as a price (its basket widget contains '0,00 DKK')."""
    tree = HTMLParser(body)
    canonical = tree.css_first('link[rel="canonical"]')
    if canonical is not None and "frontpage" in \
            (canonical.attributes.get("href") or ""):
        return ParsedPrice(error="shop redirected to frontpage — product URL "
                                 "is wrong or the product is gone")
    title = ""
    for h1 in tree.css("h1"):
        title = h1.text(strip=True)
        if title:
            break
    if not title:
        return ParsedPrice(error="no product title (h1) on page — "
                                 "not a product page?")

    price: Optional[float] = None
    # Prefer machine-readable price metadata when present.
    for node in tree.css('[itemprop="price"], meta[property="product:price:amount"]'):
        raw = node.attributes.get("content") or node.text()
        if raw:
            try:
                price = float(str(raw).replace(",", "."))
                break
            except ValueError:
                price = parse_danish_price(str(raw))
                if price is not None:
                    break
    text = tree.text(separator=" ")
    if price is None:
        m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*(?:DKK|kr)", text)
        if m:
            price = parse_danish_price(m.group(1))
    if price is None:
        return ParsedPrice(title=title, error="no price found in page")

    in_stock: Optional[bool] = None
    if re.search(r"(?i)(udsolgt|ikke på lager|sold out)", text):
        in_stock = False
    elif re.search(r"(?i)(på lager|læg i kurv|add to cart|køb)", text):
        in_stock = True
    return ParsedPrice(title=title, price=price, in_stock=in_stock)


def parse_pricecharting(body: str, pc_field: str = "used") -> ParsedPrice:
    """Parse a pricecharting.com product page. Returns USD (reference only).

    pc_field: which price column to read. Sealed products list their price in
    the 'Loose Price' column = id 'used_price' (that is the default); the
    'new'/'complete' columns are usually empty ('-') for sealed. If the
    requested column is empty we fall back across the others so a real number
    is used rather than nothing.
    """
    tree = HTMLParser(body)
    h1 = tree.css_first("h1#product_name") or tree.css_first("h1")
    title = h1.text(strip=True) if h1 else ""
    order = [pc_field] + [f for f in ("used", "new", "complete")
                          if f != pc_field]
    for field in order:
        cell = tree.css_first(f"#{field}_price .price, td#{field}_price")
        if cell is None:
            continue
        price = parse_usd_price(cell.text(strip=True))
        if price is not None and price > 0:
            return ParsedPrice(title=title, price=price, in_stock=None)
    return ParsedPrice(title=title, error="no USD price found")


_JSONLD_IN_STOCK = ("instock", "preorder", "presale", "limitedavailability")
_JSONLD_NO_STOCK = ("outofstock", "soldout", "discontinued")


def _jsonld_offer_price(offer: dict) -> tuple[Optional[float], str]:
    """Current selling price + currency from a schema.org Offer.

    Handles a direct ``offer.price`` / ``offer.lowPrice`` and the WooCommerce
    shape where price lives in ``offer.priceSpecification[]`` (a list of
    UnitPriceSpecification). The genuine selling price is the spec WITHOUT a
    ListPrice priceType (ListPrice is the struck-through 'before' price) — take
    the lowest such, else the lowest overall. Returns (None, "") if no price.
    """
    for key in ("price", "lowPrice"):
        raw = offer.get(key)
        if raw is not None:
            try:
                return float(raw), str(offer.get("priceCurrency") or "")
            except (TypeError, ValueError):
                pass
    specs = offer.get("priceSpecification")
    if isinstance(specs, dict):
        specs = [specs]
    current, listed = [], []
    for sp in specs or []:
        if not isinstance(sp, dict):
            continue
        try:
            val = float(sp.get("price"))
        except (TypeError, ValueError):
            continue
        pair = (val, str(sp.get("priceCurrency") or ""))
        is_list = "listprice" in str(sp.get("priceType") or "").lower()
        (listed if is_list else current).append(pair)
    pool = current or listed
    if pool:
        return min(pool, key=lambda t: t[0])
    return None, ""


def parse_jsonld_product(body: str, query_words: Optional[list] = None,
                         expected_currency: str = "DKK") -> ParsedPrice:
    """Parse any product page embedding schema.org JSON-LD Product data
    (<script type="application/ld+json">). Generic: works for br.dk and most
    modern webshop platforms without a bespoke parser.

    query_words: like kelz0r_product's guard — if given and not all present in
    the product name, the page is treated as wrong/repurposed (error, never a
    wrong price). A priceCurrency mismatch with the source's configured
    currency is likewise an error rather than a silently mis-labelled number.
    """
    tree = HTMLParser(body)
    products = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (ValueError, TypeError):
            continue                        # malformed block — try the next
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            graph = it.get("@graph")
            candidates = graph if isinstance(graph, list) else [it]
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                t = c.get("@type")
                types = t if isinstance(t, list) else [t]
                if "Product" in types:
                    products.append(c)
    if not products:
        return ParsedPrice(error="no JSON-LD Product data on page")

    p = products[0]
    title = str(p.get("name") or "")
    if query_words:
        low = title.lower()
        if not all(w.lower() in low for w in query_words):
            return ParsedPrice(title=title, error=(
                f"JSON-LD product {title!r} does not match {query_words} "
                "— wrong/repurposed page?"))
    offers = p.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return ParsedPrice(title=title, error="no usable JSON-LD offer")
    price, spec_currency = _jsonld_offer_price(offers)
    if price is None:
        return ParsedPrice(title=title, error="no price in JSON-LD offer")
    currency = str(offers.get("priceCurrency") or spec_currency or "").upper()
    if currency and currency != expected_currency.upper():
        return ParsedPrice(title=title, error=(
            f"JSON-LD priceCurrency {currency} != configured "
            f"{expected_currency} — refusing to mis-label the price"))
    availability = str(offers.get("availability") or "").lower()
    if any(k in availability for k in _JSONLD_NO_STOCK):
        in_stock = False
    elif any(k in availability for k in _JSONLD_IN_STOCK):
        in_stock = True
    else:
        in_stock = None
    return ParsedPrice(title=title, price=price, in_stock=in_stock)


def parse_faraos(body: str, query_words: Optional[list] = None,
                 requested_url: Optional[str] = None) -> ParsedPrice:
    """Parse a faraos.dk product page (Angular SSR — no JSON-LD).

    The main product price is a clean attribute on the single
    ``<div data-view="product" data-price="3979.00">`` element, so we read that
    rather than the 739 KB escaped Angular state blob.

    SOFT-404 GUARD: a dead/removed slug still returns HTTP 200 but renders the
    parent CATEGORY, with ``og:url`` pointing at that category rather than the
    requested product. When ``requested_url`` is given we require the page's
    ``og:url`` path to match it — otherwise it's a category fallback, reported
    as an honest not-found instead of a wrong (neighbouring) price.
    """
    tree = HTMLParser(body)

    def _meta(prop: str) -> Optional[str]:
        m = re.search(rf'<meta property="{prop}" content="([^"]*)"', body)
        return m.group(1) if m else None

    if requested_url:
        og_url = _meta("og:url")
        if og_url:
            want = urllib.parse.urlsplit(requested_url).path.rstrip("/")
            got = urllib.parse.urlsplit(og_url).path.rstrip("/")
            if want != got:
                return ParsedPrice(error=(
                    f"Faraos page resolved to {got!r} not {want!r} — dead "
                    "product (soft-404 to category), refusing to record"))

    node = tree.css_first('div[data-view="product"][data-price]')
    if node is None:
        return ParsedPrice(error="no Faraos product price block on page")
    title = _meta("og:title") or ""
    if query_words and title:
        low = title.lower()
        if not all(w.lower() in low for w in query_words):
            return ParsedPrice(title=title, error=(
                f"Faraos product {title!r} does not match {query_words} "
                "— wrong page?"))
    try:
        price = float(node.attributes.get("data-price"))
    except (TypeError, ValueError):
        return ParsedPrice(title=title, error="no numeric Faraos data-price")

    low_body = body.lower()
    if "udsolgt" in low_body:
        in_stock = False
    elif "læg i kurv" in low_body or "forudbestil" in low_body:
        in_stock = True                 # buyable (in stock or pre-order)
    else:
        in_stock = None
    return ParsedPrice(title=title, price=price, in_stock=in_stock)


def parse_riporflip(body: str, set_name: str) -> ParsedPrice:
    """Extract a Rip-EV percentage for a set from riporfliptcg.com.

    Best-effort: finds the set name in the page and takes the nearest
    percentage figure. price is the EV in percent (e.g. 108.0)."""
    tree = HTMLParser(body)
    text = tree.text(separator=" ")
    idx = text.lower().find(set_name.lower())
    if idx == -1:
        return ParsedPrice(error=f"set {set_name!r} not found on page")
    window = text[idx:idx + 400]
    m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%", window)
    if not m:
        return ParsedPrice(title=set_name, error="no EV %% near set name")
    return ParsedPrice(title=set_name,
                       price=float(m.group(1).replace(",", ".")), in_stock=None)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    product_id: str
    shop: str
    url: str
    method: str
    observed_at: str
    title: str = ""
    price_native: Optional[float] = None
    currency: str = "DKK"
    price_dkk: Optional[float] = None
    landed_dkk: Optional[float] = None
    in_stock: Optional[bool] = None
    status: str = "error"
    error: Optional[str] = None
    allocation_risk: bool = False
    reference_only: bool = False
    source_kind: str = "html"
    added_by: Optional[str] = None      # who entered a manual row; None if scraped


def _to_dkk(amount: float, currency: str, settings: dict) -> float:
    if currency == "DKK":
        return amount
    if currency == "EUR":
        return amount * EUR_DKK
    if currency == "USD":
        return amount * settings["usd_dkk"]
    raise ValueError(f"unsupported currency {currency}")


def _fixture_name(url: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")
    return slug[-120:] + (".json" if ".json" in url or url.endswith(".js") else ".html")


def record_fixture(url: str, body: str) -> None:
    """Save a real response as a fixture — only on first success per URL."""
    FIXTURES_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_LIVE_DIR / _fixture_name(url)
    if not path.exists():
        path.write_text(body, encoding="utf-8")


def resolve_shipping_dkk(source: dict, settings: dict) -> float:
    """Effective shipping for a source: its own shipping_dkk wins, else the
    per-shop estimate in settings.shop_shipping_dkk, else 0 (unknown — the UI
    marks such landed prices with * so 'landed' never quietly means 'shelf')."""
    own = source.get("shipping_dkk")
    if own not in (None, ""):
        return float(own)
    per_shop = settings.get("shop_shipping_dkk") or {}
    return float(per_shop.get(source.get("shop"), 0) or 0)


def shipping_known_shops(cfg: dict) -> set:
    """Shops whose landed price includes a real shipping figure: any shop with
    a per-shop estimate, plus shops whose every source sets shipping_dkk."""
    settings = cfg.get("settings", {})
    known = {s for s, v in (settings.get("shop_shipping_dkk") or {}).items()
             if v not in (None, "")}
    for p in cfg.get("products", []):
        for src in p.get("sources", []):
            if src.get("shipping_dkk") not in (None, ""):
                known.add(src["shop"])
    return known


def scan_source(product: dict, source: dict, fetcher: PoliteFetcher,
                settings: dict) -> Observation:
    """Fetch and parse one configured source. Never raises."""
    method = source["method"]
    url = source["url"]
    obs = Observation(
        product_id=product["id"], shop=source["shop"], url=url, method=method,
        observed_at=now_iso(),
        currency=source.get("currency", "DKK"),
        reference_only=bool(source.get("reference_only")),
        source_kind="shopify" if method.startswith("shopify") else "html",
    )
    # Geo-priced shops (Shopify Markets): flinamania.dk serves USD prices to
    # non-Danish IPs, so scanning it from a US-based CI runner records wrong
    # numbers (caught by the plausibility band on the first cron run). Such
    # sources are marked requires_dk_ip and are policy-SKIPPED — not failed —
    # when the scan runs from a non-DK vantage (settings.non_dk_vantage, set
    # by the scheduled-scan entry). App scans from Denmark are unaffected.
    if source.get("requires_dk_ip") and settings.get("non_dk_vantage"):
        obs.status = "skipped"
        obs.error = "geo-priced shop — only checked from Denmark"
        return obs

    if method == "cardmarket_manual":
        obs.source_kind = "manual"
        obs.status = "manual"
        obs.error = "manual entry — use the Cardmarket field in the UI"
        return obs

    try:
        if method == "shopify_handle":
            fetch = fetcher.get(url.rstrip("/") + ".js")
            parsed = parse_shopify_product_js(fetch.body) if fetch.ok else None
            if fetch.ok and parsed.error:
                # Handle endpoint changed? Fall back to the products.json list.
                origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
                handle = url.rstrip("/").rsplit("/", 1)[-1]
                fetch = fetcher.get(f"{origin}/products.json?limit=250")
                parsed = (parse_shopify_products_json(fetch.body, [], handle=handle)
                          if fetch.ok else None)
        elif method == "shopify_search":
            parsed = None
            fetch = None
            origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
            for page in range(1, 9):
                fetch = fetcher.get(f"{origin}/products.json?limit=250&page={page}")
                if not fetch.ok:
                    break
                # Some shops answer out-of-range pages with an HTML page (200
                # OK). Detect that BEFORE overwriting `parsed`, so the honest
                # "no product matching …" error from real pages is preserved
                # rather than replaced by an "invalid JSON" message.
                try:
                    products = json.loads(fetch.body).get("products")
                except json.JSONDecodeError:
                    break
                page_parsed = parse_shopify_products_json(
                    fetch.body, source.get("query", "").split(),
                    source.get("exclude", []))
                if page_parsed.error is None:
                    parsed = page_parsed
                    break
                parsed = page_parsed  # keep last real "no match" error
                if not products:
                    break
        elif method == "kelz0r_search":
            fetch = fetcher.get(url)
            parsed = parse_kelz0r(fetch.body, source.get("query", "").split() or None,
                                  source.get("exclude", [])) if fetch.ok else None
        elif method == "kelz0r_product":
            fetch = fetcher.get(url)
            parsed = (parse_kelz0r_product(fetch.body,
                                           source.get("query", "").split() or None)
                      if fetch.ok else None)
        elif method == "epicpanda":
            fetch = fetcher.get(url)
            parsed = parse_epicpanda(fetch.body) if fetch.ok else None
        elif method == "jsonld_product":
            fetch = fetcher.get(url)
            parsed = (parse_jsonld_product(
                fetch.body, source.get("query", "").split() or None,
                expected_currency=obs.currency)
                if fetch.ok else None)
        elif method == "faraos":
            fetch = fetcher.get(url)
            parsed = (parse_faraos(
                fetch.body, source.get("query", "").split() or None,
                requested_url=url)
                if fetch.ok else None)
        elif method == "pricecharting":
            fetch = fetcher.get(url)
            parsed = (parse_pricecharting(fetch.body, source.get("pc_field", "new"))
                      if fetch.ok else None)
        elif method == "riporflip":
            fetch = fetcher.get(url)
            parsed = (parse_riporflip(fetch.body, source.get("set_name", ""))
                      if fetch.ok else None)
        else:
            obs.error = f"unknown method {method!r}"
            return obs
    except Exception as e:  # a parser bug must show as UNVERIFIED, not crash
        obs.error = f"parser crashed: {type(e).__name__}: {e}"
        return obs

    if fetch is not None and fetch.blocked:
        # A robots.txt policy block is a permanent, known SKIP — not a
        # transient failure. It must not poison an otherwise-verified verdict;
        # product_verdict surfaces it as a note instead.
        obs.status = "skipped"
        obs.error = fetch.error
        return obs
    if fetch is None or not fetch.ok:
        obs.error = (fetch.error if fetch and fetch.error
                     else f"HTTP {fetch.status}" if fetch else "no fetch")
        return obs
    if parsed is None or parsed.error:
        obs.error = parsed.error if parsed else "parse failed"
        obs.title = parsed.title if parsed else ""
        return obs
    if parsed.price is None or parsed.price <= 0:
        # A 0/negative "price" is always a misparse (basket widgets, gift
        # counters …) — refuse to record it as a verified value.
        obs.title = parsed.title
        obs.error = f"implausible price {parsed.price!r} — refusing to record"
        return obs

    obs.title = parsed.title
    obs.price_native = parsed.price
    obs.in_stock = parsed.in_stock
    obs.allocation_risk = parsed.allocation_risk
    if method == "riporflip":
        obs.currency = "%"          # EV percentage, not a price
        obs.price_dkk = None
        obs.landed_dkk = None
    else:
        try:
            obs.price_dkk = _to_dkk(parsed.price, obs.currency, settings)
        except ValueError as e:
            # Bad "currency" in config must fail THIS source, not abort the
            # whole scan (scan_source promises never to raise).
            obs.error = str(e)
            return obs
        obs.landed_dkk = obs.price_dkk + resolve_shipping_dkk(source, settings)
        # Plausibility band (per-product, optional): with scheduled scans
        # running unattended, a silent mis-parse (a pack price recorded as a
        # box) could ping Discord with a bogus BUY and nobody would be at the
        # screen to catch it. Outside the band → refuse, like price <= 0.
        triggers = product.get("triggers", {})
        lo = triggers.get("plausible_min_dkk")
        hi = triggers.get("plausible_max_dkk")
        if (lo is not None and obs.landed_dkk < float(lo)) or \
           (hi is not None and obs.landed_dkk > float(hi)):
            obs.error = (f"implausible landed price {obs.landed_dkk:.2f} kr "
                         f"(outside plausible band {lo}–{hi}) — refusing to "
                         "record")
            obs.price_native = obs.price_dkk = obs.landed_dkk = None
            obs.in_stock = None
            obs.status = "error"
            return obs
    obs.status = "ok"
    if settings.get("record_fixtures") and not fetch.from_cache:
        try:
            record_fixture(fetch.url, fetch.body)
        except OSError:
            pass
    return obs


def insert_observation(conn: Database, obs: Observation,
                       scan_id: Optional[int]) -> None:
    conn.execute(
        """INSERT INTO observations
           (scan_id, product_id, shop, url, method, title, observed_at,
            price_native, currency, price_dkk, landed_dkk, in_stock, status,
            error, allocation_risk, reference_only, source_kind, added_by)
           VALUES (:scan_id, :product_id, :shop, :url, :method, :title,
            :observed_at, :price_native, :currency, :price_dkk, :landed_dkk,
            :in_stock, :status, :error, :allocation_risk, :reference_only,
            :source_kind, :added_by)""",
        {"scan_id": scan_id, "product_id": obs.product_id, "shop": obs.shop,
         "url": obs.url, "method": obs.method, "title": obs.title,
         "observed_at": obs.observed_at, "price_native": obs.price_native,
         "currency": obs.currency, "price_dkk": obs.price_dkk,
         "landed_dkk": obs.landed_dkk,
         "in_stock": None if obs.in_stock is None else int(obs.in_stock),
         "status": obs.status, "error": obs.error,
         "allocation_risk": int(obs.allocation_risk),
         "reference_only": int(obs.reference_only),
         "source_kind": obs.source_kind, "added_by": obs.added_by})


def add_manual_cardmarket_entry(conn: Database, product: dict,
                                price_eur: float, settings: dict,
                                added_by: Optional[str] = None) -> Observation:
    """Store a manually entered Cardmarket price like scraped data.

    `added_by` (a person's name) is recorded for shared deployments so the
    collection can show who entered each price; None for single-user runs.
    """
    cm = product.get("cardmarket", {})
    shipping = float(cm.get("shipping_dkk", 0))
    obs = Observation(
        product_id=product["id"], shop="cardmarket", url=cm.get("url", ""),
        method="cardmarket_manual", observed_at=now_iso(),
        title=product["name"], price_native=price_eur, currency="EUR",
        price_dkk=price_eur * EUR_DKK, landed_dkk=price_eur * EUR_DKK + shipping,
        in_stock=True, status="ok", source_kind="manual", added_by=added_by)
    insert_observation(conn, obs, scan_id=None)
    return obs


def run_scan(cfg: dict, conn: Database,
             progress=None) -> tuple[int, list[Observation]]:
    """Scan every configured source of every product. Returns (scan_id, obs).

    progress: optional callback(product_name, shop, obs) for UI updates.
    """
    settings = cfg["settings"]
    scan_id = conn.insert_scan(now_iso())
    fetcher = PoliteFetcher(settings, conn)
    observations: list[Observation] = []
    try:
        for product in cfg["products"]:
            for source in product.get("sources", []):
                obs = scan_source(product, source, fetcher, settings)
                if obs.status != "manual":
                    insert_observation(conn, obs, scan_id)
                    observations.append(obs)
                if progress:
                    progress(product["name"], source["shop"], obs)
    finally:
        fetcher.close()
        conn.execute("UPDATE scans SET finished_at = :f WHERE id = :id",
                     {"f": now_iso(), "id": scan_id})
    return scan_id, observations


# ---------------------------------------------------------------------------
# History / trends
# ---------------------------------------------------------------------------

def latest_observations(conn: Database, product_id: str,
                        sources: Optional[list] = None) -> list[dict]:
    """Latest observation per shop+method for a product (any status).

    If `sources` (the product's current config sources) is given, rows are
    restricted to the (shop, method) pairs still configured — so a source the
    user removed or renamed in the Config tab can never wedge the product in a
    stale BUY or a permanent UNVERIFIED from its last historical row.
    """
    rows = conn.execute(
        """SELECT o.* FROM observations o
           JOIN (SELECT shop, method, MAX(id) AS mid FROM observations
                 WHERE product_id = :pid GROUP BY shop, method) x
             ON o.id = x.mid ORDER BY o.shop""",
        {"pid": product_id}).fetchall()
    if sources is None:
        return rows
    allowed = {(s["shop"], s["method"]) for s in sources}
    return [r for r in rows if (r["shop"], r["method"]) in allowed]


def last_good_observation(conn: Database, product_id: str,
                          shop: str, method: str) -> Optional[dict]:
    return conn.execute(
        """SELECT * FROM observations
           WHERE product_id = :pid AND shop = :shop AND method = :method
             AND status = 'ok'
           ORDER BY id DESC LIMIT 1""",
        {"pid": product_id, "shop": shop, "method": method}).fetchone()


def daily_cheapest_series(conn: Database, product_id: str,
                          days: int = 60) -> list[tuple[str, float]]:
    """Per-day minimum verified in-stock landed price across DK shop sources.

    Manual Cardmarket entries are EXCLUDED: they are sporadic, so letting them
    into a per-day minimum manufactures phantom rises/falls on the days they
    happen to be entered. This mirrors product_verdict, which also computes
    'cheapest' from shop sources only. (Matches the spec's 'never fake trends'.)
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT substr(observed_at, 1, 10) AS day, MIN(landed_dkk) AS price
           FROM observations
           WHERE product_id = :pid AND status = 'ok' AND in_stock = 1
             AND reference_only = 0 AND source_kind != 'manual'
             AND landed_dkk > 0 AND observed_at >= :since
           GROUP BY day ORDER BY day""",
        {"pid": product_id, "since": since}).fetchall()
    return [(r["day"], r["price"]) for r in rows]


def shop_series(conn: Database, product_id: str,
                days: int = 60) -> dict[str, list[tuple[str, float]]]:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT shop, substr(observed_at, 1, 10) AS day,
                  MIN(landed_dkk) AS price
           FROM observations
           WHERE product_id = :pid AND status = 'ok' AND landed_dkk > 0
             AND observed_at >= :since
           GROUP BY shop, day ORDER BY day""",
        {"pid": product_id, "since": since}).fetchall()
    out: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        out.setdefault(r["shop"], []).append((r["day"], r["price"]))
    return out


def cardmarket_series(conn: Database, product_id: str,
                      days: int = 365) -> list[tuple[str, float]]:
    """Manually-entered Cardmarket prices over time, as (timestamp, EUR).

    The inverse of daily_cheapest_series: ONLY manual Cardmarket entries
    (source_kind='manual'), plotted as-entered rather than grouped per day —
    CM entries are sporadic, so a per-day MIN would manufacture phantom trends
    (see daily_cheapest_series). price_native is the honest signal: the EUR the
    user actually typed, not the EUR_DKK-pegged derived DKK.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT observed_at, price_native FROM observations
           WHERE product_id = :pid AND source_kind = 'manual' AND status = 'ok'
             AND price_native IS NOT NULL AND observed_at >= :since
           ORDER BY observed_at""",
        {"pid": product_id, "since": since}).fetchall()
    return [(r["observed_at"], r["price_native"]) for r in rows]


@dataclass
class Trend:
    today: Optional[float] = None
    avg7: Optional[float] = None       # None = insufficient history
    avg30: Optional[float] = None
    days7: int = 0
    days30: int = 0

    def pct_vs(self, avg: Optional[float]) -> Optional[float]:
        if self.today is None or not avg:
            return None
        return (self.today - avg) / avg * 100.0


def compute_trend(conn: Database, product_id: str,
                  settings: dict) -> Trend:
    series = daily_cheapest_series(conn, product_id, days=30)
    today_str = date.today().isoformat()
    cut7 = (date.today() - timedelta(days=7)).isoformat()
    cut30 = (date.today() - timedelta(days=30)).isoformat()
    trend = Trend()
    by_day = dict(series)
    trend.today = by_day.get(today_str)
    # Averages are over the days STRICTLY BEFORE today, within each window.
    d7 = [(d, p) for d, p in series if cut7 <= d < today_str]
    d30 = [(d, p) for d, p in series if cut30 <= d < today_str]
    trend.days7, trend.days30 = len(d7), len(d30)
    if len(d7) >= settings["trend_min_days_7d"]:
        trend.avg7 = sum(p for _, p in d7) / len(d7)
    if len(d30) >= settings["trend_min_days_30d"]:
        trend.avg30 = sum(p for _, p in d30) / len(d30)
    return trend


def cardmarket_stability(conn: Database, product_id: str,
                         threshold_eur: float, days: int) -> tuple[bool, str]:
    """True only when the manual CM entries actually demonstrate a price at or
    below `threshold_eur` across the WHOLE trailing `days` window.

    That needs: (a) an 'anchor' entry from at or before the window start that
    is already ≤ threshold (proving the low price held at the start, not just
    recently), and (b) every entry inside the window ≤ threshold. Without the
    anchor a pre-window spike (e.g. €250 → €180 six days ago) would be silently
    ignored. Never guesses on sparse data.
    """
    since = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT observed_at, price_native FROM observations
           WHERE product_id = :pid AND source_kind = 'manual' AND status = 'ok'
           ORDER BY observed_at""", {"pid": product_id}).fetchall()
    if not rows:
        return False, "no Cardmarket entries yet"
    recent = [r for r in rows if r["observed_at"] >= since]
    pre = [r for r in rows if r["observed_at"] < since]     # before the window
    if not pre:
        return False, (f"need ≥ {days} days of Cardmarket history (oldest entry "
                       f"is {rows[0]['observed_at'][:10]})")
    if not recent:
        return False, "no recent Cardmarket entry — last one is stale"
    anchor = pre[-1]            # the price as it stood entering the window
    if anchor["price_native"] > threshold_eur:
        return False, (f"price was €{anchor['price_native']:g} at the start of "
                       f"the {days}-day window — not stable-low for the full span")
    if any(r["price_native"] > threshold_eur for r in recent):
        return False, f"an entry in the last {days} days was above €{threshold_eur:g}"
    return True, f"stable ≤ €{threshold_eur:g} for {days} days"


# ---------------------------------------------------------------------------
# Verdict engine
# ---------------------------------------------------------------------------

VERDICT_ORDER = ["BUY", "WAIT_FALLING", "AVOID", "HOLD", "UNVERIFIED"]

VERDICT_STYLE = {
    "BUY": ("🟢", "BUY"),
    "WAIT_FALLING": ("🟡", "WAIT — falling"),
    "AVOID": ("🔴", "AVOID"),
    "HOLD": ("⚪", "HOLD / WAIT"),
    "UNVERIFIED": ("⚫", "UNVERIFIED"),
}


@dataclass
class Verdict:
    code: str
    reason: str
    shop: Optional[str] = None
    url: Optional[str] = None
    cheapest_dkk: Optional[float] = None
    cheapest_shop: Optional[str] = None
    cheapest_url: Optional[str] = None
    in_stock: Optional[bool] = None
    vs_trigger_dkk: Optional[float] = None
    trend: Optional[Trend] = None
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return VERDICT_STYLE[self.code][0]

    @property
    def label(self) -> str:
        return VERDICT_STYLE[self.code][1]


def _cheapest_key(settings: dict):
    """Sort key for picking the cheapest shop row: lowest landed price wins;
    an EXACT price tie goes to a shop listed in settings.preferred_shops
    (e.g. br.dk — 1 year of returns on unopened products makes it the better
    venue at the same price). Preference never beats a genuinely lower price."""
    preferred = settings.get("preferred_shops") or {}

    def key(r):
        return (r["landed_dkk"], 0 if r["shop"] in preferred else 1)
    return key


def _latest_cm_entry(conn: Database,
                     product_id: str) -> Optional[dict]:
    return conn.execute(
        """SELECT * FROM observations
           WHERE product_id = :pid AND source_kind = 'manual' AND status = 'ok'
           ORDER BY id DESC LIMIT 1""", {"pid": product_id}).fetchone()


def product_verdict(conn: Database, product: dict,
                    settings: dict) -> Verdict:
    """Apply the verdict rules in the documented priority order.

    0. manual strategic flag set (e.g. Riot EOL news) -> BUY  (explicit human
       override; deliberately sits ABOVE the data rules because it does not
       depend on scraped prices — any concurrent fetch failures are still
       listed in v.failures so nothing is hidden).
    1. any (buyable-source) fetch failed        -> UNVERIFIED
    2. cheapest verified in-stock <= buy_below  -> BUY   (DK shops)
       fresh manual CM entry <= cm threshold (+stability window if configured)
    3. today < 7d avg and above trigger         -> WAIT_FALLING
    4. cheapest >= avoid_above                  -> AVOID
    5. otherwise                                -> HOLD with distance to trigger
    """
    triggers = product.get("triggers", {})
    latest = latest_observations(conn, product["id"], product.get("sources", []))
    trend = compute_trend(conn, product["id"], settings)
    v = Verdict(code="HOLD", reason="", trend=trend)

    shop_rows = [r for r in latest
                 if not r["reference_only"] and r["source_kind"] != "manual"]
    # A robots.txt block is a policy SKIP (surfaced as a note), NOT a failure:
    # it is permanent, so treating it like a transient error would wedge the
    # product in UNVERIFIED forever even when other shops verified fine.
    skipped = [r for r in shop_rows if r["status"] == "skipped"]
    failures = [r for r in shop_rows if r["status"] not in ("ok", "skipped")]
    checkable = [r for r in shop_rows if r["status"] != "skipped"]
    for r in skipped:
        v.notes.append(f"{r['shop']}: not checked — {r['error']}")
    for r in failures:
        prev = last_good_observation(conn, product["id"], r["shop"], r["method"])
        last_good = (f"last good {fmt_dkk(prev['landed_dkk'])} at "
                     f"{prev['observed_at']}" if prev else "no good value ever")
        v.failures.append(f"{r['shop']}: {r['error']} ({last_good})")

    ok_rows = [r for r in shop_rows if r["status"] == "ok"]
    in_stock_rows = [r for r in ok_rows if r["in_stock"] == 1
                     and r["landed_dkk"] is not None and r["landed_dkk"] > 0]
    cheapest = min(in_stock_rows, key=_cheapest_key(settings), default=None)
    if cheapest is not None:
        v.cheapest_dkk = cheapest["landed_dkk"]
        v.cheapest_shop = cheapest["shop"]
        v.cheapest_url = cheapest["url"]
        v.in_stock = True
        reason = (settings.get("preferred_shops") or {}).get(cheapest["shop"])
        if reason:
            v.notes.append(f"✦ {cheapest['shop']}: {reason} "
                           "(foretrækkes ved prislighed)")
    for r in ok_rows:
        if r["allocation_risk"]:
            v.notes.append(f"⚠ {r['shop']}: 'Risiko for allokering' flagged")

    cm_row = _latest_cm_entry(conn, product["id"])
    buy_below = triggers.get("buy_below_dkk")
    avoid_above = triggers.get("avoid_above_dkk")
    cm_below = triggers.get("cardmarket_buy_below_eur")
    distance = (v.cheapest_dkk - buy_below
                if v.cheapest_dkk is not None and buy_below else None)
    v.vs_trigger_dkk = distance

    # A manual Cardmarket entry never auto-expires (unlike a scrape that goes
    # UNVERIFIED), so guard against an ancient entry silently driving a BUY.
    cm_stale = False
    cm_age_days: Optional[float] = None
    max_age = settings.get("cardmarket_max_age_days")
    if cm_row is not None:
        cm_age_days = (datetime.now()
                       - datetime.fromisoformat(cm_row["observed_at"])).days
        if max_age and cm_age_days > max_age:
            cm_stale = True
            v.notes.append(
                f"Cardmarket entry €{cm_row['price_native']:g} is "
                f"{cm_age_days} days old (> {max_age}d) — treated as stale; "
                "enter today's price to act on it")

    # Rule 0: manual strategic override (e.g. Riot end-of-life announcement).
    flag = triggers.get("manual_buy_flag")
    if flag and product.get("flags", {}).get(flag):
        v.code = "BUY"
        v.reason = f"manual override flag '{flag}' is set"
        if v.failures:
            v.reason += (f" — note: {len(v.failures)} source(s) failed this "
                         "scan (see failure list)")
        return v

    # Rule 1 — data hygiene beats everything data-driven.
    if failures:
        v.code = "UNVERIFIED"
        v.reason = (f"{len(failures)} of {len(checkable)} checkable sources "
                    "failed — verdict withheld, see failure list")
        return v
    if not ok_rows and cm_row is None:
        v.code = "UNVERIFIED"
        if skipped and not checkable:
            names = ", ".join(r["shop"] for r in skipped)
            v.reason = (f"no checkable source — {names} disallowed by "
                        "robots.txt; enter a Cardmarket price to get a verdict")
        else:
            v.reason = "no data yet — run a scan / enter a Cardmarket price"
        return v

    # Rule 2 — BUY triggers.
    if buy_below and cheapest is not None and cheapest["landed_dkk"] <= buy_below:
        v.code = "BUY"
        v.reason = (f"{cheapest['shop']} at {fmt_dkk(cheapest['landed_dkk'])} "
                    f"≤ trigger {fmt_dkk(buy_below, 0)}")
        v.shop, v.url = cheapest["shop"], cheapest["url"]
        release = triggers.get("not_before")
        if release and date.today().isoformat() < release:
            v.notes.append(f"preorder — releases {release}; trigger allows "
                           "preordering at/below the buy price only")
        return v
    if (cm_below and cm_row is not None and not cm_stale
            and cm_row["price_native"] <= cm_below):
        seen = cm_row["observed_at"][:10]
        stable_days = triggers.get("cardmarket_stable_days")
        if stable_days:
            ok, why = cardmarket_stability(conn, product["id"], cm_below,
                                           stable_days)
            if ok:
                v.code = "BUY"
                v.reason = (f"Cardmarket €{cm_row['price_native']:g} (entered "
                            f"{seen}) ≤ €{cm_below:g}, {why}")
                v.shop, v.url = "cardmarket", cm_row["url"]
                return v
            v.notes.append(f"Cardmarket €{cm_row['price_native']:g} ≤ "
                           f"€{cm_below:g} but not yet stable: {why}")
        else:
            v.code = "BUY"
            v.reason = (f"Cardmarket €{cm_row['price_native']:g} (entered "
                        f"{seen}) ≤ trigger €{cm_below:g}")
            v.shop, v.url = "cardmarket", cm_row["url"]
            return v

    # Rule 3 — falling price, still above trigger.
    if (trend.avg7 is not None and trend.today is not None
            and trend.today < trend.avg7):
        pct7 = trend.pct_vs(trend.avg7)
        pct30 = trend.pct_vs(trend.avg30)
        detail = f"{pct7:+.1f}% vs 7d avg"
        if pct30 is not None:
            detail += f", {pct30:+.1f}% vs 30d avg"
        v.code = "WAIT_FALLING"
        v.reason = f"falling: {detail} — above trigger, keep watching"
        return v

    # Rule 4 — AVOID ceiling.
    if avoid_above and v.cheapest_dkk is not None and v.cheapest_dkk >= avoid_above:
        v.code = "AVOID"
        v.reason = (f"cheapest {fmt_dkk(v.cheapest_dkk)} ≥ avoid ceiling "
                    f"{fmt_dkk(avoid_above, 0)}")
        return v

    # Rule 5 — HOLD with distance to trigger.
    v.code = "HOLD"
    parts = []
    if distance is not None:
        parts.append(f"{fmt_dkk(distance, 0)} above DK trigger "
                     f"{fmt_dkk(buy_below, 0)}")
    elif buy_below:
        parts.append("no verified in-stock DK price vs trigger "
                     f"{fmt_dkk(buy_below, 0)}")
    if cm_below and cm_row is not None:
        gap = cm_row["price_native"] - cm_below
        parts.append(f"Cardmarket €{cm_row['price_native']:g} is "
                     f"€{gap:+.0f} vs €{cm_below:g} trigger")
    elif cm_below:
        parts.append(f"no Cardmarket entry yet (trigger €{cm_below:g})")
    if not parts:
        parts.append("no trigger configured")
    v.reason = "; ".join(parts)
    return v


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def export_markdown(cfg: dict, conn: Database) -> str:
    settings = cfg["settings"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Kort og Godt — prisscan {now}",
        "",
        f"FX: EUR/DKK {EUR_DKK} (pegged, hardcoded) · "
        f"USD/DKK {settings['usd_dkk']} (config)",
        "",
        "## Overblik",
        "",
        "| Produkt | Verdict | Bedste pris (landed) | Butik | vs trigger |",
        "|---|---|---|---|---|",
    ]
    details: list[str] = []
    for product in cfg["products"]:
        v = product_verdict(conn, product, settings)
        lines.append(
            f"| {product['name']} | {v.emoji} {v.label} "
            f"| {fmt_dkk(v.cheapest_dkk)} | {v.cheapest_shop or '–'} "
            f"| {fmt_dkk(v.vs_trigger_dkk, 0) if v.vs_trigger_dkk is not None else '–'} |")

        details.append(f"\n## {product['name']} — {v.emoji} {v.label}\n")
        details.append(f"- **Verdict:** {v.reason or v.label}")
        t = v.trend

        def trend_line(label: str, avg: Optional[float]) -> str:
            if not t or avg is None:
                return f"- {label}: insufficient history"
            pct = t.pct_vs(avg)          # None when there is no in-stock today
            if pct is None:
                return (f"- {label}: {fmt_dkk(avg)} "
                        "(no verified in-stock price today to compare)")
            return f"- {label}: {fmt_dkk(avg)} ({pct:+.1f}% today vs avg)"

        details.append(trend_line("7d avg", t.avg7 if t else None))
        details.append(trend_line("30d avg", t.avg30 if t else None))
        for note in v.notes:
            details.append(f"- Note: {note}")
        for f_ in v.failures:
            details.append(f"- ⚠ FAILED: {f_}")
        details.append("")
        details.append("| Kilde | Pris | Landed DKK | Lager | Set | URL |")
        details.append("|---|---|---|---|---|---|")
        for r in latest_observations(conn, product["id"],
                                     product.get("sources", [])):
            if r["status"] == "ok":
                native = (f"{r['price_native']:.2f} {r['currency']}"
                          if r["price_native"] is not None else "–")
                stock = {1: "på lager", 0: "udsolgt"}.get(r["in_stock"], "?")
                details.append(
                    f"| {r['shop']} | {native} | {fmt_dkk(r['landed_dkk'])} "
                    f"| {stock} | {r['observed_at']} | {r['url']} |")
            elif r["status"] == "skipped":
                details.append(
                    f"| {r['shop']} | skipped (robots.txt) | – | – "
                    f"| {r['observed_at']} | {r['url']} |")
            else:
                details.append(
                    f"| {r['shop']} | UNVERIFIED | – | – | {r['observed_at']} "
                    f"| {r['url']} |")
    lines += details

    # Optional collection / portfolio summary.
    collection = get_collection(conn)
    if collection.get("holdings"):
        cv = value_collection(conn, cfg, collection)
        lines += [
            "", "## Samling (collection)", "",
            f"- Valuation basis: **{cv['basis']}**",
            f"- Market value (valued holdings): {fmt_dkk(cv['total_value'], 0)}",
            f"- Cost basis (holdings with a recorded cost): "
            f"{fmt_dkk(cv['total_cost'], 0)}",
            f"- Unrealized P/L: {fmt_dkk(cv['unrealized_pl'], 0)}",
            f"- Valued {cv['n_valued']}/{cv['n_items']} held holdings "
            f"({cv['n_unverified']} UNVERIFIED)",
        ]
        if cv["n_valued_no_cost"]:
            lines.append(
                f"- Note: {cv['n_valued_no_cost']} valued holding(s) worth "
                f"{fmt_dkk(cv['value_no_cost'], 0)} have no recorded cost and "
                "are excluded from P/L")
        if cv["n_sold"]:
            lines += [
                f"- Realized P/L (from {cv['n_sold']} sold): "
                f"{fmt_dkk(cv['realized_pl'], 0)} "
                f"(proceeds {fmt_dkk(cv['realized_proceeds'], 0)})",
            ]
        lines += [
            "",
            "| Item | Qty | Unit value | Line value | Unit cost | P/L | From |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in cv["rows"]:
            lines.append(
                f"| {r.name} | {r.quantity:g} "
                f"| {fmt_dkk(r.unit_value_dkk) if r.unit_value_dkk is not None else 'UNVERIFIED'} "
                f"| {fmt_dkk(r.line_value) if r.line_value is not None else '–'} "
                f"| {fmt_dkk(r.unit_cost_dkk) if r.unit_cost_dkk is not None else '–'} "
                f"| {fmt_dkk(r.line_pl) if r.line_pl is not None else '–'} "
                f"| {r.value_source or '–'} |")

    lines += [
        "",
        "---",
        "",
        "*eBay: skipped in v1 (needs API keys; non-EU is uncompetitive after "
        "+25% Danish VAT + ~160 kr fee). Cardmarket prices are manual entries "
        "— the site is not scraped.*",
        "",
        "*Every price above is traceable to the URL and timestamp in its row. "
        "UNVERIFIED means the fetch or parse failed — the tool never "
        "interpolates or silently reuses stale data.*",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Collection / portfolio value
# ---------------------------------------------------------------------------

def current_value(conn: Database, product: dict, settings: dict,
                  basis: str = "replacement"
                  ) -> Optional[tuple[float, str, str]]:
    """Best current unit value (DKK) for a watchlist product, or None.

    'replacement' → cheapest verified in-stock landed price among the product's
                    configured shop sources (what it costs to buy again).
    'cardmarket'  → the latest fresh manual Cardmarket entry (resale reference).
    Returns (value_dkk, source_label, observed_at) or None when there is no
    verified value — the collection is never valued on a guess.
    """
    if basis == "cardmarket":
        row = _latest_cm_entry(conn, product["id"])
        if row is None:
            return None
        age = (datetime.now()
               - datetime.fromisoformat(row["observed_at"])).days
        max_age = settings.get("cardmarket_max_age_days")
        if max_age and age > max_age:
            return None                 # stale manual entry — don't value on it
        val = row["landed_dkk"] if row["landed_dkk"] else row["price_dkk"]
        if val and val > 0:
            return float(val), "cardmarket", row["observed_at"]
        return None
    # replacement
    latest = latest_observations(conn, product["id"], product.get("sources", []))
    ok = [r for r in latest
          if r["status"] == "ok" and not r["reference_only"]
          and r["source_kind"] != "manual" and r["in_stock"] == 1
          and r["landed_dkk"] is not None and r["landed_dkk"] > 0]
    if not ok:
        return None
    best = min(ok, key=_cheapest_key(settings))
    return float(best["landed_dkk"]), best["shop"], best["observed_at"]


@dataclass
class HoldingValue:
    name: str
    quantity: float
    unit_cost_dkk: Optional[float]
    unit_value_dkk: Optional[float]
    value_source: str
    observed_at: Optional[str]
    status: str                        # "valued" | "unverified"
    added_by: Optional[str] = None     # who added the holding (shared use)

    @property
    def line_value(self) -> Optional[float]:
        if self.unit_value_dkk is None:
            return None
        return self.unit_value_dkk * self.quantity

    @property
    def line_cost(self) -> Optional[float]:
        if self.unit_cost_dkk is None:
            return None
        return self.unit_cost_dkk * self.quantity

    @property
    def line_pl(self) -> Optional[float]:
        if self.line_value is None or self.line_cost is None:
            return None
        return self.line_value - self.line_cost


def value_holding(conn: Database, holding: dict,
                  product_by_id: dict, settings: dict,
                  basis: str) -> HoldingValue:
    """Value one holding. Prefers a live scanned value for a linked watchlist
    product; falls back to a manual per-unit value; else UNVERIFIED."""
    try:
        qty = float(holding.get("quantity") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    unit_cost = holding.get("unit_cost_dkk")
    unit_cost = float(unit_cost) if unit_cost not in (None, "") else None
    pid = holding.get("product_id")
    name = holding.get("name") or pid or "(unnamed)"

    value = source = seen = None
    if pid and pid in product_by_id:
        cv = current_value(conn, product_by_id[pid], settings, basis)
        if cv is not None:
            value, source, seen = cv
    if value is None:
        manual = holding.get("manual_value_dkk")
        if manual not in (None, ""):
            value, source = float(manual), "manual value"
    status = "valued" if value is not None and qty > 0 else "unverified"
    return HoldingValue(name=name, quantity=qty, unit_cost_dkk=unit_cost,
                        unit_value_dkk=value,
                        value_source=source or "", observed_at=seen,
                        status=status, added_by=holding.get("added_by") or None)


def _holding_is_sold(h: dict) -> bool:
    sp = h.get("sold_price_dkk")
    try:
        return sp not in (None, "") and float(sp) > 0
    except (TypeError, ValueError):
        return False


def _sold_year(sold_date: str) -> Optional[int]:
    """Best-effort calendar year from the free-text `sold_date` (None on junk)."""
    m = re.search(r"\b(\d{4})\b", sold_date or "")
    if not m:
        return None
    year = int(m.group(1))
    return year if 1990 <= year <= 2100 else None


def _empty_person_bucket() -> dict:
    return {"n_items": 0, "n_valued": 0, "market_value": 0.0, "cost": 0.0,
            "unrealized_pl": 0.0, "n_sold": 0, "realized_proceeds": 0.0,
            "realized_cost": 0.0, "realized_pl": 0.0}


def value_collection(conn: Database, cfg: dict, collection: dict,
                     basis: Optional[str] = None) -> dict:
    """Value the collection, split into holdings you still own (valued from
    live prices → unrealized P/L) and holdings you've sold (→ realized P/L).

    'Current' totals cover only the VALUED held holdings so market value and
    its cost basis are comparable; total_invested is the cost of all held
    holdings you own.
    """
    settings = cfg["settings"]
    if basis is None:
        basis = collection.get("settings", {}).get("valuation_basis",
                                                    "replacement")
    product_by_id = {p["id"]: p for p in cfg.get("products", [])}
    holdings = collection.get("holdings", [])
    held = [h for h in holdings if not _holding_is_sold(h)]
    sold = [h for h in holdings if _holding_is_sold(h)]

    rows = [value_holding(conn, h, product_by_id, settings, basis)
            for h in held]
    valued = [r for r in rows if r.status == "valued"]
    # Unrealized P/L and its cost basis cover ONLY valued holdings that also
    # have a recorded cost — a missing cost must not be treated as 0 (that
    # would book the item's whole market value as pure profit), exactly as the
    # realized side below. Market value still reflects everything you own that
    # is priced; any valued-but-uncosted holdings are reported separately so
    # the numbers stay honest and comparable.
    priced = [r for r in valued if r.line_cost is not None]
    total_value = sum(r.line_value for r in valued)         # worth of all valued
    priced_value = sum(r.line_value for r in priced)        # worth of the P/L set
    total_cost = sum(r.line_cost for r in priced)           # cost of the P/L set
    unrealized_pl = priced_value - total_cost               # = sum of line_pl
    total_invested = total_cost
    n_valued_no_cost = len(valued) - len(priced)
    value_no_cost = total_value - priced_value

    sold_rows = []
    for h in sold:
        try:
            qty = float(h.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        uc = h.get("unit_cost_dkk")
        unit_cost = float(uc) if uc not in (None, "") else None
        sold_price = float(h["sold_price_dkk"])
        proceeds = sold_price * qty
        realized = (proceeds - unit_cost * qty) if unit_cost is not None else None
        cost_basis = unit_cost * qty if unit_cost is not None else None
        # cost_basis truthiness also guards divide-by-zero (a 0-cost sale).
        roi_pct = (realized / cost_basis * 100.0
                   if cost_basis and realized is not None else None)
        sold_date = h.get("sold_date") or ""
        sold_rows.append({
            "name": h.get("name") or h.get("product_id") or "(unnamed)",
            "quantity": qty, "unit_cost_dkk": unit_cost,
            "sold_price_dkk": sold_price, "proceeds": proceeds,
            "realized_pl": realized, "sold_date": sold_date,
            "roi_pct": roi_pct, "year": _sold_year(sold_date),
            "added_by": h.get("added_by") or None,
        })
    realized_proceeds = sum(r["proceeds"] for r in sold_rows)
    # P/L and its cost basis only cover sold rows that have a recorded cost —
    # a missing cost can't be turned into a gain, so it never inflates P/L.
    with_cost = [r for r in sold_rows if r["unit_cost_dkk"] is not None]
    realized_cost = sum(r["unit_cost_dkk"] * r["quantity"] for r in with_cost)
    realized_pl = sum(r["realized_pl"] for r in with_cost)

    # Per-person breakdown for shared deployments: the same unrealized/realized
    # aggregates, bucketed by who added each holding. Unattributed → "(unknown)".
    by_person: dict[str, dict] = {}
    for r in rows:
        b = by_person.setdefault(r.added_by or "(unknown)", _empty_person_bucket())
        b["n_items"] += 1
        if r.status == "valued":
            b["n_valued"] += 1
            b["market_value"] += r.line_value
            if r.line_cost is not None:
                b["cost"] += r.line_cost
                b["unrealized_pl"] += r.line_pl
    for sr in sold_rows:
        b = by_person.setdefault(sr["added_by"] or "(unknown)",
                                 _empty_person_bucket())
        b["n_sold"] += 1
        b["realized_proceeds"] += sr["proceeds"]
        if sr["unit_cost_dkk"] is not None:
            b["realized_cost"] += sr["unit_cost_dkk"] * sr["quantity"]
            b["realized_pl"] += sr["realized_pl"]

    # Realized P/L rolled up by calendar year (unparseable dates → "unknown").
    realized_by_year: dict = {}
    for sr in sold_rows:
        key = sr["year"] if sr["year"] is not None else "unknown"
        yb = realized_by_year.setdefault(
            key, {"proceeds": 0.0, "cost": 0.0, "pl": 0.0, "n": 0})
        yb["n"] += 1
        yb["proceeds"] += sr["proceeds"]
        if sr["unit_cost_dkk"] is not None:
            yb["cost"] += sr["unit_cost_dkk"] * sr["quantity"]
            yb["pl"] += sr["realized_pl"]

    return {
        "rows": rows, "basis": basis,
        "total_value": total_value, "total_cost": total_cost,
        "total_invested": total_invested,
        "unrealized_pl": unrealized_pl,
        "n_items": len(rows),
        "n_valued": len(valued),
        "n_valued_no_cost": n_valued_no_cost,
        "value_no_cost": value_no_cost,
        "n_unverified": sum(1 for r in rows if r.status == "unverified"),
        "sold_rows": sold_rows,
        "n_sold": len(sold_rows),
        "realized_proceeds": realized_proceeds,
        "realized_cost": realized_cost,
        "realized_pl": realized_pl,
        "by_person": by_person,
        "realized_by_year": realized_by_year,
    }


def snapshot_collection(conn: Database, cfg: dict,
                        collection: dict) -> dict:
    """Record the current collection value as a dated snapshot (idempotent
    per calendar day: overwrites an existing same-day snapshot)."""
    v = value_collection(conn, cfg, collection)
    today = date.today().isoformat()
    conn.execute(
        "DELETE FROM collection_snapshots WHERE substr(taken_at,1,10) = :today",
        {"today": today})
    conn.execute(
        """INSERT INTO collection_snapshots
           (taken_at, basis, total_value_dkk, total_cost_dkk,
            total_invested_dkk, n_items, n_valued, n_unverified)
           VALUES (:taken_at, :basis, :total_value, :total_cost,
            :total_invested, :n_items, :n_valued, :n_unverified)""",
        {"taken_at": now_iso(), "basis": v["basis"],
         "total_value": v["total_value"], "total_cost": v["total_cost"],
         "total_invested": v["total_invested"], "n_items": v["n_items"],
         "n_valued": v["n_valued"], "n_unverified": v["n_unverified"]})
    return v


def collection_value_series(conn: Database,
                            days: int = 365) -> list[dict]:
    """One row per day (latest snapshot that day): value + cost basis."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT s.* FROM collection_snapshots s
           JOIN (SELECT substr(taken_at,1,10) AS day, MAX(id) AS mid
                 FROM collection_snapshots GROUP BY day) x ON s.id = x.mid
           WHERE substr(s.taken_at,1,10) >= :since
           ORDER BY s.taken_at""", {"since": since}).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Realized-sales CSV export
# ---------------------------------------------------------------------------

def _csv_escape(s: str) -> str:
    """Minimal RFC-4180 escaping so names/dates with commas stay one field."""
    if any(ch in s for ch in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def export_sales_csv(val: dict) -> str:
    """CSV of realized (sold) holdings from a value_collection() result.

    Pure string builder (mirrors export_markdown) — the caller feeds it to a
    Streamlit download_button. One header row plus one row per sold holding.
    """
    cols = ["name", "quantity", "unit_cost_dkk", "sold_price_dkk", "proceeds",
            "realized_pl", "roi_pct", "sold_date", "year", "added_by"]
    lines = [",".join(cols)]
    for r in val.get("sold_rows", []):
        cells = []
        for c in cols:
            v = r.get(c)
            if v is None:
                cells.append("")
            elif isinstance(v, float):
                cells.append(f"{v:.2f}")
            else:
                cells.append(_csv_escape(str(v)))
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Report-an-issue feedback store
# ---------------------------------------------------------------------------

def add_feedback(conn: Database, person: Optional[str], message: str,
                 app_version: str = "",
                 product_id: Optional[str] = None) -> None:
    """Store an in-app 'report an issue' message in the shared feedback table."""
    conn.execute(
        """INSERT INTO feedback
           (created_at, person, message, app_version, product_id, resolved)
           VALUES (:created_at, :person, :message, :app_version,
                   :product_id, 0)""",
        {"created_at": now_iso(), "person": person, "message": message,
         "app_version": app_version, "product_id": product_id})


def list_feedback(conn: Database, include_resolved: bool = True) -> list[dict]:
    """Feedback entries, newest first."""
    where = "" if include_resolved else "WHERE resolved = 0"
    return conn.execute(
        f"SELECT * FROM feedback {where} ORDER BY id DESC").fetchall()


# ---------------------------------------------------------------------------
# Discord alerts — app-triggered (no scheduler). Fired after a manual SCAN:
# products that flipped INTO the BUY set since the previous scan are posted to
# a Discord webhook. The previous BUY set lives in app_config['last_buy_set'].
# ---------------------------------------------------------------------------

def get_last_buy_set(conn: Database) -> set:
    """Product ids that were BUY at the previous scan (empty on first run)."""
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = 'last_buy_set'").fetchone()
    if not row or not row["value"]:
        return set()
    try:
        return set(json.loads(row["value"]))
    except (ValueError, TypeError):
        return set()


def put_last_buy_set(conn: Database, ids) -> None:
    _put_kv(conn, "last_buy_set", sorted(set(ids)))


def newly_buy_ids(current, previous) -> list:
    """Ids that are BUY now but weren't before — pure set diff, no DB/network."""
    return sorted(set(current) - set(previous))


def build_discord_payload(buys: list) -> dict:
    """Discord webhook JSON for newly-flipped BUY products.

    `buys`: list of {"name", "reason"(opt), "url"(opt)}. Pure — no network —
    so it is trivially unit-testable. Mirrors the app's 'Act now — BUY' text.
    """
    if not buys:
        return {"content": ""}
    lines = ["\U0001F7E2 **Kort og Godt — new BUY signal(s)**"]
    for b in buys:
        reason = b.get("reason") or ""
        line = f"• **{b['name']}**" + (f" — {reason}" if reason else "")
        if b.get("url"):
            line += f"  <{b['url']}>"
        lines.append(line)
    return {"content": "\n".join(lines)}


def post_discord(webhook_url: str, payload: dict) -> bool:
    """POST a payload to a Discord webhook. Returns True on a 2xx.

    Guarded: any failure (network, bad URL, non-2xx) is swallowed and returns
    False — a scan must never break because Discord is unreachable.
    """
    if not webhook_url or not payload.get("content"):
        return False
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=10)
        return resp.status_code < 300
    except Exception:       # noqa: BLE001 — alerts are best-effort
        return False


def notify_new_buys(conn: Database, webhook_url: Optional[str],
                    current_buys: list) -> list:
    """Diff current BUYs against the stored set, ping the new ones, persist.

    `current_buys`: list of {"id", "name", "reason"(opt), "url"(opt)}.
    Returns the newly-BUY ids. Persists the current set even when no webhook is
    configured, so enabling Discord later doesn't replay the whole existing set.
    """
    current_ids = [b["id"] for b in current_buys]
    new_ids = newly_buy_ids(current_ids, get_last_buy_set(conn))
    if webhook_url and new_ids:
        fresh = [b for b in current_buys if b["id"] in set(new_ids)]
        post_discord(webhook_url, build_discord_payload(fresh))
    put_last_buy_set(conn, current_ids)
    return new_ids


def run_headless(conn: Database, webhook: Optional[str] = None,
                 progress=None, non_dk_vantage: bool = False) -> dict:
    """One unattended scan cycle — the scheduled (GitHub Actions) entry point.

    Scan → collection snapshot → verdicts → Discord diff, exactly what the app
    does after a manual SCAN. Two deliberate differences:
      * fixtures are never recorded (CI filesystems are ephemeral), and
      * without a webhook the BUY-diff state (last_buy_set) is left UNTOUCHED,
        so a later app-side manual scan can still diff and ping — running the
        diff here with nowhere to send it would silently eat those alerts.
    """
    cfg = get_config(conn)
    cfg["settings"]["record_fixtures"] = False
    if non_dk_vantage:      # e.g. a US-based CI runner: skip geo-priced shops
        cfg["settings"]["non_dk_vantage"] = True
    scan_id, observations = run_scan(cfg, conn, progress)
    try:
        snapshot_collection(conn, cfg, get_collection(conn))
    except Exception:       # noqa: BLE001 — tracking must never break the scan
        pass
    verdicts = {p["id"]: product_verdict(conn, p, cfg["settings"])
                for p in cfg["products"]}
    buys = [{"id": p["id"], "name": p["name"],
             "reason": verdicts[p["id"]].reason, "url": verdicts[p["id"]].url}
            for p in cfg["products"] if verdicts[p["id"]].code == "BUY"]
    new_ids = notify_new_buys(conn, webhook, buys) if webhook else None
    return {
        "scan_id": scan_id,
        "n_sources": len(observations),
        "n_ok": sum(1 for o in observations if o.status == "ok"),
        "n_failed": sum(1 for o in observations
                        if o.status not in ("ok", "skipped")),
        "verdict_counts": {c: sum(1 for v in verdicts.values() if v.code == c)
                           for c in VERDICT_ORDER},
        "buys": buys,
        "new_buy_ids": new_ids,        # None = diff skipped (no webhook)
    }
