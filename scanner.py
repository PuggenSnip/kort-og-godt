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

# Bumped whenever a release changes scanner BEHAVIOR (not just adds functions).
# app.py compares this against the version it was written for and reloads a
# stale module after a Streamlit-Cloud hot-swap — a hasattr probe on one
# function proved blind to behavior changes (v1.3.13's kelz0r pre-fetch skip
# never activated in a process whose scanner predated it but had the probed
# v1.3.8 function). Keep in sync with _REQUIRED_SCANNER_API in app.py.
# v3: single-card tracking (make_single_product / alertable_products /
#     track_only excluded from price-drop alerts).
SCANNER_API_VERSION = 3

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
    # Data is considered stale after this many hours since the last completed
    # scan — the app then shows a "press SCAN" banner (a human fallback for a
    # missed scheduled run).
    "stale_hours": 8,
    # A product's cheapest landed price falling by at least this % since the
    # previous scan fires a Discord price-drop alert (even if it is not a BUY).
    "price_drop_pct": 10,
    # Shops whose bot-guard rejects the HOSTED app's datacenter IPs: skipped
    # pre-fetch when settings.hosted_vantage is set (the Streamlit Cloud scan
    # path). In DEFAULT_SETTINGS (not only the seed/migration) so the guard
    # holds on every load via _normalize_settings, independent of config
    # migration state. Harmless locally — hosted_vantage is never set there.
    "hosted_blocked_shops": {
        "kelz0r.dk": "bot-guard drops cloud-host IPs (Streamlit) — "
                     "scheduled scans cover it",
    },
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


# ---------------------------------------------------------------------------
# Single-card tracking (v1.4.0) — a single is just a track_only product
# ---------------------------------------------------------------------------

def alertable_products(products: list[dict]) -> list[dict]:
    """Products that participate in verdict panels and Discord alerts.
    track_only singles are collection assets — valued and charted, but never
    a BUY/AVOID signal and never an alert (a 3 kr card dropping 10% is not
    news the group needs pinged about)."""
    return [p for p in products if not p.get("track_only")]


