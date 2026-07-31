# Deploying Kort og Godt for 3 people (shared, live data)

This puts the app online at one URL that all three of you open from any PC.
Everyone sees the **same live data** (scans, Cardmarket entries, collection),
and when you push a code change it **auto-updates** for everyone.

Architecture: **Streamlit Community Cloud** (runs the app, free) +
**Supabase Postgres** (the shared database, free). Local runs still work
unchanged — with no `DATABASE_URL` set, the app uses a local SQLite file.

You'll need to create three free accounts (GitHub, Supabase, Streamlit). I
can't create accounts for you, so those sign-up clicks are yours; everything
else is prepared.

---

## 1. Put the code on GitHub (once)

From this folder:

```bash
git init
git add .
git commit -m "Kort og Godt"
```

Then create a **private** repo on github.com and follow its "push existing
repository" lines, e.g.:

```bash
git remote add origin https://github.com/<you>/kort-og-godt.git
git branch -M main
git push -u origin main
```

`.gitignore` already excludes the local database and `secrets.toml`, so no
private data or passwords get pushed.

## 2. Create the shared database (Supabase)

1. Sign up at **supabase.com** → **New project** (pick a region near you, set a
   database password).
2. In the project: **Connect** (top bar) → **Connection string** → **URI**.
   Copy it. It looks like:
   `postgresql://postgres:YOURPASS@db.xxxx.supabase.co:5432/postgres`
3. Add `?sslmode=require` to the end. Keep this string safe — it's your
   `DATABASE_URL`.

The app creates its tables automatically on first connect — nothing to run.

## 3. Deploy on Streamlit Community Cloud

1. Sign up at **share.streamlit.io** with your GitHub account.
2. **Create app** → pick your repo, branch `main`, main file `app.py`.
3. **Advanced settings → Secrets**, paste (using your real values):

   ```toml
   DATABASE_URL = "postgresql://postgres:YOURPASS@db.xxxx.supabase.co:5432/postgres?sslmode=require"
   APP_PASSWORD = "a-shared-password-for-the-three-of-you"
   # Optional (v0.2) — Discord ping when a product newly flips to BUY:
   DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/XXXX/YYYY"
   ```

4. **Deploy**. In ~a minute you get a URL like
   `https://kort-og-godt.streamlit.app`.

Share the URL + the password with the other two. Done — same data for all.

## 3b. Optional: Discord BUY alerts (v0.2)

After any **SCAN**, products that *newly* crossed into **BUY** since the last
scan are posted to a Discord channel — no cron, it rides the scan. To enable:

1. In your Discord server: **Server Settings → Integrations → Webhooks → New
   Webhook**, pick the channel, **Copy Webhook URL**.
2. Add it as the `DISCORD_WEBHOOK_URL` secret (above). Leave it unset to keep
   alerts off. The URL is a secret — it is read from secrets/env only and is
   never stored in `watchlist.json`.

Only *newly*-BUY products are posted (the previous BUY set is remembered in the
shared DB), so re-scanning with no change stays quiet. Scheduled/background
alerts that fire without anyone opening the app are planned for v0.3.

## 4. Daily use

- All three open the URL, type the password, and use it normally.
- A **SCAN** by anyone updates prices for everyone (the 1-hour cache is shared
  too, so you don't hammer the shops).
- Cardmarket entries, trigger edits, and the collection are all shared.

## 5. "Live updating" the program

- **Data** is already live/shared through Postgres.
- **Code**: edit locally, then `git commit` + `git push`. Streamlit Cloud
  redeploys automatically within a minute and all three get the new version —
  no reinstall.

## Notes

- **Free tiers** comfortably cover 3 users. Supabase pauses a free project
  after ~1 week of zero activity; opening the app wakes it (first load may take
  a few seconds).
- **Seeding**: on the very first run the shared DB is seeded from
  `watchlist.json` and `collection.json` in the repo. After that the DB is the
  source of truth; edit via the app's Config/Collection tabs (or push new JSON
  and clear the `app_config` table to re-seed).
- **v0.2 upgrade**: no manual database step. On startup the app adds the new
  `observations.added_by` column to an existing shared DB automatically and
  idempotently, and creates the new `feedback` table — you just push the code.
- **Backup**: Supabase has its own backups; you can also use the app's
  *Export markdown report* and *Backup watchlist.json* buttons.
- **Local/offline** still works: just run `Start Kort og Godt.bat` with no
  `DATABASE_URL` — it uses a local SQLite file (not shared).
