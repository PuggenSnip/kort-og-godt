"""Kort og Godt — TCG sealed-product price scanner (Streamlit UI).

Run with:  streamlit run app.py
Read-only tool: fetches prices, compares against triggers, stores history.
It never purchases anything.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import scanner

APP_VERSION = "1.2.0"    # semver MAJOR.MINOR.PATCH. 1.0 = first stable release;
                         # minor bumps = feature/watchlist waves after it.

# Use the trading-card logo as the browser-tab icon (fallback to an emoji).
try:
    from PIL import Image as _PILImage
    _icon_file = Path(__file__).resolve().parent / "kort_og_godt.png"
    _PAGE_ICON = _PILImage.open(_icon_file) if _icon_file.exists() else "🃏"
except Exception:       # noqa: BLE001 — branding must never block startup
    _PAGE_ICON = "🃏"

st.set_page_config(page_title="Kort og Godt", page_icon=_PAGE_ICON,
                   layout="wide")

_CSS = """
<style>
/* ===== Kort og Godt — v0.9 visual system (black & gold trading-card) ===== */
:root{
  --kog-gold:#D6B05C; --kog-gold-bright:#EACE86; --kog-gold-dim:rgba(214,176,92,.26);
}
.block-container{ padding-top:2.1rem; max-width:1320px; }

