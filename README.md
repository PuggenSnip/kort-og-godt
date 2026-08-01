# Kort og Godt 🃏

Local, read-only price scanner **and collection tracker** for TCG sealed
products. One button: **SCAN**. Fetches live prices from configured Danish/US
shops, compares them against your triggers, stores history in SQLite and
answers **BUY / WAIT / AVOID** per product. It never purchases anything, never
logs in, never bypasses bot protection.

## Run

**Easiest:** double-click the **Kort og Godt** shortcut on your Desktop
(or `Start Kort og Godt.bat` in this folder) — it creates the environment on
first run, then opens the app at http://localhost:8501.

Or from a terminal:

```
.venv\Scripts\python -m streamlit run app.py
```

**Desktop shortcut & icon.** The Desktop shortcut uses the trading-card logo
`kort_og_godt.ico`. To regenerate the icon (and the browser-tab favicon
`kort_og_godt.png`): `.venv\Scripts\python make_icon.py`. To recreate the
shortcut on another machine, point a new shortcut at `Start Kort og Godt.bat`
with icon `kort_og_godt.ico`.

Tests:

```
.venv\Scripts\python -m pytest -q
```

## Share it with your group (live, multi-user)

By default the app uses a **local** SQLite database (single machine). To let
several people on different PCs see the **same live data**, deploy it once to
Streamlit Community Cloud with a shared Postgres database — everyone opens one
URL, and the data (scans, Cardmarket entries, collection, config) is shared.
Set `DATABASE_URL` (shared Postgres) and `APP_PASSWORD` (shared login) as
secrets; leave them unset for local single-user use. Optionally set
`DISCORD_WEBHOOK_URL` to get a Discord ping when a product newly flips to BUY
after a scan. Full step-by-step in **[DEPLOY.md](DEPLOY.md)**.

## What's new in v0.3 — wider, honest coverage

- **Fætter BR (br.dk) is now a scanned source** — the old "not scannable"
  note was stale: BR's robots.txt only blocks its search, not product pages,
  and product pages carry clean JSON-LD price data. Verified live (Pitch
  Black ETB, 599 kr, fri fragt > 599). Powered by a new **generic
  `jsonld_product` method** that works on any shop embedding schema.org
  Product data — usable for future non-Shopify shops straight from Config.
- **Preferred-shop tie-break**: `settings.preferred_shops` names shops that
  win the "cheapest" pick on an **exact** landed-price tie, with the reason
  shown on the verdict. Seeded: **br.dk — 1 års ombytning på uåbnede varer**
  (a same-price BR purchase is strictly better: full downside hedge). A
  genuinely lower price elsewhere always beats the preference.
- **3 new Danish shops** (all independently verified live before adding):
  **cardx.dk**, **flinamania.dk**, **spilforsyningen.dk** — exact box/ETB
  listings only, matched by verified product handle. Notably: first-ever
  redundancy for both Riftbound displays, three new Pitch Black ETB sources,
  and a second Hobbit Play Booster source. (halmeshule.dk was checked too but
  carries none of our exact SKUs — dropped rather than guessed.)
- **kelz0r.dk unblocked the honest way.** Its robots.txt only forbids the
  *search results* page — product and category pages are allowed. The dead
  robots-blocked search sources are gone; validated direct product-page
  sources replace them (Pitch Black Booster Box 1.849,95, Hobbit Play Booster
  Display 1.379,95 — both live, in stock, allocation-risk flag detected).
  More kelz0r products can be added in Config with method `kelz0r_product`
  and a `…-p-NNN.html` URL.
- **Landed prices are honest now.** Per-shop shipping estimates
  (`settings.shop_shipping_dkk`, editable) feed every landed price; a shop
  with no figure shows **landed\*** — explicitly shelf price, not a fake
  all-in. BUY/AVOID triggers now compare true landed cost. symbizon.dk ships
  free > 599 kr (seeded 0); other DK shops are seeded at a flat 45 kr
  estimate — **tune these to reality in Config**.
- **Release sources reach existing deployments automatically.** The stored DB
  config is migrated add-only on startup (`config_version`): new sources are
  merged in, dead robots-blocked ones dropped, and your edited triggers,
  flags and notes are never touched.
- Checked but deferred to v0.4: generic JSON-LD parser (Faraos, Pokemons.dk
  and other non-Shopify shops), eBay Browse API, Swedish shops (SEK),
  price-plausibility guard, source-health dashboard.

## Earlier (v0.2) — shared-first

- **Per-person attribution** — a lightweight "who are you?" name picker on
  entry (no passwords). Holdings and manual Cardmarket entries are stamped with
  who added them, and the Collection tab shows a **per-person P/L** breakdown
  plus an *Everyone / just me* filter.
