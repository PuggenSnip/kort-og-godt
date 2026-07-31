"""Kort og Godt — TCG sealed-product price scanner (Streamlit UI).

Run with:  streamlit run app.py
Read-only tool: fetches prices, compares against triggers, stores history.
It never purchases anything.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import scanner

APP_VERSION = "0.2"      # beta — bump this as the app matures

# Use the trading-card logo as the browser-tab icon (fallback to an emoji).
try:
    from PIL import Image as _PILImage
    _icon_file = Path(__file__).resolve().parent / "kort_og_godt.png"
    _PAGE_ICON = _PILImage.open(_icon_file) if _icon_file.exists() else "🃏"
except Exception:       # noqa: BLE001 — branding must never block startup
    _PAGE_ICON = "🃏"

st.set_page_config(page_title="Kort og Godt", page_icon=_PAGE_ICON,
                   layout="wide")

st.markdown(
    """
    <style>
    div[data-testid="stButton"] > button[kind="primary"] {
        font-size: 1.6rem; font-weight: 700; padding: 0.9rem 3rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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


st.title("🃏 Kort og Godt")
st.markdown(
    f"<div style='color:#B8912E;font-size:0.8rem;font-weight:600;"
    f"margin-top:-10px;margin-bottom:4px'>v{APP_VERSION} · beta</div>",
    unsafe_allow_html=True)
st.caption(
    "Local TCG sealed-price scanner — read-only, polite (1 req/s/domain, "
    "1 h cache, robots.txt respected). eBay is skipped in v1: needs API keys, "
    "and non-EU offers are uncompetitive after +25% Danish VAT + ~160 kr fee. "
    "Cardmarket is never scraped — enter its price manually below."
)

if "_flash" in st.session_state:
    st.success(st.session_state.pop("_flash"))

tab_scan, tab_collection, tab_config = st.tabs(
    ["📊 Scan", "🎴 Collection", "⚙️ Config"])


# ---------------------------------------------------------------------------
# Scan tab
# ---------------------------------------------------------------------------

with tab_scan:
    col_btn, col_info = st.columns([1, 2])
    with col_btn:
        scan_clicked = st.button("🔍  SCAN", type="primary",
                                 use_container_width=True)
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
            "Cheapest (landed)": scanner.fmt_dkk(v.cheapest_dkk),
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
    mc = st.columns(5)
    mc[0].metric("🟢 BUY", counts.get("BUY", 0))
    mc[1].metric("🟡 Falling", counts.get("WAIT_FALLING", 0))
    mc[2].metric("🔴 AVOID", counts.get("AVOID", 0))
    mc[3].metric("⚪ HOLD", counts.get("HOLD", 0))
    mc[4].metric("⚫ Unverified", counts.get("UNVERIFIED", 0))

    buys = [(p["name"], verdicts[p["id"]]) for p in cfg["products"]
            if verdicts[p["id"]].code == "BUY"]
    if buys:
        st.success("**Act now — BUY:**\n\n" + "\n".join(
            f"- **{name}** — {v.reason}"
            + (f"  ·  [open shop]({v.url})" if v.url else "")
            for name, v in buys))

    # Discord ping: after a successful scan, post the products that flipped INTO
    # BUY since the previous scan. No cron — it rides this SCAN. The webhook is a
    # secret (DISCORD_WEBHOOK_URL); if unset, notify_new_buys just tracks state.
    if scan_succeeded:
        current_buys = [{"id": p["id"], "name": p["name"],
                         "reason": verdicts[p["id"]].reason,
                         "url": verdicts[p["id"]].url}
                        for p in cfg["products"]
                        if verdicts[p["id"]].code == "BUY"]
        webhook = _secret("DISCORD_WEBHOOK_URL")
        new_ids = scanner.notify_new_buys(conn, webhook, current_buys)
        if new_ids and webhook:
            st.toast(f"Discord: pinged {len(new_ids)} new BUY(s)")

    st.dataframe(
        rows, use_container_width=True, hide_index=True,
        column_config={"Link": st.column_config.LinkColumn(
            "Link", display_text="open shop")},
    )

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
                        price = scanner.fmt_dkk(r["landed_dkk"])
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
                st.dataframe(src_rows, use_container_width=True,
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
            # honest signal, not the EUR_DKK-pegged DKK).
            cm_hist = scanner.cardmarket_series(conn, product["id"])
            if len(cm_hist) >= 2:
                st.caption("Cardmarket manual entries (€)")
                st.line_chart(
                    {"Cardmarket €":
                     {t[:16].replace("T", " "): p for t, p in cm_hist}},
                    height=160)

            # Cardmarket manual entry.
            cm = product.get("cardmarket") or {}
            if cm.get("url"):
                cm_col1, cm_col2, cm_col3 = st.columns([1, 1, 2])
                with cm_col1:
                    st.link_button("🔗 Check Cardmarket", cm["url"])
                with cm_col2:
                    eur = st.number_input(
                        "Today's lowest (€)", min_value=0.0, step=1.0,
                        value=None, key=f"cm_{product['id']}",
                        help="Paste today's lowest Cardmarket price. Stored "
                             "like scraped data (EUR × 7.46 → DKK).")
                with cm_col3:
                    # No `disabled=` (a disabled button swallows the click that
                    # commits the number_input) — validate inside the handler.
                    if st.button("Save Cardmarket price",
                                 key=f"cm_save_{product['id']}"):
                        if eur is None or eur <= 0:
                            st.warning("Enter a price above 0 first.")
                        else:
                            scanner.add_manual_cardmarket_entry(
                                conn, product, float(eur), settings,
                                added_by=person)
                            flash(f"Saved Cardmarket €{eur:g} for "
                                  f"{product['name']} "
                                  f"({scanner.fmt_dkk(eur * scanner.EUR_DKK)})")
                            st.rerun()
                    cm_last = conn.execute(
                        "SELECT price_native, observed_at, added_by "
                        "FROM observations "
                        "WHERE product_id = :pid AND source_kind = 'manual' "
                        "ORDER BY id DESC LIMIT 1",
                        {"pid": product["id"]}).fetchone()
                    if cm_last:
                        by = (f" by {cm_last['added_by']}"
                              if cm_last.get("added_by") else "")
                        st.caption(
                            f"Last entry: €{cm_last['price_native']:g} on "
                            f"{cm_last['observed_at'].replace('T', ' ')}{by}")


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
        use_container_width=True, key="holdings_editor",
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

        # Per-person P/L breakdown (shared deployments). Shown when the data is
        # actually attributed — a single "(unknown)" bucket adds no signal.
        bp = val["by_person"]
        if len(bp) > 1 or (bp and "(unknown)" not in bp):
            prows = []
            for name in sorted(bp):
                b = bp[name]
                ppct = (b["unrealized_pl"] / b["cost"] * 100) if b["cost"] else None
                prows.append({
                    "Person": name,
                    "Items": b["n_items"],
                    "Market value": scanner.fmt_dkk(b["market_value"], 0),
                    "Cost basis": scanner.fmt_dkk(b["cost"], 0),
                    "Unrealized P/L": scanner.fmt_dkk(b["unrealized_pl"], 0),
                    "U-ROI": (f"{ppct:+.1f}%" if ppct is not None else "–"),
                    "Sold": b["n_sold"],
                    "Realized P/L": scanner.fmt_dkk(b["realized_pl"], 0),
                })
            st.markdown("**By person**")
            st.dataframe(prows, use_container_width=True, hide_index=True)

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
        st.dataframe(hrows, use_container_width=True, hide_index=True)

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
        st.dataframe(srows, use_container_width=True, hide_index=True)

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
            st.markdown("**Realized by year**")
            st.dataframe(yrows, use_container_width=True, hide_index=True)
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
    st.subheader("Triggers")
    st.caption("Edit and press **Save triggers**. Empty = no trigger. "
               f"EUR/DKK is hardcoded at {scanner.EUR_DKK} (pegged).")
    trig_rows = []
    for p in cfg["products"]:
        t = p.get("triggers", {})
        trig_rows.append({
            "id": p["id"],
            "Product": p["name"],
            "BUY ≤ (DKK)": t.get("buy_below_dkk"),
            "AVOID ≥ (DKK)": t.get("avoid_above_dkk"),
            "CM BUY ≤ (€)": t.get("cardmarket_buy_below_eur"),
            "CM stable days": t.get("cardmarket_stable_days"),
            "Not before": t.get("not_before"),
        })
    edited = st.data_editor(trig_rows, hide_index=True,
                            use_container_width=True,
                            disabled=["id", "Product"], key="trig_editor")
    if st.button("💾 Save triggers"):
        by_id = {r["id"]: r for r in edited}
        for p in cfg["products"]:
            r = by_id.get(p["id"])
            if not r:
                continue
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