def make_single_product(name: str, kelz0r_url: str = "",
                        cardmarket_url: str = "") -> dict:
    """Build a track_only watchlist product for ONE single card (pure — no DB,
    no network; the UI form and tests share it). Raises ValueError with a
    user-facing message on bad input.

    The kelz0r source (if given) gets the card's own words as the title-guard
    query — a dead slug serving kelz0r's soft-404 category page then FAILS
    instead of recording a wrong price — and shipping_dkk 0, because a single
    rides along in a bigger order: landed == shelf keeps the collection from
    being inflated by a 45 kr shipping estimate on a 3 kr card."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Card name is required.")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    if not slug:
        raise ValueError("Card name needs at least some letters or digits.")
    product: dict = {"id": f"single-{slug}", "name": name,
                     "track_only": True, "sources": [], "triggers": {}}
    k = (kelz0r_url or "").strip()
    if k:
        if not re.match(r"https?://(www\.)?kelz0r\.dk/.+-p-\d+\.html?$", k):
            raise ValueError(
                "The kelz0r link must be a PRODUCT page — it ends in "
                "…-p-123456.html (category/search pages can't be tracked).")
        query = " ".join(w for w in re.findall(r"[a-z0-9]+", name.lower())
                         if len(w) > 2)[:60]
        product["sources"].append({
            "shop": "kelz0r.dk", "method": "kelz0r_product", "url": k,
            "query": query or name.lower(), "shipping_dkk": 0})
    c = (cardmarket_url or "").strip()
    if c:
        if "cardmarket.com" not in c:
            raise ValueError("The Cardmarket link must be a cardmarket.com URL.")
        product["cardmarket"] = {"url": c}
    return product


def _put_kv(conn, key: str, obj: dict) -> None:
    value = json.dumps(obj, ensure_ascii=False)
    # Atomic UPSERT (one statement, one transaction). The old DELETE-then-INSERT
    # ran as two separate autocommitted transactions, leaving a window where the
    # row was gone — a concurrent get_config on the shared Postgres would see no
    # config and re-seed from the bundled watchlist.json, silently reverting
    # every user edit. ON CONFLICT is supported by both modern SQLite (3.24+,
    # far below Python 3.12's bundled version) and Postgres.
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES (:k, :v) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        {"k": key, "v": value})


SEED_CONFIG_VERSION = 19   # bump when watchlist.json ships sources/settings
                           # that existing DB configs should absorb
                           # (v10: notify-watch launch items;
                           #  v11: 30th-Celebration watch targets → landed MSRP;
                           #  v12: watchlist-v2 sync — Mega Brave landed target;
                           #  v13: remove all Japanese products (group preference);
                           #  v14: + Perfect Order ETB (collection tracking);
                           #  v15: 5 Aug brief — no-YGO rule, retire TMNT +
                           #       Riftbound Origins, revised buy targets;
                           #  v16: hosted_blocked_shops — kelz0r rejects the
                           #       hosted app's IPs, skip pre-fetch there;
                           #  v17: + Chaos Rising PC ETB reference-watch;
                           #  v18: 16 Aug brief — PB Box last-call 1500 +
                           #       avoid ceiling 1560;
                           #  v19: + Lonely Mountain 0248 single (user))

# Conditional trigger corrections: product_id -> {trigger key: (old, new)} or
# {trigger key: [(old1, new1), (old2, new2), ...]} — a CHAIN applied in order,
# so a DB that skipped releases still lands on the final value. _migrate_config
# bumps a key ONLY when it still holds the old value (None = key absent), so a
# user's own edit in Config → Triggers is never clobbered. This is the one
# sanctioned trigger overwrite in an otherwise user-territory merge; an entry
# already matching `new` on a migrated DB is a harmless no-op.
_CONDITIONAL_TARGET_FIXES = {
    # v11 — 30th-Celebration watch targets: shelf MSRP → landed MSRP.
    "pkmn-30th-celebration-etb-en": {"buy_below_dkk": (799, 850)},
    "pkmn-30th-celebration-bundle-en": {"buy_below_dkk": (499, 550)},
    "pkmn-30th-celebration-upc-en": {"buy_below_dkk": (2199, 2250)},
    # v15 — 5 Aug brief: Hobbit Play post-release dip target 1100→1200;
    # FF Collector €700-grail → "≤€850 = consider" (landed).
    "mtg-hobbit-play-booster-box": {"buy_below_dkk": (1100, 1200)},
    "mtg-final-fantasy-collector-booster-box": {"buy_below_dkk": (5220, 6390)},
    # v15 opened the Pitch Black buy window (1450→1600, CM €185→190);
    # v18 — 16 Aug brief: reversal confirmed, LAST CALL — tighten the DK
    # trigger to 1500 and add the €200+ship do-not-chase ceiling (AVOID at
    # ≥1560), so Danish retail at 1599 reads AVOID, not BUY.
    "pitch-black-booster-box-en": {"buy_below_dkk": [(1450, 1600),
                                                     (1600, 1500)],
                                   "cardmarket_buy_below_eur": (185, 190),
                                   "avoid_above_dkk": (None, 1560)},
}

# Products removed from the list (the REMOVE side of the otherwise add-only
# merge). Only ids listed here are removed; a user's own products are
# untouched. A collection holding linked to a removed product keeps its row +
# link and simply loses live valuation until/unless the product ever returns.
_REMOVED_PRODUCT_IDS = frozenset({
    # v13 — group rule: no Japanese-language products.
    "pokemon-151-jp-booster-box",
    "mega-brave-jp-booster-box",
    "abyss-eye-jp-booster-box",
    "nihil-zero-jp-booster-box",
    "pkmn-30th-celebration-jp-m6a-box",
    "mega-symphonia-jp-booster-box",
    "mega-dream-ex-jp-booster-box",
    "inferno-x-jp-booster-box",
    "terastal-festival-ex-jp-booster-box",
    "black-bolt-jp-booster-box",
    "white-flare-jp-booster-box",
    # v15 — group rule: no Yu-Gi-Oh (investment-only ruleset, 5 Aug brief).
    "ygo-rarity-collection-5-ra05",
    # v15 — retired triggers (5 Aug brief): TMNT floor firm at €259,
    # Riftbound Origins 1st print sold out.
    "mtg-tmnt-collector-booster-box",
    "riftbound-origins-display-en",
})


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
    for map_key in ("shop_shipping_dkk", "preferred_shops",
                    "hosted_blocked_shops"):
        seed_map = seed.get("settings", {}).get(map_key, {})
        mine = cfg.setdefault("settings", {}).setdefault(map_key, {})
        for shop, val in seed_map.items():
            if shop not in mine:
                mine[shop] = val
    # Conditional trigger corrections (buy targets the tool itself seeded at a
    # shelf figure while the verdict compares LANDED): bump each to its landed
    # target ONLY where the stored value is still the old figure, so a user's
    # own edit is never overwritten. See _CONDITIONAL_TARGET_FIXES.
    for p in cfg.get("products", []):
        fixes = _CONDITIONAL_TARGET_FIXES.get(p.get("id"))
        if fixes:
            t = p.setdefault("triggers", {})
            for key, pairs in fixes.items():
                if not isinstance(pairs, list):
                    pairs = [pairs]
                for old, new in pairs:      # chain: applied in release order
                    if t.get(key) == old:
                        t[key] = new
    # Drop retired products (only the ids in _REMOVED_PRODUCT_IDS; user
    # products are untouched). Collection holdings linked to a removed product
    # keep their row + link; they just lose live valuation while it's absent.
    if _REMOVED_PRODUCT_IDS:
        cfg["products"] = [p for p in cfg.get("products", [])
                           if p.get("id") not in _REMOVED_PRODUCT_IDS]
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

    # robots.txt gets its own, much longer cache TTL (RFC 9309 allows up to
    # 24 h). This matters on the shared deployment: the GitHub cron refreshes
    # robots.txt ~8x/day from IPs the shops accept, while kelz0r rejects
    # Streamlit Cloud's IPs — with the 1 h page TTL an in-app scan >1 h after
    # a cron re-fetched robots from the blocked IP, hit the fail-closed
    # "unreachable" path, and skipped every kelz0r source (seen live, scan
    # #50). A 24 h robots TTL keeps the cron-warmed verdict valid all day.
    ROBOTS_CACHE_TTL_SECONDS = 24 * 3600

    def _cache_get(self, url: str) -> Optional[FetchResult]:
        ttl = (self.ROBOTS_CACHE_TTL_SECONDS
               if url.endswith("/robots.txt")
               else self.settings["cache_ttl_seconds"])
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
        # Atomic UPSERT (portable across modern SQLite + Postgres) — no
        # delete-then-insert gap where a concurrent reader sees no cache row.
        self.conn.execute(
            "INSERT INTO http_cache (url, fetched_at, status, body) "
            "VALUES (:url, :fetched_at, :status, :body) "
            "ON CONFLICT (url) DO UPDATE SET "
            "fetched_at = excluded.fetched_at, status = excluded.status, "
            "body = excluded.body",
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
        # WHOLE-WORD matching (not substring): so an exclude of "pack" can't be
        # dodged by "backpack", and "box" doesn't match "boxset".
        title_words = re.findall(r"\w+", title)
        tset = set(title_words)
        if not all(w in tset for w in query_words):
            continue
        if any(w in tset for w in exclude_words):
            continue
        # COVERAGE score: query words as a fraction of the title's words. The
        # tightest match (fewest extra words) wins — replacing the old
        # 1/len(title) which just picked the shortest matching title — and a
        # query buried in a long, mostly-unrelated title scores too low to win.
        score = len(query_words) / max(len(title_words), 1)
        if score > best_score:
            best, best_score = p, score
    if best is None:
        return ParsedPrice(error=f"no product matching {query_words!r}")
    if handle is None and best_score < 0.2:
        return ParsedPrice(
            title=best.get("title", ""),
            error=(f"weak match for {query_words!r} "
                   f"(coverage {best_score:.0%}) — refusing"))

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
    # OUT signals are checked FIRST: DanDomain's out-of-stock phrasing is
    # "Forventet på lager d. DD-MM" + a "Giv besked når varen er på lager"
    # notify button (no literal "udsolgt"), and both contain the substring
    # "på lager" — so they must be caught before the in-stock check. Bare "køb"
    # is page chrome (menu/footer) and is NOT a stock signal, so it is dropped;
    # a real in-stock page shows "Læg i kurv"/"add to cart" or a plain "På lager".
    if re.search(r"(?i)(udsolgt|ikke på lager|sold out|forventet på lager|"
                 r"giv besked|notify me)", text):
        in_stock = False
    elif re.search(r"(?i)(på lager|læg i kurv|add to cart)", text):
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
    record: bool = True                 # False = don't persist (a vantage skip
                                        # that must not shadow fresher good rows)


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


def _as_float(x) -> Optional[float]:
    """float(x) but None on anything non-numeric — so a bad config value typed
    into the raw-JSON editor can never raise out of a scan."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def resolve_shipping_dkk(source: dict, settings: dict) -> float:
    """Effective shipping for a source: its own shipping_dkk wins, else the
    per-shop estimate in settings.shop_shipping_dkk, else 0 (unknown — the UI
    marks such landed prices with * so 'landed' never quietly means 'shelf').

    A non-numeric value (e.g. someone typing "free" in the config editor) is
    treated as unknown (0), never raised — scan_source must never crash."""
    own = source.get("shipping_dkk")
    if own not in (None, ""):
        val = _as_float(own)
        if val is not None:
            return val
    per_shop = settings.get("shop_shipping_dkk") or {}
    return _as_float(per_shop.get(source.get("shop"))) or 0.0


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

    # Shops whose bot-guard rejects the hosted app's datacenter IPs (kelz0r
    # drops Streamlit Cloud connections: robots.txt AND product pages time
    # out). Fetching would burn 15 s per source and record failures that wedge
    # the products UNVERIFIED — so the hosted vantage politely skips them
    # up-front. record=False: unlike the geo skip above (where NO vantage
    # scans the shop regularly), these shops ARE covered hours-fresh by the
    # scheduled GitHub scans, and a persisted skip row would shadow that good
    # data out of the latest-per-source verdict view for no gain.
    if (settings.get("hosted_vantage")
            and source["shop"] in (settings.get("hosted_blocked_shops") or {})):
        obs.status = "skipped"
        obs.error = ("shop blocks this host's IPs — covered by the "
                     "scheduled scans")
        obs.record = False
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
        # _as_float so a non-numeric band typed into the config editor disables
        # that bound instead of raising out of the scan.
        lo = _as_float(triggers.get("plausible_min_dkk"))
        hi = _as_float(triggers.get("plausible_max_dkk"))
        if (lo is not None and obs.landed_dkk < lo) or \
           (hi is not None and obs.landed_dkk > hi):
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
                                added_by: Optional[str] = None,
                                observed_at: Optional[str] = None) -> Observation:
    """Store a manually entered Cardmarket price like scraped data.

    `added_by` (a person's name) is recorded for shared deployments so the
    collection can show who entered each price; None for single-user runs.
    `observed_at` backdates the entry (ISO date/datetime) so history can be
    filled in; None stamps now. A backdated entry never hijacks the current
    verdict — _latest_cm_entry orders by observed_at, so a newer-dated price
    still wins (and one older than cardmarket_max_age_days is treated as stale).
    """
    cm = product.get("cardmarket", {})
    shipping = float(cm.get("shipping_dkk", 0))
    obs = Observation(
        product_id=product["id"], shop="cardmarket", url=cm.get("url", ""),
        method="cardmarket_manual", observed_at=observed_at or now_iso(),
        title=product["name"], price_native=price_eur, currency="EUR",
        price_dkk=price_eur * EUR_DKK, landed_dkk=price_eur * EUR_DKK + shipping,
        in_stock=True, status="ok", source_kind="manual", added_by=added_by)
    insert_observation(conn, obs, scan_id=None)
    return obs