- **Cardmarket price-history chart** — each product expander plots your manual
  Cardmarket entries over time (in €).
- **Richer realized (sold) reporting** — per-sale **ROI %**, a **by-year**
  roll-up with a chart, and a **CSV export** of your sales.
- **Discord alerts (no cron)** — after a SCAN, products that *newly* flipped to
  BUY are posted to a Discord webhook. Set `DISCORD_WEBHOOK_URL` as a secret to
  enable; unset = off. (Scheduled/background alerts are planned for v0.3.)
- **Report an issue** — a small form in the Config tab saves feedback to the
  shared database for the maintainer.

## Earlier (v0.1 beta)

- **Collection / portfolio tracking** — value what you own from live prices,
  cost basis, **unrealized P/L**, and **realized P/L** on items you've sold,
  with a value-over-time chart (see below).
- **Scan dashboard** — verdict counts (BUY / falling / AVOID / HOLD /
  unverified) and an "Act now — BUY" callout at the top of the Scan tab.
- **Wider source coverage** — 151 JP, Final Fantasy and TMNT now have real
  mtgwebshop.dk sources (added 2026-07-31; some were out of stock then).
- **One-click Windows launcher** (`Start Kort og Godt.bat`).

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI — `streamlit run app.py` |
| `scanner.py` | Core logic (fetching, parsers, verdicts) — no Streamlit, unit-testable |
| `db.py` | Database layer (SQLAlchemy): local SQLite by default, shared Postgres via `DATABASE_URL` |
| `watchlist.json` | Seed products, per-shop URLs, triggers, FX & politeness settings |
| `collection.json` | Seed holdings (what you own) for value tracking |
| `kortoggodt.db` | Local SQLite: scan history, config, collection + 1 h HTTP cache (created on first run) |
| `DEPLOY.md` | How to host it online so 3 people share live data |
| `make_icon.py` | Regenerates the app icon (`kort_og_godt.ico` / `.png`) |
| `fixtures/synthetic/` | Hand-written fixtures the parser tests run against |
| `fixtures/live/` | Real responses, recorded on first successful fetch per URL |

## Data sources

- **symbizon.dk, mtgwebshop.dk, cardx.dk, flinamania.dk, spilforsyningen.dk,
  rogerz.dk** — Shopify JSON (`/products/<handle>.js`, fallback/search via
  `/products.json?limit=250`).
- **kelz0r.dk** — HTML scrape (osCommerce) of *allowed* product pages
  (`…-p-NNN.html`; robots.txt only forbids the search-results endpoint).
  Parses Danish prices + "På lager"; flags "Risiko for allokering".
- **br.dk (Fætter BR)** — generic JSON-LD product data (`jsonld_product`
  method; robots.txt only blocks their search). Preferred on exact price
  ties: 1 year of returns on unopened products.
- **epicpanda.dk** — HTML scrape, `1.999,95 DKK` format.
- **pricecharting.com** — US reference for trends (`reference_only`: shown,
  never feeds the verdict).
- **riporfliptcg.com** — Rip-EV % per set (reference only).
- **Cardmarket** — **not scraped** (Cloudflare + ToS). Per product you get a
  deep-link button and a manual €-entry field; entries are stored like
  scraped data (EUR × 7.46 → DKK).
- **eBay** — skipped in v1: needs API keys, and non-EU offers are
  uncompetitive after +25 % Danish VAT + ~160 kr fee.

## Politeness

Max 1 request/second/domain · identifying User-Agent · 15 s timeout ·
1 h response cache · robots.txt respected (a disallowed URL simply shows
UNVERIFIED). Personal use only.

## Verdict engine (priority order)

0. A set manual override flag (e.g. `riot_eol`) → **BUY**. This is an explicit
   human decision independent of scraped prices, so it sits above the data
   rules; any concurrent fetch failures are still listed.
1. Any buyable-source fetch/parse failed → **UNVERIFIED** (shows the last
   good value + its timestamp; never interpolates).
2. Cheapest verified in-stock price ≤ `buy_below_dkk` → **BUY** (shop +
   link). A **fresh** Cardmarket entry ≤ `cardmarket_buy_below_eur`
   (respecting `cardmarket_stable_days` when set) → **BUY**.
3. Today < 7-day average and above trigger → **WAIT — falling** (% vs 7d/30d).
4. Price ≥ `avoid_above_dkk` → **AVOID**.
5. Else → **HOLD/WAIT** with distance to trigger in kr.

