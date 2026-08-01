"""Headless scheduled scan — the GitHub Actions cron entry point.

Runs one full scan cycle against the shared database and (when configured)
pings Discord with newly-flipped BUYs. Same read-only, polite behavior as a
manual SCAN in the app; see scanner.run_headless for the exact semantics.

Environment:
  DATABASE_URL         shared Postgres (unset -> local SQLite, useful for dev)
  DISCORD_WEBHOOK_URL  optional; unset -> scan + snapshot only, no diff/ping

Exit code: 0 on a completed scan; an uncaught scan-level exception exits
non-zero so a broken run shows red in the Actions UI.
"""
from __future__ import annotations

import os
import sys

import scanner


def main() -> int:
    conn = scanner.get_db()
    try:
        def progress(product_name, shop, obs):
            if obs.status == "manual":
                return
            mark = {"ok": "OK  ", "skipped": "SKIP"}.get(obs.status, "FAIL")
            detail = (scanner.fmt_dkk(obs.landed_dkk)
                      if obs.status == "ok" and obs.landed_dkk is not None
                      else (obs.error or ""))
            print(f"{mark} {shop:22s} {product_name[:40]:40s} {detail}")

        summary = scanner.run_headless(
            conn, os.environ.get("DISCORD_WEBHOOK_URL") or None, progress,
            # GitHub-hosted runners sit outside Denmark; geo-priced shops
            # (requires_dk_ip sources) would serve foreign-currency prices.
            non_dk_vantage=bool(os.environ.get("GITHUB_ACTIONS")))
    finally:
        conn.close()

    print(f"\nScan #{summary['scan_id']}: {summary['n_ok']}/"
          f"{summary['n_sources']} sources OK, "
          f"{summary['n_failed']} failed")
    print("Verdicts:", ", ".join(
        f"{c}={n}" for c, n in summary["verdict_counts"].items() if n))
    if summary["new_buy_ids"] is None:
        print("Discord: skipped (DISCORD_WEBHOOK_URL not set) — "
              "BUY-diff state left for the app")
    elif summary["new_buy_ids"]:
        print(f"Discord: pinged {len(summary['new_buy_ids'])} new BUY(s): "
              + ", ".join(summary["new_buy_ids"]))
    else:
        print("Discord: no newly-flipped BUYs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