def list_cardmarket_entries(conn: Database, product_id: str,
                            limit: int = 20) -> list[dict]:
    """Recent manual Cardmarket entries (newest-dated first) for display and
    management: id, observed_at, price_native (EUR), added_by."""
    rows = conn.execute(
        """SELECT id, observed_at, price_native, added_by FROM observations
           WHERE product_id = :pid AND source_kind = 'manual'
             AND price_native IS NOT NULL
           ORDER BY observed_at DESC, id DESC LIMIT :lim""",
        {"pid": product_id, "lim": limit}).fetchall()
    return [dict(r) for r in rows]


def cardmarket_entries_all(conn: Database,
                           limit: int = 20) -> dict[str, list[dict]]:
    """list_cardmarket_entries for EVERY product in one query — {pid: entries}.
    The per-product LIMIT is applied in Python (manual entries are a tiny,
    hand-typed table), preserving the exact per-product ordering and cap."""
    rows = conn.execute(
        """SELECT product_id, id, observed_at, price_native, added_by
           FROM observations
           WHERE source_kind = 'manual' AND price_native IS NOT NULL
           ORDER BY product_id, observed_at DESC, id DESC""").fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        lst = out.setdefault(r["product_id"], [])
        if len(lst) < limit:
            d = dict(r)
            d.pop("product_id")
            lst.append(d)
    return out


def delete_cardmarket_entry(conn: Database, obs_id: int) -> bool:
    """Delete one manual Cardmarket entry by id. Scoped to source_kind='manual'
    so a scraped observation can never be removed through this path. Returns
    True when a row was actually deleted."""
    row = conn.execute(
        "SELECT id FROM observations "
        "WHERE id = :i AND source_kind = 'manual'", {"i": obs_id}).fetchone()
    if not row:
        return False
    conn.execute(
        "DELETE FROM observations WHERE id = :i AND source_kind = 'manual'",
        {"i": obs_id})
    return True