Trends come only from your own scan history (DK shop prices — sporadic manual
Cardmarket entries are excluded so they can't fake a rise/fall). 7d/30d
averages need at least `trend_min_days_7d` / `trend_min_days_30d` distinct
days of data — before that the UI says *insufficient history* rather than
faking a trend.

**Skipped vs failed.** A source blocked by `robots.txt` is a permanent policy
*skip*, not a transient failure — it is shown as a note ("not checked") and
does **not** force the product to UNVERIFIED when other shops verified fine. A
product is UNVERIFIED only when a source that *should* work failed, or when
every source is skipped and no Cardmarket price is entered.

**Stale Cardmarket entries.** A manually entered Cardmarket price never
auto-refreshes, so one older than `cardmarket_max_age_days` (default 30) stops
driving a BUY and is shown as a stale note instead. Every Cardmarket-driven
verdict includes the entry date.

## Collection (portfolio) tracking

The **🎴 Collection** tab tracks what you own and its value:

- Add holdings (item name, quantity, unit cost, acquired date). Link a holding
  to a watchlist product to value it **automatically** from the latest scan;
  leave it unlinked and give a **manual value** for anything the scanner can't
  price.
- **Valuation basis** toggle: *Cheapest DK shop* (replacement cost, the
  default) or *Cardmarket* (resale, from your manual entries).
- Shows **market value**, **cost basis**, and **unrealized P/L** (kr and %).
  P/L covers only holdings that have both a value and a recorded cost — a
  missing cost is never treated as zero. A holding with no verified price on
  the chosen basis is **UNVERIFIED** and left out of the totals; a valued
  holding with no cost counts toward market value but not P/L. Never guessed.
- **Value over time**: a snapshot of the collection's value is recorded
  automatically after every scan (one point per day; there's also a manual
  *Snapshot value now* button), charted as market value vs cost basis.
- **Per-person (v0.2)**: on a shared deployment each holding is stamped with
  who added it. A *By person* table breaks down market value, cost, and both
  unrealized and realized P/L per person, and an *Everyone / just me* filter
  scopes the holdings table. Legacy holdings with no owner show as *(unknown)*.
- **Realized reporting (v0.2)**: the *Realized (sold)* section adds per-sale
  **ROI %**, a **by-year** roll-up with a P/L bar chart, and a **CSV export**.
- The markdown export includes a collection summary.

Holdings live in `collection.json` (editable/backup-able); the value history
lives in SQLite. `collection.json` ships with two clearly-labelled EXAMPLE
holdings — edit or delete them for your own collection.

## FX

EUR/DKK is **hardcoded at 7.46** (pegged). USD/DKK is a config value
(default 6.40), editable in the Config tab.

## Notes on the seed watchlist (as of 2026-07-31)

Verified live against the real shops while building; a few practical facts:

- **kelz0r.dk search is `robots.txt`-disallowed** (`/magic/advanced_search_result.php`).
  Those sources therefore show as *skipped*. kelz0r **product** pages
  (`…-p-NNN.html`, method `kelz0r_product`) are allowed and work — paste a
  direct product URL in Config to get a kelz0r price for a product.
- **151 JP**, **MTG Final Fantasy** and **TMNT** now have real mtgwebshop.dk
  sources (added 2026-07-31). All three were *out of stock* there at the time,
  so you'll see a verified out-of-stock reference price and a HOLD/CM-driven
  verdict rather than a blank UNVERIFIED. They still lean on their
  Cardmarket-driven triggers.
- **pricecharting.com** is wired as a US reference for *Prismatic Evolutions*
  (verified URL; `reference_only`, so it never moves a verdict). Add more
  pricecharting references in Config with method `pricecharting`,
  `currency: USD`, `reference_only: true`, `pc_field: used` (sealed products
  list under the "Loose Price" column).
- **rogerz.dk** is a supported Shopify source (methods `shopify_handle` /
  `shopify_search`) but currently stocks graded singles and prerelease events
  rather than the sealed boxes on this list, so it is not seeded. Add it in
  Config if it starts carrying a tracked product.
- **Epic Panda** listed *Riftbound Origins* on 2026-07-29 but has since
  delisted it (its URL now 302s to the frontpage — which the parser correctly
  reports rather than misreading as a 0 kr price), so that source was removed.

All of the above is editable in-app — trigger values encode research decisions
dated 2026-07-29 and are meant to be changed as markets move.

## Honesty rules

- Every number on screen is traceable to a URL + timestamp (source tables
  in each product expander and in the markdown export).
- If a site changes layout, parsing fails → the product shows UNVERIFIED.
  Stale data is never silently shown as fresh.
- Trigger values encode research decisions dated 2026-07-29; edit them
  in-app as markets move.