/* ----- Custom header ----- */
.kog-header{
  display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
  border-bottom:1px solid var(--kog-gold-dim);
  padding-bottom:12px; margin:0 0 6px 0;
}
.kog-header .kog-logo{ font-size:2.0rem; line-height:1; }
.kog-header h1.kog-title{
  margin:0; padding:0; font-size:2.0rem; font-weight:800; letter-spacing:.4px;
  background:linear-gradient(92deg,var(--kog-gold-bright),var(--kog-gold));
  -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
}
.kog-header .kog-tagline{ color:#B7B0A2; font-size:.9rem; }
.kog-header .kog-ver{
  margin-left:auto; color:var(--kog-gold); font-weight:700; font-size:.76rem;
  border:1px solid var(--kog-gold-dim); padding:2px 11px; border-radius:999px;
  white-space:nowrap;
}

/* ----- Metric cards ----- */
[data-testid="stMetric"]{
  background:linear-gradient(180deg,rgba(214,176,92,.07),rgba(214,176,92,.015));
  border:1px solid var(--kog-gold-dim); border-radius:14px; padding:12px 16px;
}
[data-testid="stMetricValue"]{ color:var(--kog-gold-bright); font-weight:800; }
[data-testid="stMetricLabel"]{ opacity:.9; font-weight:600; }

/* ----- Primary SCAN button: gold ----- */
div[data-testid="stButton"] > button[kind="primary"]{
  background:linear-gradient(180deg,#EACE86,#C79A3E); color:#1c1608; border:none;
  font-size:1.45rem; font-weight:800; padding:.7rem 2.4rem; border-radius:12px;
  box-shadow:0 6px 18px rgba(199,154,62,.28);
}
div[data-testid="stButton"] > button[kind="primary"]:hover{
  filter:brightness(1.06); box-shadow:0 8px 22px rgba(199,154,62,.42);
}

/* ----- Tabs ----- */
[data-baseweb="tab-list"]{ gap:6px; }
button[data-baseweb="tab"]{ font-weight:600; }
button[data-baseweb="tab"][aria-selected="true"]{ color:var(--kog-gold-bright); }
[data-baseweb="tab-highlight"]{ background-color:var(--kog-gold)!important; }

/* ----- Section headings (st.header/subheader): gold accent bar ----- */
[data-testid="stHeadingWithActionElements"]{ position:relative; padding-left:13px; }
[data-testid="stHeadingWithActionElements"]::before{
  content:""; position:absolute; left:0; top:.2em; bottom:.2em; width:3px;
  border-radius:2px; background:var(--kog-gold);
}

/* ----- Expanders ----- */
[data-testid="stExpander"]{
  border:1px solid rgba(214,176,92,.16); border-radius:12px;
  background:rgba(255,255,255,.012);
}
[data-testid="stExpander"] summary:hover{ color:var(--kog-gold-bright); }

/* ----- Dataframes ----- */
[data-testid="stDataFrame"]{ border:1px solid rgba(214,176,92,.14); border-radius:10px; }

/* ----- Sidebar ----- */
[data-testid="stSidebar"]{ border-right:1px solid var(--kog-gold-dim); }
</style>
"""
st.markdown(_CSS, unsafe_allow_html=True)


def _secret(name: str, default=None):
    """Read a value from Streamlit secrets or the environment (either works)."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:       # noqa: BLE001 — no secrets file locally is fine
        pass
    return os.environ.get(name, default)


def _require_login() -> None:
    """Optional shared-password gate. Active only when APP_PASSWORD is set
    (e.g. on the shared cloud deployment); a no-op for local single-user runs."""
    password = _secret("APP_PASSWORD")
    if not password or st.session_state.get("_authed"):
        return
    st.title("🔒 Kort og Godt")
    st.caption("Enter the shared password to continue.")
    entered = st.text_input("Password", type="password")
    if entered and entered == password:
        st.session_state["_authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password.")
    st.stop()


def _require_person(conn, cfg) -> str:
    """Ask which shared user is here, so holdings and Cardmarket entries can be
    attributed. Chosen once per session; a new name joins the roster."""
    if st.session_state.get("_person"):
        return st.session_state["_person"]
    people = list(cfg["settings"].get("people", []))
    st.title("🃏 Kort og Godt")
    st.caption("Who's using the app? This labels what you add on the shared "
               "data — pick your name or add it.")
    ADD_NEW = "➕ Add a new name…"
    options = (people + [ADD_NEW]) if people else [ADD_NEW]
    choice = st.selectbox("I am…", options)
    name = "" if choice == ADD_NEW else choice
    if choice == ADD_NEW:
        name = st.text_input("Your name").strip()
    if st.button("Continue", type="primary"):
        if not name:
            st.warning("Pick a name or type a new one.")
            st.stop()
        if name not in people:
            cfg["settings"].setdefault("people", []).append(name)
            scanner.put_config(conn, cfg)
        st.session_state["_person"] = name
        st.rerun()
    st.stop()


@st.cache_resource
def _db():
    # DATABASE_URL (secrets/env) -> shared Postgres; unset -> local SQLite file.
    return scanner.get_db(_secret("DATABASE_URL"))


_require_login()
conn = _db()
cfg = scanner.get_config(conn)
settings = cfg["settings"]
person = _require_person(conn, cfg)
with st.sidebar:
    st.caption(f"👤 You are **{person}**")
    if st.button("Switch user"):
        st.session_state.pop("_person", None)
        st.rerun()


def flash(msg: str) -> None:
    """Queue a success message to survive the st.rerun() that follows a save."""
    st.session_state["_flash"] = msg


def last_scan_display() -> str:
    row = conn.execute(
        "SELECT finished_at FROM scans WHERE finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    return row["finished_at"].replace("T", " ") if row else "never"


def pl_breakdown_rows(bucket_map: dict, label_col: str) -> list:
    """Format a value_collection breakdown map (by_person/by_game/by_set) into
    display rows — same columns for all three so they read consistently."""
    out = []
    for key in sorted(bucket_map):
        b = bucket_map[key]
        upct = (b["unrealized_pl"] / b["cost"] * 100) if b["cost"] else None
        out.append({
            label_col: key,
            "Items": b["n_items"],
            "Market value": scanner.fmt_dkk(b["market_value"], 0),
            "Cost basis": scanner.fmt_dkk(b["cost"], 0),
            "Unrealized P/L": scanner.fmt_dkk(b["unrealized_pl"], 0),
            "U-ROI": (f"{upct:+.1f}%" if upct is not None else "–"),
            "Sold": b["n_sold"],
            "Realized P/L": scanner.fmt_dkk(b["realized_pl"], 0),
        })
    return out


st.markdown(
    f"""
    <div class="kog-header">
      <span class="kog-logo">🃏</span>
      <h1 class="kog-title">Kort og Godt</h1>
      <span class="kog-tagline">TCG sealed-price radar — buy Danish, watch global</span>
      <span class="kog-ver">v{APP_VERSION}</span>
    </div>
    """,
    unsafe_allow_html=True)

if "_flash" in st.session_state:
    st.success(st.session_state.pop("_flash"))

# Staleness fallback: if the scheduled scan hasn't run in a while, nudge a human
# to refresh (data stays current even when GitHub's cron misses a slot).
_stale_h = scanner.hours_since_last_scan(conn)
if _stale_h is not None and _stale_h >= settings.get("stale_hours", 8):
    st.warning(f"⚠ Prices are **{_stale_h:.0f} h** old — a scheduled scan may "
               "have been missed. Press **🔍 SCAN** on the Scan tab to refresh.")

tab_scan, tab_collection, tab_config = st.tabs(
    ["📊 Scan", "🎴 Collection", "⚙️ Config"])


# ---------------------------------------------------------------------------
# Scan tab
# ---------------------------------------------------------------------------

with tab_scan:
    col_btn, col_info = st.columns([1, 2])
    with col_btn:
        scan_clicked = st.button("🔍  SCAN", type="primary",
                                 width="stretch")
    with col_info:
        metric_slot = st.empty()
        metric_slot.metric("Last scan", last_scan_display())

    scan_succeeded = False
    if scan_clicked:
        n_sources = sum(
            1 for p in cfg["products"] for s in p.get("sources", [])
            if s["method"] != "cardmarket_manual")
        progress_bar = st.progress(0.0, text="Starting scan …")
        failures: list[str] = []
        done = 0

        with st.status("Scanning sources …", expanded=True) as status:
            def on_progress(product_name: str, shop: str,
                            obs: scanner.Observation) -> None:
                global done
                if obs.status == "manual":
                    return          # manual CM sources aren't fetched
                done += 1
                if obs.status == "ok":
                    price = (f"{obs.price_native:g} %" if obs.currency == "%"
                             else scanner.fmt_dkk(obs.landed_dkk))
                    st.write(f"✅ {shop} — {product_name}: {price}")
                elif obs.status == "skipped":
                    st.write(f"⏭️ {shop} — {product_name}: {obs.error}")
                else:
                    st.write(f"❌ {shop} — {product_name}: {obs.error}")
                    failures.append(f"{shop} / {product_name}: {obs.error}")
                progress_bar.progress(min(done / max(n_sources, 1), 1.0),
                                      text=f"{done}/{n_sources} sources")

            try:
                scan_id, observations = scanner.run_scan(cfg, conn, on_progress)
            except Exception as e:  # noqa: BLE001 — never leave a half-scan crash
                status.update(label=f"Scan aborted: {type(e).__name__}: {e}",
                              state="error")
                observations = []
            else:
                scan_succeeded = True
                ok_n = sum(1 for o in observations if o.status == "ok")
                status.update(
                    label=f"Scan #{scan_id} done — {ok_n}/{len(observations)} "
                          "sources OK", state="complete")
                # Auto-record collection value using the just-scanned prices.
                try:
                    scanner.snapshot_collection(
                        conn, cfg, scanner.get_collection(conn))
                except Exception:       # collection tracking must never break a scan
                    pass

        metric_slot.metric("Last scan", last_scan_display())
        if failures:
            st.error("**Failed sources** (their products show UNVERIFIED):\n\n"
                     + "\n".join(f"- {f}" for f in failures))

    # -- Results table ------------------------------------------------------
    st.subheader("Verdicts")
    # Shops with a real shipping figure show plain "landed"; anything else is
    # shelf price and gets a * so "landed" never quietly overpromises.
    _ship_known = scanner.shipping_known_shops(cfg)

    def landed_fmt(price, shop, decimals=2):
        s = scanner.fmt_dkk(price, decimals)
        if price is None or shop in _ship_known:
            return s
        return f"{s} *"

    rows = []
    verdicts: dict[str, scanner.Verdict] = {}
    for product in cfg["products"]:
        v = scanner.product_verdict(conn, product, settings)
        verdicts[product["id"]] = v
        t = v.trend

        def arrow(avg):
            # Distinguish "not enough scans yet" from a real flat/again trend.
            if not t or avg is None:
                return "insufficient history"
            pct = t.pct_vs(avg)
            if pct is None:
                return "no price today"
            return f"{'↓' if pct < 0 else '↑' if pct > 0 else '→'} {pct:+.1f}%"

        rows.append({
            "Product": product["name"],
            "Verdict": f"{v.emoji} {v.label}",
            "Cheapest (landed)": landed_fmt(v.cheapest_dkk, v.cheapest_shop),
            "Shop": v.cheapest_shop or "–",
            "Stock": ("på lager" if v.in_stock else
                      "–" if v.in_stock is None else "udsolgt"),
            "vs trigger": (scanner.fmt_dkk(v.vs_trigger_dkk, 0)
                           if v.vs_trigger_dkk is not None else "–"),
            "7d": arrow(t.avg7 if t else None),
            "30d": arrow(t.avg30 if t else None),
            "Why": v.reason,
            # None (not "") renders as an empty cell — no dead 'open shop' link.
            "Link": v.cheapest_url or None,
        })
    # Summary bar — verdict counts + a prominent "act now" callout.
    counts = {}
    for v in verdicts.values():
        counts[v.code] = counts.get(v.code, 0) + 1
    mc = st.columns(6)
    mc[0].metric("🟢 BUY", counts.get("BUY", 0))
    mc[1].metric("⏳ Watch", counts.get("WATCH", 0))
    mc[2].metric("🟡 Falling", counts.get("WAIT_FALLING", 0))
    mc[3].metric("🔴 AVOID", counts.get("AVOID", 0))
    mc[4].metric("⚪ HOLD", counts.get("HOLD", 0))
    mc[5].metric("⚫ Unverified", counts.get("UNVERIFIED", 0))

    buys = [(p["name"], verdicts[p["id"]]) for p in cfg["products"]
            if verdicts[p["id"]].code == "BUY"]
    if buys:
        st.success("**Act now — BUY:**\n\n" + "\n".join(
            f"- **{name}** — {v.reason}"
            + (f"  ·  [open shop]({v.url})" if v.url else "")
            for name, v in buys))

    # Watches — parked/monitored preorders & grails. Shown separately from the
    # act-now BUY box: no action needed, just tracking for the target moment.
    watches = [(p["name"], verdicts[p["id"]]) for p in cfg["products"]
               if verdicts[p["id"]].code == "WATCH"]
    if watches:
        st.info("**⏳ Watching (preorders / grails — alert fires when priced ≤ "
                "target):**\n\n" + "\n".join(
                    f"- **{name}** — {v.reason}"
                    + (f"  ·  [open shop]({v.cheapest_url})"
                       if v.cheapest_url else "")
                    for name, v in watches))

    # Discord ping: after a successful scan, post the products that flipped INTO
    # BUY since the previous scan. No cron — it rides this SCAN. The webhook is a
    # secret (DISCORD_WEBHOOK_URL); if unset, notify_new_buys just tracks state.
    if scan_succeeded:
        current_buys = [{"id": p["id"], "name": p["name"],
                         "reason": verdicts[p["id"]].reason,
                         "url": verdicts[p["id"]].url or verdicts[p["id"]].cheapest_url,
                         "price": verdicts[p["id"]].cheapest_dkk,
                         "shop": verdicts[p["id"]].cheapest_shop,
                         "trigger": p.get("triggers", {}).get("buy_below_dkk")}
                        for p in cfg["products"]
                        if verdicts[p["id"]].code == "BUY"]
        current_items = [{"id": p["id"], "name": p["name"],
                          "price": verdicts[p["id"]].cheapest_dkk,
                          "shop": verdicts[p["id"]].cheapest_shop,
                          "url": verdicts[p["id"]].cheapest_url}
                         for p in cfg["products"]
                         if verdicts[p["id"]].cheapest_dkk is not None]
        watch_items = [{"id": p["id"], "name": p["name"],
                        "price": verdicts[p["id"]].cheapest_dkk,
                        "shop": verdicts[p["id"]].cheapest_shop,
                        "url": verdicts[p["id"]].cheapest_url,
                        "target": p.get("triggers", {}).get("buy_below_dkk")}
                       for p in cfg["products"]
                       if p.get("watch")
                       and verdicts[p["id"]].cheapest_dkk is not None]
        webhook = _secret("DISCORD_WEBHOOK_URL")
        new_ids = scanner.notify_new_buys(conn, webhook, current_buys)
        drops = scanner.notify_price_drops(
            conn, webhook, current_items, settings.get("price_drop_pct", 10))
        watch_live = scanner.notify_watch_live(
            conn, webhook, watch_items, exclude_ids=new_ids)
        if webhook and (new_ids or drops or watch_live):
            parts = ([f"{len(new_ids)} new BUY(s)"] if new_ids else []) + \
                    ([f"{len(drops)} price drop(s)"] if drops else []) + \
                    ([f"{len(watch_live)} watch(es) live"] if watch_live else [])
            st.toast("Discord: pinged " + ", ".join(parts))

    st.dataframe(
        rows, width="stretch", hide_index=True,
        column_config={"Link": st.column_config.LinkColumn(
            "Link", display_text="open shop")},
    )
    if any((r["Cheapest (landed)"] or "").endswith("*") for r in rows):
        st.caption("\\* shipping unknown for this shop — price shown is the "
                   "shelf price. Set it in Config → settings → "
                   "`shop_shipping_dkk` to make 'landed' honest.")

    st.download_button(
        "📄 Export markdown report",
        data=scanner.export_markdown(cfg, conn),
        file_name=f"kortoggodt-report-{datetime.now():%Y-%m-%d-%H%M}.md",
        mime="text/markdown",
    )

    # -- Per-product detail -------------------------------------------------
    st.subheader("Products")
    for product in cfg["products"]:
        v = verdicts[product["id"]]
        with st.expander(f"{v.emoji} {product['name']} — {v.label}"):
            st.write(f"**Why:** {v.reason or v.label}")
            for note in v.notes:
                st.warning(note)
            for f_ in v.failures:
                st.error(f_)
            if product.get("notes"):
                st.caption(product["notes"])

            # Source table — every number traceable to URL + timestamp.
            latest = scanner.latest_observations(conn, product["id"],
                                                 product.get("sources", []))
            if latest:
                src_rows = []
                for r in latest:
                    if r["status"] == "ok" and r["currency"] == "%":
                        price = f"EV {r['price_native']:g} %"
                    elif r["status"] == "ok":
                        price = landed_fmt(r["landed_dkk"], r["shop"])
                    elif r["status"] == "skipped":
                        price = "skipped"
                    else:
                        price = "UNVERIFIED"
                    src_rows.append({
                        "Shop": r["shop"],
                        "Matched title": r["title"] or "–",
                        "Price (landed)": price,
                        "Native": (f"{r['price_native']:.2f} {r['currency']}"
                                   if r["price_native"] is not None else "–"),
                        "Stock": {1: "på lager", 0: "udsolgt"}.get(
                            r["in_stock"], "?"),
                        "Seen": r["observed_at"].replace("T", " "),
                        "Error": r["error"] or "",
                        "URL": r["url"],
                    })
                st.dataframe(src_rows, width="stretch",
                             hide_index=True,
                             column_config={"URL": st.column_config.LinkColumn(
                                 "URL", display_text="source")})
            else:
                st.info("No observations yet — hit SCAN.")

            # History sparkline.
            series = scanner.daily_cheapest_series(conn, product["id"])
            if len(series) >= 2:
                st.line_chart({"cheapest landed DKK":
                               {d: p for d, p in series}},
                              height=160)
            else:
                st.caption("Sparkline appears after scans on ≥ 2 days.")

            # Cardmarket manual-entry history (EUR, plotted as-entered — the
            # honest signal, not the EUR_DKK-pegged DKK). The buy threshold is
            # overlaid as a flat line so "how far below target" reads at a glance.
            cm = product.get("cardmarket") or {}
            cm_below = product.get("triggers", {}).get("cardmarket_buy_below_eur")
            cm_hist = scanner.cardmarket_series(conn, product["id"])
            if len(cm_hist) >= 2:
                st.caption("Cardmarket manual entries (€)")
                chart = {"Cardmarket €":
                         {t[:16].replace("T", " "): p for t, p in cm_hist}}
                if cm_below:
                    chart["Buy threshold €"] = {
                        t[:16].replace("T", " "): cm_below for t, _ in cm_hist}
                st.line_chart(chart, height=180)

            # Compact stats line: latest, freshness, range, target status.
            stats = scanner.cardmarket_stats(conn, product, settings)
            if stats:
                bits = [f"latest **€{stats['latest_eur']:g}** "
                        f"({scanner.fmt_dkk(stats['latest_dkk'], 0)}) on "
                        f"{stats['latest_date'][:10]}"]
                if stats["age_days"] is not None:
                    bits.append(f"{stats['age_days']}d ago"
                                + (" — stale" if stats["stale"] else ""))
                bits.append(f"range €{stats['min_eur']:g}–€{stats['max_eur']:g} "
                            f"({stats['n']} entr{'y' if stats['n'] == 1 else 'ies'})")
                if stats["threshold_eur"]:
                    mark = ("✅ ≤ target" if stats["meets_threshold"]
                            else "above target")
                    bits.append(f"target €{stats['threshold_eur']:g} — {mark}")
                st.caption("💶 " + "  ·  ".join(bits))

            # Manual entry — with an optional backdate to fill in history.
            if cm.get("url"):
                cm_col1, cm_col2, cm_col3, cm_col4 = st.columns([1, 1, 1, 1])
                with cm_col1:
                    st.link_button("🔗 Check Cardmarket", cm["url"])
                with cm_col2:
                    eur = st.number_input(
                        "Lowest (€)", min_value=0.0, step=1.0, value=None,
                        key=f"cm_{product['id']}",
                        help="Paste the lowest Cardmarket price. Stored like "
                             "scraped data (EUR × 7.46 → DKK).")
                with cm_col3:
                    as_of = st.date_input(
                        "As of", value=date.today(), key=f"cmd_{product['id']}",
                        help="When this price was seen. Leave as today for a "
                             "current price; set earlier to fill in history — "
                             "backdated entries never change the verdict.")
                with cm_col4:
                    # No `disabled=` (a disabled button swallows the click that
                    # commits the number_input) — validate inside the handler.
                    if st.button("Save price", key=f"cm_save_{product['id']}"):
                        if eur is None or eur <= 0:
                            st.warning("Enter a price above 0 first.")
                        else:
                            # Only pass observed_at when backdating, so a normal
                            # today entry keeps its full timestamp (now).
                            obs_at = (None if as_of == date.today()
                                      else f"{as_of.isoformat()}T12:00:00")
                            scanner.add_manual_cardmarket_entry(
                                conn, product, float(eur), settings,
                                added_by=person, observed_at=obs_at)
                            flash(f"Saved Cardmarket €{eur:g} for "
                                  f"{product['name']} "
                                  f"({scanner.fmt_dkk(eur * scanner.EUR_DKK)})"
                                  + ("" if obs_at is None else f", as of {as_of}"))
                            st.rerun()

                # Manage / correct entries — list recent ones, delete a bad one.
                entries = scanner.list_cardmarket_entries(conn, product["id"])
                if entries:
                    with st.expander(
                            f"✏️ Manage Cardmarket entries ({len(entries)})"):
                        for e in entries:
                            ec1, ec2 = st.columns([4, 1])
                            by = (f" · {e['added_by']}"
                                  if e.get("added_by") else "")
                            ec1.write(f"€{e['price_native']:g} · "
                                      f"{e['observed_at'][:10]}{by}")
                            if ec2.button("🗑 Delete", key=f"cmdel_{e['id']}"):
                                scanner.delete_cardmarket_entry(conn, e["id"])
                                flash(f"Deleted Cardmarket entry "
                                      f"€{e['price_native']:g} "
                                      f"({e['observed_at'][:10]})")
                                st.rerun()


# ---------------------------------------------------------------------------
# Collection tab
# ---------------------------------------------------------------------------

with tab_collection:
    collection = scanner.get_collection(conn)
    prod_name_to_id = {p["name"]: p["id"] for p in cfg["products"]}
    id_to_name = {v: k for k, v in prod_name_to_id.items()}
    NONE_LABEL = "— (not on watchlist)"

    st.subheader("My collection")
    st.caption(
        "Track what you own and its value. Holdings linked to a watchlist "
        "product are valued automatically from the latest scan; anything else "
        "uses the manual value you enter. Value is never guessed — an item "
        "with no verified price shows as UNVERIFIED and is left out of totals.")

    basis = st.radio(
        "Valuation basis", scanner.VALUATION_BASES,
        index=scanner.VALUATION_BASES.index(
            collection["settings"].get("valuation_basis", "replacement")),
        format_func=lambda b: ("Cheapest DK shop (replacement cost)"
                               if b == "replacement"
                               else "Cardmarket (resale, from manual entries)"),
        horizontal=True, key="val_basis")
    if basis != collection["settings"].get("valuation_basis"):
        collection["settings"]["valuation_basis"] = basis
        scanner.put_collection(conn, collection)

    # -- Holdings editor ----------------------------------------------------
    st.caption("To record a sale, fill **Sold price (DKK)** (per unit) — the "
               "holding then moves to *Realized* below.")
    def _blank(x):
        if x is None or x == "":
            return True
        try:
            return bool(pd.isna(x))
        except (TypeError, ValueError):
            return False

    def _num(x):
        return None if _blank(x) else float(x)

    def _txt(x):
        return "" if _blank(x) else str(x).strip()

    editor_cols = ["id", "pid", "Item", "Added by", "Watchlist product", "Qty",
                   "Unit cost (DKK)", "Acquired", "Manual value (DKK)",
                   "Sold price (DKK)", "Sold date", "Notes"]
    editor_rows = []
    for h in collection["holdings"]:
        editor_rows.append({
            "id": h.get("id", ""),
            "pid": h.get("product_id") or "",     # hidden: preserves the link
            "Item": h.get("name", ""),
            "Added by": h.get("added_by") or "",
            "Watchlist product": id_to_name.get(h.get("product_id"),
                                                NONE_LABEL),
            "Qty": h.get("quantity", 1),
            "Unit cost (DKK)": h.get("unit_cost_dkk"),
            "Acquired": h.get("acquired", ""),
            "Manual value (DKK)": h.get("manual_value_dkk"),
            "Sold price (DKK)": h.get("sold_price_dkk"),
            "Sold date": h.get("sold_date", ""),
            "Notes": h.get("notes", ""),
        })
    # A typed DataFrame (not a bare list) so the columns exist even when there
    # are zero holdings — otherwise st.data_editor renders a column-less grid
    # with no "add row" affordance and the feature is dead on first run.
    editor_df = pd.DataFrame(editor_rows, columns=editor_cols)
    edited = st.data_editor(
        editor_df, num_rows="dynamic", hide_index=True,
        width="stretch", key="holdings_editor",
        column_config={
            "id": None,     # hidden internal key
            "pid": None,    # hidden watchlist-product id (survives display)
            "Added by": st.column_config.TextColumn(
                help="Who added this holding. New rows default to you; "
                     "edit to reassign."),
            "Watchlist product": st.column_config.SelectboxColumn(
                options=[NONE_LABEL] + list(prod_name_to_id),
                help="Link to a watchlist product to value it automatically."),
            "Qty": st.column_config.NumberColumn(min_value=0, step=1),
            "Unit cost (DKK)": st.column_config.NumberColumn(
                min_value=0.0, help="What you paid per unit (DKK)."),
            "Manual value (DKK)": st.column_config.NumberColumn(
                min_value=0.0,
                help="Per-unit value to use when not linked to a watchlist "
                     "product (or when it has no verified price)."),
            "Sold price (DKK)": st.column_config.NumberColumn(
                min_value=0.0,
                help="Per-unit sale price. Set this to mark the holding sold "
                     "(realized P/L)."),
        })
    if st.button("💾 Save collection"):
        new_holdings = []
        for i, r in enumerate(edited.to_dict("records")):
            item = _txt(r.get("Item"))
            pname = r.get("Watchlist product")
            pname = NONE_LABEL if _blank(pname) else pname
            orig_pid = _txt(r.get("pid")) or None
            if pname != NONE_LABEL:
                pid = prod_name_to_id.get(pname)          # linked/relinked
            elif orig_pid and orig_pid not in prod_name_to_id.values():
                pid = orig_pid    # product removed from watchlist — keep link
            else:
                pid = None                                # deliberately unlinked
            if not item and not pid:
                continue                                  # skip blank rows
            if not item and pid:
                item = id_to_name.get(pid, pid)
            orig_id = _txt(r.get("id"))
            hid = orig_id or f"h{i}-{abs(hash(item)) % 100000}"
            # New rows (no original id) are attributed to the current user;
            # existing rows keep whatever the "Added by" cell shows (blank =
            # legacy/unknown, so a save never claims someone else's holdings).
            added_by = _txt(r.get("Added by")) or (person if not orig_id else None)
            new_holdings.append({
                "id": hid,
                "name": item,
                "product_id": pid,
                "added_by": added_by,
                "quantity": _num(r.get("Qty")) or 0,
                "unit_cost_dkk": _num(r.get("Unit cost (DKK)")),
                "acquired": _txt(r.get("Acquired")),
                "manual_value_dkk": _num(r.get("Manual value (DKK)")),
                "sold_price_dkk": _num(r.get("Sold price (DKK)")),
                "sold_date": _txt(r.get("Sold date")),
                "notes": _txt(r.get("Notes")),
            })
        collection["holdings"] = new_holdings
        scanner.put_collection(conn, collection)
        flash(f"Saved {len(new_holdings)} holding(s)")
        st.rerun()

    # -- Valuation summary --------------------------------------------------
    val = scanner.value_collection(conn, cfg, collection, basis=basis)
    if val["n_items"] == 0 and val["n_sold"] == 0:
        st.info("No holdings yet — add rows above (type an item, optionally "
                "link a watchlist product), then **Save collection**.")
    elif val["n_items"] > 0:
        pl = val["unrealized_pl"]
        pct = (pl / val["total_cost"] * 100) if val["total_cost"] else None
        c1, c2, c3 = st.columns(3)
        c1.metric("Market value", scanner.fmt_dkk(val["total_value"], 0),
                  help="Current worth of all holdings with a verified value.")
        c2.metric("Cost basis", scanner.fmt_dkk(val["total_cost"], 0),
                  help="What you paid for the holdings that both have a value "
                       "and a recorded cost (the P/L set).")
        c3.metric("Unrealized P/L", scanner.fmt_dkk(pl, 0),
                  delta=(f"{pct:+.1f}%" if pct is not None else None),
                  help="Market value minus cost, over holdings that have both "
                       "a value and a recorded cost.")
        if val["n_unverified"]:
            st.warning(
                f"{val['n_valued']} of {val['n_items']} holdings valued — "
                f"{val['n_unverified']} UNVERIFIED (no verified price on the "
                "**" + basis + "** basis) and excluded from Market value / "
                "P/L. Enter a manual value or a Cardmarket price to include "
                "them.")
        if val["n_valued_no_cost"]:
            st.info(
                f"{val['n_valued_no_cost']} valued holding(s) worth "
                f"{scanner.fmt_dkk(val['value_no_cost'], 0)} have no recorded "
                "cost, so they count toward Market value but not P/L. Add a "
                "unit cost to include them.")

        # P/L broken down three ways. Each is shown only when it actually splits
        # the portfolio (more than one group), so single-row tables that just
        # repeat the totals above are suppressed.
        bp = val["by_person"]
        if len(bp) > 1 or (bp and "(unknown)" not in bp):
            st.markdown("**By person**")
            st.dataframe(pl_breakdown_rows(bp, "Person"),
                         width="stretch", hide_index=True)

        bg = val["by_game"]
        if len(bg) > 1:
            st.markdown("**By game**")
            st.dataframe(pl_breakdown_rows(bg, "Game"),
                         width="stretch", hide_index=True)
            # Total P/L (unrealized + realized) per game — the portfolio at a glance.
            game_pl = {g: bg[g]["unrealized_pl"] + bg[g]["realized_pl"]
                       for g in bg}
            if any(v for v in game_pl.values()):
                st.bar_chart({"Total P/L (DKK)": game_pl}, height=200)

        bs = val["by_set"]
        if len(bs) > 1:
            st.markdown("**By set**")
            st.dataframe(pl_breakdown_rows(bs, "Set"),
                         width="stretch", hide_index=True)

        # Per-holding breakdown, optionally scoped to one person ("just me").
        present = sorted({(r.added_by or "(unknown)") for r in val["rows"]})
        who = st.selectbox("Show holdings for", ["Everyone"] + present,
                           key="hold_filter")
        shown = (val["rows"] if who == "Everyone"
                 else [r for r in val["rows"]
                       if (r.added_by or "(unknown)") == who])
        hrows = []
        for r in shown:
            hrows.append({
                "Item": r.name,
                "Added by": r.added_by or "–",
                "Qty": r.quantity,
                "Unit value": (scanner.fmt_dkk(r.unit_value_dkk)
                               if r.unit_value_dkk is not None else "UNVERIFIED"),
                "Line value": (scanner.fmt_dkk(r.line_value)
                               if r.line_value is not None else "–"),
                "Unit cost": (scanner.fmt_dkk(r.unit_cost_dkk)
                              if r.unit_cost_dkk is not None else "–"),
                "P/L": (scanner.fmt_dkk(r.line_pl)
                        if r.line_pl is not None else "–"),
                "Value from": (f"{r.value_source}"
                               + (f" @ {r.observed_at[:16].replace('T', ' ')}"
                                  if r.observed_at else "")) or "–",
            })
        st.dataframe(hrows, width="stretch", hide_index=True)

    # -- Realized (sold holdings) -------------------------------------------
    if val["n_sold"]:
        st.markdown("#### Realized (sold)")
        rpl = val["realized_pl"]
        rpct = (rpl / val["realized_cost"] * 100) if val["realized_cost"] else None
        r1, r2, r3 = st.columns(3)
        r1.metric("Proceeds", scanner.fmt_dkk(val["realized_proceeds"], 0))
        r2.metric("Cost of sold", scanner.fmt_dkk(val["realized_cost"], 0))
        r3.metric("Realized P/L", scanner.fmt_dkk(rpl, 0),
                  delta=(f"{rpct:+.1f}%" if rpct is not None else None))
        srows = []
        for s in val["sold_rows"]:
            srows.append({
                "Item": s["name"], "Qty": s["quantity"],
                "Added by": s.get("added_by") or "–",
                "Unit cost": (scanner.fmt_dkk(s["unit_cost_dkk"])
                              if s["unit_cost_dkk"] is not None else "–"),
                "Sold @": scanner.fmt_dkk(s["sold_price_dkk"]),
                "Proceeds": scanner.fmt_dkk(s["proceeds"]),
                "Realized P/L": (scanner.fmt_dkk(s["realized_pl"])
                                 if s["realized_pl"] is not None else "–"),
                "ROI": (f"{s['roi_pct']:+.1f}%"
                        if s.get("roi_pct") is not None else "–"),
                "Sold date": s["sold_date"] or "–",
            })
        st.dataframe(srows, width="stretch", hide_index=True)

        st.download_button(
            "📄 Export sales CSV",
            data=scanner.export_sales_csv(val),
            file_name=f"kortoggodt-sales-{datetime.now():%Y-%m-%d}.csv",
            mime="text/csv")

        # Realized rolled up by calendar year (numeric years first, then any
        # unparseable-date sales under "unknown").
        rby = val["realized_by_year"]
        if rby:
            keys = sorted(k for k in rby if k != "unknown")
            if "unknown" in rby:
                keys.append("unknown")
            yrows = []
            for k in keys:
                yb = rby[k]
                ypct = (yb["pl"] / yb["cost"] * 100) if yb["cost"] else None
                yrows.append({
                    "Year": str(k),
                    "Sold": yb["n"],
                    "Proceeds": scanner.fmt_dkk(yb["proceeds"], 0),
                    "Cost": scanner.fmt_dkk(yb["cost"], 0),
                    "Realized P/L": scanner.fmt_dkk(yb["pl"], 0),
                    "ROI": (f"{ypct:+.1f}%" if ypct is not None else "–"),
                })
            st.markdown("**Realized by tax year**")
            st.caption("Grouped by the calendar year of the sale date — the "
                       "Danish tax year. Sales with no parseable date fall under "
                       "'unknown'.")
            st.dataframe(yrows, width="stretch", hide_index=True)
            year_pl = {str(k): rby[k]["pl"] for k in keys if k != "unknown"}
            if year_pl:
                st.bar_chart({"Realized P/L (DKK)": year_pl}, height=200)

    # -- Value over time ----------------------------------------------------
    st.subheader("Value over time")
    scol1, scol2 = st.columns([1, 3])
    with scol1:
        if st.button("📸 Snapshot value now"):
            scanner.snapshot_collection(conn, cfg, collection)
            flash("Collection value snapshot saved")
            st.rerun()
    series = scanner.collection_value_series(conn)
    if len(series) >= 2:
        chart = {
            "Market value": {s["taken_at"][:10]: s["total_value_dkk"]
                             for s in series},
            "Cost basis": {s["taken_at"][:10]: s["total_cost_dkk"]
                           for s in series},
        }
        st.line_chart(chart, height=240)
        with scol2:
            st.caption("Auto-snapshotted after each scan (one point per day). "
                       "Both lines cover the valued holdings only.")
    else:
        with scol2:
            st.caption("The value-over-time chart appears after snapshots on "
                       "≥ 2 days. A snapshot is taken automatically each scan.")


# ---------------------------------------------------------------------------
# Config tab
# ---------------------------------------------------------------------------

with tab_config:
    # -- Source health ------------------------------------------------------
    st.subheader("Source health")
    st.caption("Every configured source and its latest scan — worst first, so "
               "a broken, stale, or drifting shop is visible at a glance.")
    _health = scanner.source_health(conn, cfg)
    _rank_emoji = {0: "🔴", 1: "🟠", 2: "🟡", 3: "🟢"}
    _hrows = [{
        "": _rank_emoji.get(r["rank"], "⚪"),
        "Product": r["product"], "Shop": r["shop"], "Method": r["method"],
        "Status": r["status"],
        "Landed": scanner.fmt_dkk(r["landed"], 0) if r["landed"] is not None else "–",
        "Age": (f"{r['age_h']:.0f} h" if r["age_h"] is not None else "–"),
        "Note": r["flag"] or r["error"] or "",
    } for r in _health]
    _bad = sum(1 for r in _health if r["rank"] <= 1)
    if _bad:
        st.warning(f"{_bad} source(s) need attention (errors or drift). "
                   "See the red/orange rows below.")
    st.dataframe(_hrows, width="stretch", hide_index=True)

    st.subheader("Triggers")
    st.caption("Edit and press **Save triggers**. Empty = no trigger. "
               f"EUR/DKK is hardcoded at {scanner.EUR_DKK} (pegged).")
    st.caption("**Watch** = a parked item (unpriced preorder or a grail far "
               "above target): it shows ⏳ WATCH instead of cluttering BUY/HOLD, "
               "and pings when it prices ≤ its BUY target (or first goes live).")
    trig_rows = []
    for p in cfg["products"]:
        t = p.get("triggers", {})
        trig_rows.append({
            "id": p["id"],
            "Product": p["name"],
            "Watch": bool(p.get("watch")),
            "BUY ≤ (DKK)": t.get("buy_below_dkk"),
            "AVOID ≥ (DKK)": t.get("avoid_above_dkk"),
            "CM BUY ≤ (€)": t.get("cardmarket_buy_below_eur"),
            "CM stable days": t.get("cardmarket_stable_days"),
            "Not before": t.get("not_before"),
        })
    edited = st.data_editor(
        trig_rows, hide_index=True, width="stretch",
        disabled=["id", "Product"], key="trig_editor",
        column_config={"Watch": st.column_config.CheckboxColumn(
            "Watch", help="Parked/monitored item — shows ⏳ WATCH until it "
                          "reaches its BUY target.")})
    if st.button("💾 Save triggers"):
        by_id = {r["id"]: r for r in edited}
        for p in cfg["products"]:
            r = by_id.get(p["id"])
            if not r:
                continue
            # Watch is a product-level flag, not a trigger. Store True only
            # when set, so unmarked products stay clean (no "watch": false).
            if r.get("Watch"):
                p["watch"] = True
            else:
                p.pop("watch", None)
            t = p.setdefault("triggers", {})
            for key, col in [("buy_below_dkk", "BUY ≤ (DKK)"),
                             ("avoid_above_dkk", "AVOID ≥ (DKK)"),
                             ("cardmarket_buy_below_eur", "CM BUY ≤ (€)"),
                             ("cardmarket_stable_days", "CM stable days"),
                             ("not_before", "Not before")]:
                val = r.get(col)
                if val in (None, ""):
                    t.pop(key, None)
                else:
                    # data_editor makes every numeric cell a float; keep whole
                    # numbers as ints so "14.0 days" doesn't leak into reasons.
                    if isinstance(val, float) and val.is_integer():
                        val = int(val)
                    t[key] = val
        scanner.put_config(conn, cfg)
        flash("Triggers saved to watchlist.json")
        st.rerun()

    st.subheader("Manual flags")
    flags_changed = False
    for p in cfg["products"]:
        flag = p.get("triggers", {}).get("manual_buy_flag")
        if flag:
            current = p.get("flags", {}).get(flag, False)
            new = st.checkbox(f"{p['name']}: **{flag}** "
                              "(instant BUY signal when set)",
                              value=current, key=f"flag_{p['id']}_{flag}")
            if new != current:
                p.setdefault("flags", {})[flag] = new
                flags_changed = True
    if flags_changed:
        scanner.put_config(conn, cfg)
        flash("Flags saved")
        st.rerun()

    st.subheader("Settings")
    usd = st.number_input("USD/DKK", value=float(settings["usd_dkk"]),
                          step=0.01, format="%.2f")
    if usd != settings["usd_dkk"]:
        cfg["settings"]["usd_dkk"] = usd
        scanner.put_config(conn, cfg)
        st.success("USD/DKK saved")

    st.subheader("Full config (add/remove products & shops)")
    st.caption("The whole watchlist.json — edit and save. Invalid JSON is "
               "rejected, nothing is written. Your edits here are kept across "
               "other saves; press ↻ to reload the file if it changed.")
    # Seed the editor from session_state once, so a save elsewhere (triggers,
    # a flag toggle) doesn't silently wipe unsaved edits in this box.
    if "raw_cfg" not in st.session_state:
        st.session_state["raw_cfg"] = json.dumps(cfg, ensure_ascii=False,
                                                 indent=2)
    col_a, col_b, col_c = st.columns(3)
    with col_c:
        if st.button("↻ Reload from file"):
            st.session_state["raw_cfg"] = json.dumps(
                scanner.get_config(conn), ensure_ascii=False, indent=2)
            st.rerun()
    st.text_area("watchlist.json", height=400, key="raw_cfg")
    with col_a:
        if st.button("💾 Save full config"):
            try:
                parsed = json.loads(st.session_state["raw_cfg"])
                assert isinstance(parsed.get("products"), list), \
                    "'products' must be a list"
                for p in parsed["products"]:
                    assert p.get("id") and p.get("name"), \
                        "every product needs id and name"
            except (json.JSONDecodeError, AssertionError) as e:
                st.error(f"Not saved — invalid config: {e}")
            else:
                scanner.put_config(conn, parsed)
                flash("Config saved")
                st.rerun()
    with col_b:
        st.download_button("⬇ Backup watchlist.json",
                           data=json.dumps(cfg, ensure_ascii=False, indent=2),
                           file_name="watchlist-backup.json",
                           mime="application/json")

    # -- Report an issue ----------------------------------------------------
    st.subheader("Report an issue")
    st.caption("Spotted a bug or want a change? Send it — it's saved to the "
               "shared database for the maintainer.")
    with st.form("feedback_form", clear_on_submit=True):
        fb_msg = st.text_area("What happened / what would you like?",
                              height=100, key="fb_msg")
        if st.form_submit_button("Send report"):
            if fb_msg.strip():
                scanner.add_feedback(conn, person, fb_msg.strip(), APP_VERSION)
                flash("Thanks — your report was saved.")
                st.rerun()
            else:
                st.warning("Write a message first.")
    fb = scanner.list_feedback(conn)
    if fb:
        with st.expander(f"📮 Reported issues ({len(fb)})"):
            for f_ in fb:
                who = f_["person"] or "anon"
                when = (f_["created_at"] or "").replace("T", " ")
                st.markdown(f"- **{who}** · {when} · v{f_['app_version'] or '?'}"
                            f"\n\n    {f_['message']}")