def cardmarket_stats(conn: Database, product: dict,
                     settings: dict,
                     prefetched_series: Optional[dict] = None,
                     prefetched_cm: Optional[dict] = None) -> Optional[dict]:
    """Summary of a product's manual Cardmarket entries for the UI: count,
    latest price (EUR + landed DKK + date + age + staleness), min/max EUR over
    the last year, and whether the latest fresh entry meets the buy threshold.
    None when there are no entries.

    prefetched_series / prefetched_cm: cardmarket_series_all /
    latest_cm_entries_all dicts — absence of a pid means "no entries"."""
    pid = product["id"]
    series = (prefetched_series.get(pid, []) if prefetched_series is not None
              else cardmarket_series(conn, pid))   # (ts, EUR), oldest→newest
    if not series:
        return None
    latest = (prefetched_cm.get(pid) if prefetched_cm is not None
              else _latest_cm_entry(conn, pid))    # by observed_at DESC
    if latest is None:
        return None
    eur_vals = [p for _, p in series]
    latest_eur = latest["price_native"]
    shipping = float((product.get("cardmarket") or {}).get("shipping_dkk", 0))
    try:
        age_days = (datetime.now()
                    - datetime.fromisoformat(latest["observed_at"])).days
    except (TypeError, ValueError):
        age_days = None
    max_age = settings.get("cardmarket_max_age_days")
    stale = bool(max_age and age_days is not None and age_days > max_age)
    cm_below = product.get("triggers", {}).get("cardmarket_buy_below_eur")
    meets = (cm_below is not None and latest_eur <= cm_below and not stale)
    return {
        "n": len(series),
        "latest_eur": latest_eur,
        "latest_dkk": latest_eur * EUR_DKK + shipping,
        "latest_date": latest["observed_at"],
        "age_days": age_days,
        "stale": stale,
        "min_eur": min(eur_vals),
        "max_eur": max(eur_vals),
        "threshold_eur": cm_below,
        "meets_threshold": meets,
    }


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
                if obs.status != "manual" and obs.record:
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
                        sources: Optional[list] = None,
                        prefetched: Optional[dict] = None) -> list[dict]:
    """Latest observation per shop+method for a product (any status).

    If `sources` (the product's current config sources) is given, rows are
    restricted to the (shop, method) pairs still configured — so a source the
    user removed or renamed in the Config tab can never wedge the product in a
    stale BUY or a permanent UNVERIFIED from its last historical row.

    `prefetched` is an optional {product_id: [rows]} map from
    latest_observations_all() — pass it to serve every product's latest rows
    from ONE query instead of a query per product (a big win over a remote DB,
    where the app rebuilds these for ~30 products on every rerun).
    """
    if prefetched is not None:
        rows = prefetched.get(product_id, [])
    else:
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


def latest_observations_all(conn: Database) -> dict:
    """Latest observation per (product, shop, method) for EVERY product, in one
    query — {product_id: [rows]}. Feed to latest_observations(prefetched=...) /
    product_verdict(prefetched=...) / source_health(prefetched=...) so a whole
    page render costs one query here instead of one per product."""
    rows = conn.execute(
        """SELECT o.* FROM observations o
           JOIN (SELECT product_id, shop, method, MAX(id) AS mid
                 FROM observations GROUP BY product_id, shop, method) x
             ON o.id = x.mid ORDER BY o.product_id, o.shop""").fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["product_id"], []).append(r)
    return out


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


def daily_cheapest_series_all(conn: Database,
                              days: int = 60) -> dict[str, list[tuple[str, float]]]:
    """daily_cheapest_series for EVERY product in one query — {pid: series}.
    Products with no rows are simply absent (callers .get(pid, [])). Feed to
    product_verdict(prefetched_series=...) and the chart loop so a cold page
    render costs one query here instead of one per product."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT product_id, substr(observed_at, 1, 10) AS day,
                  MIN(landed_dkk) AS price
           FROM observations
           WHERE status = 'ok' AND in_stock = 1
             AND reference_only = 0 AND source_kind != 'manual'
             AND landed_dkk > 0 AND observed_at >= :since
           GROUP BY product_id, day ORDER BY product_id, day""",
        {"since": since}).fetchall()
    out: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        out.setdefault(r["product_id"], []).append((r["day"], r["price"]))
    return out


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


def cardmarket_series_all(conn: Database,
                          days: int = 365) -> dict[str, list[tuple[str, float]]]:
    """cardmarket_series for EVERY product in one query — {pid: [(ts, EUR)]}."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT product_id, observed_at, price_native FROM observations
           WHERE source_kind = 'manual' AND status = 'ok'
             AND price_native IS NOT NULL AND observed_at >= :since
           ORDER BY product_id, observed_at""",
        {"since": since}).fetchall()
    out: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        out.setdefault(r["product_id"], []).append(
            (r["observed_at"], r["price_native"]))
    return out


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


def compute_trend(conn: Database, product_id: str, settings: dict,
                  series: Optional[list[tuple[str, float]]] = None) -> Trend:
    """`series`: a prefetched daily_cheapest_series covering AT LEAST 30 days
    (e.g. the 60-day one from daily_cheapest_series_all) — the 7/30-day windows
    below already filter by date, so a longer series gives identical results.
    None (the default) queries per-product as before."""
    if series is None:
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

VERDICT_ORDER = ["BUY", "WATCH", "WAIT_FALLING", "AVOID", "HOLD", "UNVERIFIED"]

VERDICT_STYLE = {
    "BUY": ("🟢", "BUY"),
    "WATCH": ("⏳", "WATCH"),
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
    # Order by the PRICE date (observed_at), not insertion id: a backdated
    # entry (a historical price entered to fill the chart) must never override
    # a newer-dated price in the verdict. id breaks ties (two same-date entries
    # → the later-entered correction wins). Same result as id-order for the
    # legacy today-only data, where observed_at and id increase together.
    return conn.execute(
        """SELECT * FROM observations
           WHERE product_id = :pid AND source_kind = 'manual' AND status = 'ok'
           ORDER BY observed_at DESC, id DESC LIMIT 1""",
        {"pid": product_id}).fetchone()


def latest_cm_entries_all(conn: Database) -> dict[str, dict]:
    """_latest_cm_entry for EVERY product in one query — {pid: full row}.
    Same newest-DATED-wins ordering (observed_at DESC, id DESC per product);
    products with no manual entry are absent from the dict."""
    rows = conn.execute(
        """SELECT * FROM observations
           WHERE source_kind = 'manual' AND status = 'ok'
           ORDER BY product_id, observed_at DESC, id DESC""").fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if r["product_id"] not in out:      # first row per pid = the latest
            out[r["product_id"]] = r
    return out


def product_verdict(conn: Database, product: dict,
                    settings: dict, prefetched: Optional[dict] = None,
                    prefetched_series: Optional[dict] = None,
                    prefetched_cm: Optional[dict] = None) -> Verdict:
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

    A product marked ``"watch": true`` is a parked, monitored item (a listed-
    but-usually-unpriced launch preorder, or a grail far above target). For it
    the ONLY actionable verdict is BUY (rule 0 or rule 2 — target reached);
    every other outcome — no price yet, priced above target, falling, would-be
    AVOID — collapses to WATCH so watches stay out of the act-now view and a
    preorder with no price yet never masquerades as UNVERIFIED. Failed sources
    are still recorded in v.failures as context, never hidden.
    """
    triggers = product.get("triggers", {})
    is_watch = bool(product.get("watch"))
    latest = latest_observations(conn, product["id"], product.get("sources", []),
                                 prefetched=prefetched)
    # prefetched_series / prefetched_cm (from daily_cheapest_series_all /
    # latest_cm_entries_all) skip the two per-product queries below. A product
    # ABSENT from a prefetched dict means "no data" (empty series / no entry),
    # never "go query" — the dicts are built from whole-table queries.
    trend = compute_trend(conn, product["id"], settings,
                          series=(prefetched_series.get(product["id"], [])
                                  if prefetched_series is not None else None))
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

    cm_row = (prefetched_cm.get(product["id"]) if prefetched_cm is not None
              else _latest_cm_entry(conn, product["id"]))
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

    # Rule 1 — data hygiene beats everything data-driven. Skipped for watches:
    # a watch is speculative and wants the earliest possible target signal, so
    # a flaky co-source must not withhold a BUY nor mask a preorder as broken —
    # the failure list still carries the detail (see the WATCH-tail below).
    if failures and not is_watch:
        v.code = "UNVERIFIED"
        v.reason = (f"{len(failures)} of {len(checkable)} checkable sources "
                    "failed — verdict withheld, see failure list")
        return v
    if not ok_rows and cm_row is None and not is_watch:
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

    # WATCH-tail — a watch that isn't a BUY is parked, not HOLD/AVOID/UNVERIFIED.
    # The reason carries the state: waiting for a price, distance above target,
    # or a falling hint. Failed sources remain in v.failures for context.
    if is_watch:
        v.code = "WATCH"
        if v.cheapest_dkk is None:
            v.reason = "listed but no in-stock price yet — waiting for it to price"
        elif buy_below:
            over = v.cheapest_dkk - buy_below
            v.reason = (f"{fmt_dkk(v.cheapest_dkk)} — {fmt_dkk(over, 0)} above "
                        f"target {fmt_dkk(buy_below, 0)}")
        else:
            v.reason = f"listed at {fmt_dkk(v.cheapest_dkk)} (no target set)"
        if (trend.avg7 is not None and trend.today is not None
                and trend.today < trend.avg7):
            v.reason += f"; falling {trend.pct_vs(trend.avg7):+.1f}% vs 7d avg"
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


# Game classification for portfolio grouping. Keyword → canonical game name,
# checked in order; the shop naming in this watchlist is consistent enough that
# a name prefix is reliable, and a product/holding can override with an explicit
# "game" field.
_GAME_KEYWORDS = (
    ("Pokémon", ("pokémon", "pokemon")),
    ("Magic", ("mtg", "magic")),
    ("Riftbound", ("riftbound",)),
    ("One Piece", ("one piece",)),
    ("Lorcana", ("lorcana",)),
    ("Yu-Gi-Oh!", ("yu-gi-oh", "yugioh")),
    ("Gundam", ("gundam",)),
)


def infer_game(name: str) -> str:
    """Best-effort game from a product/holding name; 'Other' when unrecognised."""
    n = (name or "").lower()
    for game, kws in _GAME_KEYWORDS:
        if any(k in n for k in kws):
            return game
    return "Other"


def product_game(product) -> str:
    """The game a product (dict) or a plain name (str) belongs to, for portfolio
    grouping. An explicit "game" field on a product wins; else inferred by name."""
    if isinstance(product, dict):
        explicit = product.get("game")
        if explicit:
            return str(explicit)
        return infer_game(product.get("name") or product.get("id") or "")
    return infer_game(str(product or ""))


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
    game: str = "Other"                # for per-game portfolio grouping
    set_key: str = ""                  # product name (linked) or holding name

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
    # Grouping keys: a linked holding takes its game + set label from the
    # watchlist product; an unlinked one infers the game from its own name.
    linked = product_by_id.get(pid) if pid else None
    game = product_game(linked) if linked else product_game(name)
    set_key = (linked.get("name") or pid) if linked else name

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
                        status=status, added_by=holding.get("added_by") or None,
                        game=game, set_key=set_key)


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


def _empty_pl_bucket() -> dict:
    """A zeroed unrealized+realized aggregate — reused for the by-person,
    by-game and by-set breakdowns (same shape, different grouping key)."""
    return {"n_items": 0, "units": 0.0, "n_valued": 0, "market_value": 0.0,
            "cost": 0.0, "unrealized_pl": 0.0, "n_sold": 0, "sold_units": 0.0,
            "realized_proceeds": 0.0, "realized_cost": 0.0, "realized_pl": 0.0}


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
        s_pid = h.get("product_id")
        s_linked = product_by_id.get(s_pid) if s_pid else None
        s_name = h.get("name") or s_pid or "(unnamed)"
        sold_rows.append({
            "name": s_name,
            "quantity": qty, "unit_cost_dkk": unit_cost,
            "sold_price_dkk": sold_price, "proceeds": proceeds,
            "realized_pl": realized, "sold_date": sold_date,
            "roi_pct": roi_pct, "year": _sold_year(sold_date),
            "added_by": h.get("added_by") or None,
            "game": product_game(s_linked) if s_linked else product_game(s_name),
            "set": (s_linked.get("name") or s_pid) if s_linked else s_name,
        })
    realized_proceeds = sum(r["proceeds"] for r in sold_rows)
    # P/L and its cost basis only cover sold rows that have a recorded cost —
    # a missing cost can't be turned into a gain, so it never inflates P/L.
    with_cost = [r for r in sold_rows if r["unit_cost_dkk"] is not None]
    realized_cost = sum(r["unit_cost_dkk"] * r["quantity"] for r in with_cost)
    realized_pl = sum(r["realized_pl"] for r in with_cost)

    # Breakdowns: the same unrealized/realized aggregates bucketed three ways —
    # by who added each holding (shared use), by game, and by set (product).
    # One pass over held rows and one over sold rows fills all three.
    by_person: dict[str, dict] = {}
    by_game: dict[str, dict] = {}
    by_set: dict[str, dict] = {}

    def _add_held(bucket_map: dict, key: str, r: HoldingValue) -> None:
        b = bucket_map.setdefault(key, _empty_pl_bucket())
        b["n_items"] += 1              # distinct holdings (rows)
        b["units"] += r.quantity       # physical items owned
        if r.status == "valued":
            b["n_valued"] += 1
            b["market_value"] += r.line_value
            if r.line_cost is not None:
                b["cost"] += r.line_cost
                b["unrealized_pl"] += r.line_pl

    def _add_sold(bucket_map: dict, key: str, sr: dict) -> None:
        b = bucket_map.setdefault(key, _empty_pl_bucket())
        b["n_sold"] += 1               # distinct sold holdings (rows)
        b["sold_units"] += sr["quantity"]
        b["realized_proceeds"] += sr["proceeds"]
        if sr["unit_cost_dkk"] is not None:
            b["realized_cost"] += sr["unit_cost_dkk"] * sr["quantity"]
            b["realized_pl"] += sr["realized_pl"]

    for r in rows:
        _add_held(by_person, r.added_by or "(unknown)", r)
        _add_held(by_game, r.game or "Other", r)
        _add_held(by_set, r.set_key or r.name, r)
    for sr in sold_rows:
        _add_sold(by_person, sr["added_by"] or "(unknown)", sr)
        _add_sold(by_game, sr["game"] or "Other", sr)
        _add_sold(by_set, sr["set"] or sr["name"], sr)

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
        "by_game": by_game,
        "by_set": by_set,
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


def build_discord_payload(buys: list, drops: Optional[list] = None,
                          watch_live: Optional[list] = None) -> dict:
    """Discord webhook JSON for newly-flipped BUYs, price drops and watches
    that just went live.

    `buys`:  list of {"name", "price"(opt), "shop"(opt), "trigger"(opt),
                      "reason"(opt), "url"(opt)}.
    `drops`: list of {"name", "price", "prev", "pct", "shop"(opt), "url"(opt)}.
    `watch_live`: list of {"name", "price"(opt), "shop"(opt), "target"(opt),
                      "url"(opt)} — a watched preorder that just got its first
                      price (above target; ≤-target ones ride the BUY section).
    Pure — no network — so it is trivially unit-testable.
    """
    lines: list[str] = []
    if buys:
        lines.append("\U0001F7E2 **Kort og Godt — new BUY signal(s)**")
        for b in buys:
            line = f"• **{b['name']}**"
            if b.get("price") is not None:
                line += f" — {fmt_dkk(b['price'], 0)}"
                if b.get("shop"):
                    line += f" @ {b['shop']}"
                if b.get("trigger") is not None:
                    line += f" (≤ {fmt_dkk(b['trigger'], 0)})"
            elif b.get("reason"):
                line += f" — {b['reason']}"
            if b.get("url"):
                line += f"  <{b['url']}>"
            lines.append(line)
    if drops:
        if lines:
            lines.append("")
        lines.append("\U0001F4C9 **Price drops**")
        for d in drops:
            line = (f"• **{d['name']}** — {fmt_dkk(d['price'], 0)} "
                    f"({d.get('pct', 0):+.0f}% vs {fmt_dkk(d.get('prev'), 0)})")
            if d.get("shop"):
                line += f" @ {d['shop']}"
            if d.get("url"):
                line += f"  <{d['url']}>"
            lines.append(line)
    if watch_live:
        if lines:
            lines.append("")
        lines.append("\U0001F195 **Watches now priced** (preorder went live)")
        for w in watch_live:
            line = f"• **{w['name']}**"
            if w.get("price") is not None:
                line += f" — {fmt_dkk(w['price'], 0)}"
                if w.get("shop"):
                    line += f" @ {w['shop']}"
                if w.get("target") is not None:
                    line += f" (target ≤ {fmt_dkk(w['target'], 0)})"
            if w.get("url"):
                line += f"  <{w['url']}>"
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
    """Diff current BUYs against the stored set and ping the new ones.

    `current_buys`: list of {"id", "name", "reason"(opt), "url"(opt)}. Returns
    the ids actually DELIVERED this scan.

    The tracked set advances ONLY when this scan's alert was delivered — either
    nothing was new, or a configured webhook accepted the post. With no webhook
    here (e.g. a friend's local app instance sharing the DB with the cron) or on
    a Discord error, the state is left untouched so the alert is retried — by
    this instance next scan, or by whichever instance holds the webhook (the
    cron). This stops a webhook-less instance from silently eating the cron's
    pings and a transient Discord outage from dropping an alert forever.
    (Trade-off: first enabling a webhook on a DB whose state was never advanced
    replays the current set once.)
    """
    current_ids = [b["id"] for b in current_buys]
    new_ids = newly_buy_ids(current_ids, get_last_buy_set(conn))
    delivered = True
    if new_ids:
        fresh = [b for b in current_buys if b["id"] in set(new_ids)]
        delivered = bool(webhook_url) and post_discord(
            webhook_url, build_discord_payload(fresh))
    if delivered:
        put_last_buy_set(conn, current_ids)
    return new_ids if delivered else []


def get_last_cheapest(conn: Database) -> dict:
    """Map product_id -> cheapest landed price at the previous scan."""
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = 'last_cheapest'").fetchone()
    if not row or not row["value"]:
        return {}
    try:
        d = json.loads(row["value"])
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def put_last_cheapest(conn: Database, mapping: dict) -> None:
    _put_kv(conn, "last_cheapest", mapping)


def price_drops(current: dict, previous: dict, pct_threshold: float) -> list:
    """Pure diff: product ids whose cheapest landed fell by ≥ pct_threshold %
    vs the previous scan. current/previous are {id: price}. No DB/network."""
    out = []
    for pid, price in current.items():
        prev = previous.get(pid)
        if not prev or prev <= 0 or not price or price <= 0:
            continue
        pct = (price - prev) / prev * 100.0
        if pct <= -abs(pct_threshold):
            out.append({"id": pid, "price": price, "prev": prev, "pct": pct})
    return out


def notify_price_drops(conn: Database, webhook_url: Optional[str],
                       current_items: list, pct_threshold: float) -> list:
    """Diff cheapest landed vs the previous scan, ping big drops, persist.

    `current_items`: list of {"id", "name", "price"(cheapest landed),
    "shop"(opt), "url"(opt)}. Twin of notify_new_buys — advances the stored
    cheapest map only on delivery (see notify_new_buys), so a webhook-less
    instance / a Discord outage never silently drops a price-drop alert.
    """
    current_map = {c["id"]: c["price"] for c in current_items
                   if c.get("price") is not None}
    drops = price_drops(current_map, get_last_cheapest(conn), pct_threshold)
    by_id = {c["id"]: c for c in current_items}
    enriched = [{**d, "name": by_id.get(d["id"], {}).get("name", d["id"]),
                 "shop": by_id.get(d["id"], {}).get("shop"),
                 "url": by_id.get(d["id"], {}).get("url")} for d in drops]
    delivered = True
    if enriched:
        delivered = bool(webhook_url) and post_discord(
            webhook_url, build_discord_payload([], enriched))
    if delivered:
        put_last_cheapest(conn, current_map)
    return enriched if delivered else []


def get_last_watch_priced(conn: Database) -> set:
    """Watch product ids that had a verified in-stock price at the last scan."""
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = 'last_watch_priced'").fetchone()
    if not row or not row["value"]:
        return set()
    try:
        return set(json.loads(row["value"]))
    except (ValueError, TypeError):
        return set()


def put_last_watch_priced(conn: Database, ids) -> None:
    _put_kv(conn, "last_watch_priced", sorted(set(ids)))


def newly_priced_watch_ids(current, previous, exclude=()) -> list:
    """Watch ids priced now but not at the previous scan, minus `exclude`
    (the ids already reported as new BUYs, so a below-target debut isn't
    double-pinged). Pure set diff — no DB/network."""
    return sorted((set(current) - set(previous)) - set(exclude))


def notify_watch_live(conn: Database, webhook_url: Optional[str],
                      watch_items: list, exclude_ids=()) -> list:
    """Ping watches that just got their first verified in-stock price.

    `watch_items`: list of {"id", "name", "price"(opt), "shop"(opt),
    "url"(opt), "target"(opt)} — the watches that ARE priced this scan. The
    "went live" event is the transition from unpriced to priced; a debut at or
    below target is already a new BUY (passed in `exclude_ids`) and rides that
    section instead. Twin of notify_new_buys: advances the stored priced set
    only on delivery, so a webhook-less instance / a Discord outage never
    silently drops a watch-live alert.
    """
    current_ids = [w["id"] for w in watch_items]
    new_ids = newly_priced_watch_ids(current_ids, get_last_watch_priced(conn),
                                     exclude_ids)
    delivered = True
    if new_ids:
        fresh = [w for w in watch_items if w["id"] in set(new_ids)]
        delivered = bool(webhook_url) and post_discord(
            webhook_url, build_discord_payload([], None, fresh))
    if delivered:
        put_last_watch_priced(conn, current_ids)
    return new_ids if delivered else []


def drift_pairs_all(conn: Database) -> dict[tuple, list[float]]:
    """The last TWO good landed prices per (product, shop, method) in one
    query — {(pid, shop, method): [current, previous]} (one-element list when
    only one good observation exists). Feeds source_health's drift flag; the
    per-source variant is the same query with a WHERE on the triple.
    ROW_NUMBER() works on both backends (SQLite ≥3.25 bundled with Py3.12, PG)."""
    rows = conn.execute(
        """SELECT product_id, shop, method, landed_dkk FROM (
               SELECT product_id, shop, method, landed_dkk,
                      ROW_NUMBER() OVER (
                          PARTITION BY product_id, shop, method
                          ORDER BY id DESC) AS rn
               FROM observations
               WHERE status = 'ok' AND landed_dkk > 0
                 AND method IS NOT NULL) t
           WHERE rn <= 2
           ORDER BY product_id, shop, method, rn""").fetchall()
    out: dict[tuple, list[float]] = {}
    for r in rows:
        out.setdefault((r["product_id"], r["shop"], r["method"]),
                       []).append(r["landed_dkk"])
    return out


def source_health(conn: Database, cfg: dict,
                  prefetched: Optional[dict] = None,
                  prefetched_drift: Optional[dict] = None) -> list:
    """One row per configured (product, shop, method) source with its latest
    observation — for the Config-tab health dashboard. Each row carries a
    ``rank`` (lower = worse, for worst-first sorting) and a drift ``flag`` set
    when the latest good landed price swung wildly (> 2× / < 0.5×) vs the
    previous good one (a plausible-but-suspicious move a layout change can cause).

    `prefetched` (from latest_observations_all) serves the latest rows from one
    query instead of one per product.
    """
    settings = cfg.get("settings", {})
    stale_h = settings.get("stale_hours", 8)
    now = datetime.now()
    rows = []
    for p in cfg.get("products", []):
        sources = p.get("sources", [])
        is_watch = bool(p.get("watch"))
        latest = {(r["shop"], r["method"]): r
                  for r in latest_observations(conn, p["id"], sources,
                                               prefetched=prefetched)}
        for s in sources:
            if s.get("method") == "cardmarket_manual":
                continue
            obs = latest.get((s.get("shop"), s.get("method")))
            status, price, age_h, err, flag = "no data", None, None, "", ""
            if obs is None:
                err = "never scanned"
            else:
                status = obs["status"]
                price = obs["landed_dkk"]
                err = obs["error"] or ""
                try:
                    age_h = (now - datetime.fromisoformat(
                        obs["observed_at"])).total_seconds() / 3600.0
                except (TypeError, ValueError):
                    age_h = None
                if status == "ok" and price:
                    if prefetched_drift is not None:
                        pair = prefetched_drift.get(
                            (p["id"], s["shop"], s["method"]), [])
                    else:
                        pair = [r["landed_dkk"] for r in conn.execute(
                            """SELECT landed_dkk FROM observations
                               WHERE product_id=:pid AND shop=:shop AND method=:m
                                 AND status='ok' AND landed_dkk > 0
                               ORDER BY id DESC LIMIT 2""",
                            {"pid": p["id"], "shop": s["shop"],
                             "m": s["method"]}).fetchall()]
                    if len(pair) >= 2:
                        cur, old = pair[0], pair[1]
                        if old and (cur > old * 2 or cur < old * 0.5):
                            flag = "⚠ possible mis-parse/drift"
            watch_awaiting = is_watch and status not in ("ok", "skipped")
            if watch_awaiting:
                # A watch with no price yet is the EXPECTED state, not a broken
                # source — rank it neutral (not red) so the dashboard stays a
                # signal, and label it clearly.
                rank = 2
                status = "awaiting price"
                if not err:
                    err = "watch — no price listed yet"
            elif status not in ("ok", "skipped"):
                rank = 0                                    # error / no data
            elif flag:
                rank = 1
            elif status == "skipped":
                rank = 2
            elif age_h is not None and age_h > stale_h:
                rank = 2                                    # stale
            else:
                rank = 3                                    # ok + fresh
            rows.append({
                "product": p["name"], "shop": s.get("shop"),
                "method": s.get("method"), "status": status,
                "landed": price, "age_h": age_h, "error": err,
                "flag": flag, "rank": rank, "watch": is_watch,
            })
    rows.sort(key=lambda r: (r["rank"], r["product"]))
    return rows


def hours_since_last_scan(conn: Database) -> Optional[float]:
    """Hours since the last COMPLETED scan, or None if none has finished yet."""
    row = conn.execute(
        "SELECT finished_at FROM scans WHERE finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row["finished_at"]:
        return None
    try:
        last = datetime.fromisoformat(row["finished_at"])
    except (TypeError, ValueError):
        return None
    return (datetime.now() - last).total_seconds() / 3600.0


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
             "reason": verdicts[p["id"]].reason,
             "url": verdicts[p["id"]].url or verdicts[p["id"]].cheapest_url,
             "price": verdicts[p["id"]].cheapest_dkk,
             "shop": verdicts[p["id"]].cheapest_shop,
             "trigger": p.get("triggers", {}).get("buy_below_dkk")}
            for p in cfg["products"] if verdicts[p["id"]].code == "BUY"]
    current_items = [
        {"id": p["id"], "name": p["name"],
         "price": verdicts[p["id"]].cheapest_dkk,
         "shop": verdicts[p["id"]].cheapest_shop,
         "url": verdicts[p["id"]].cheapest_url}
        for p in alertable_products(cfg["products"])
        if verdicts[p["id"]].cheapest_dkk is not None]
    # Watches that ARE priced this scan (verified in-stock landed price). A
    # debut here is the "preorder went live" event notify_watch_live pings.
    watch_items = [
        {"id": p["id"], "name": p["name"],
         "price": verdicts[p["id"]].cheapest_dkk,
         "shop": verdicts[p["id"]].cheapest_shop,
         "url": verdicts[p["id"]].cheapest_url,
         "target": p.get("triggers", {}).get("buy_below_dkk")}
        for p in cfg["products"]
        if p.get("watch") and verdicts[p["id"]].cheapest_dkk is not None]
    # All diffs are gated on the webhook, same rule as notify_new_buys: without
    # somewhere to send them, leave the state for the app-side scan to diff.
    new_ids = notify_new_buys(conn, webhook, buys) if webhook else None
    drops = (notify_price_drops(conn, webhook, current_items,
                                cfg["settings"].get("price_drop_pct", 10))
             if webhook else None)
    watch_live = (notify_watch_live(conn, webhook, watch_items,
                                    exclude_ids=new_ids or [])
                  if webhook else None)
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
        "price_drops": drops,          # None = diff skipped (no webhook)
        "watch_live": watch_live,      # None = diff skipped (no webhook)
    }
