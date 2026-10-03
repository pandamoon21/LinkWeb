# AGENTS.md

Context for AI coding agents working on LinkWeb. Read this before changing
code. Keep it accurate: if a fact below stops being true, update it here.

## What this is

A self-hosted personal link dashboard. Flask + SQLite + one server-rendered
page. No build step, no JS framework, no bundler, no test suite. Total
surface is ~2000 lines across four files.

## Layout

| File | Role |
| --- | --- |
| `app.py` | All routes, DB access, backup/restore logic |
| `config.py` | Config loading (.env / config.yaml / env vars) |
| `templates/index.html` | The single page **and all the JavaScript** |
| `static/style.css` | Design tokens + all styles |
| `static/favicon.svg` | Tab icon |
| `requirements.txt` | Flask + gunicorn |

There is no separate JS file. Page scripts are inline in `index.html`.

## Running

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py          # dev server, debug on, port 6999, autoreload
```

Production runs under gunicorn via systemd:

```bash
gunicorn --workers 2 --bind 0.0.0.0:6999 app:app
```

There are no tests. Verify changes by running the app and hitting it with
`curl`, or by driving the page in a browser.

## Must-know constraints

These are easy to get wrong. Read them before editing.

**`init_db()` runs at import time**, at module level, not inside
`if __name__ == "__main__"`. That is deliberate: gunicorn imports `app`
without running the main block, and the DB must migrate on every boot.
Do not move it back into the main block.

**Schema changes must be additive.** `init_db()` inspects
`PRAGMA table_info(links)` and `ALTER TABLE ADD COLUMN` only for missing
columns. There is no migration framework. Never drop or rewrite a column,
and never assume a fresh database — real deployments have live data.

**Tags are a comma-separated string** in one `tags` column, not a join
table. Use the `normalize_tags()` / `tags_list()` helpers; do not split on
commas by hand, since `normalize_tags` also de-dupes, trims, and converts
semicolons.

**Auth is off unless both user and password are set.** `AUTH_ENABLED`
guards a `before_request` hook. Empty credentials must stay a supported
mode (local development). Do not make auth mandatory.

**Restore is destructive and needs its guards.** `/restore` and
`/restore-upload` replace `links.db`. Before writing, `restore_db()`
snapshots the current file as `*-prerestore.db` and `is_valid_db()` checks
the incoming file is SQLite with a `links` table. `/restore` must also keep
rejecting caller-supplied paths — it takes a basename and verifies the
parent is `BACKUP_DIR`. Do not relax either check.

**`/reorder` validates every id** against existing rows and returns 400 on
anything unknown. Favorites occupy the top block by count, so the first N
ids in the payload become favorites. Preserve that semantic if you touch
it.

**`/export` omits `id` on purpose** (name, url, favorite, position, tags
only). `/import` merges and skips rows whose `(name, url)` already exists.
Keep id out of the export so imports stay portable across installs.

## Frontend conventions

**CSS uses semantic tokens**, never raw hex, in component rules. Tokens
live in `:root` with a paired `[data-theme="light"]` block. If you add a
color, add it to both blocks or light mode breaks. Existing tokens:
`--bg --surface --surface-2 --surface-3 --border --border-strong --text
--text-muted --text-faint --accent --accent-hover --accent-press
--accent-soft --on-accent --danger --danger-soft --warn --radius*
--shadow-* --space-1..6 --ring --speed --ease`.

**Responsive is mobile-first with one breakpoint set**: base, then
`min-width: 380px`, `560px`, `900px`. There is exactly one `max-width`
rule (`379px`) for very narrow screens. Do not introduce overlapping
`min-width`/`max-width` at the same value, and do not re-introduce a
`display: grid` on `.toolbar` without resetting `grid-column` at the
`560px` step — that combination previously left buttons stretched and
orphaned on foldables.

**Theme is applied by an inline script in `<head>`**, before paint, reading
`localStorage["linkweb-theme"]`, so there is no flash. Element `id`s used
by JS: `clock`, `date`, `theme-toggle`, `search`, `tag-filter`,
`empty-filter`, `link-list`, `toast`, `backup-now`, `restore-toggle`,
`restore-panel`, `restore-file`, `import-file`.

**Keyboard shortcuts**: `/` focus search, `n` focus name field, `t` toggle
theme, `Esc` clear/close. They must stay disabled while an input is
focused.

**Drag-and-drop reordering** is custom pointer-event code at the bottom of
`index.html`. `items()` deliberately selects only
`.link-item:not(.filtered-out)` so a filtered list cannot scramble hidden
rows' positions. Keep that selector.

## Working on this repo

Match the surrounding style: small focused functions, comments that explain
*why* rather than *what*, no new dependencies without a strong reason.

When changing a feature, check whether `README.md` documents it — the
README is user-facing and is kept in sync with behaviour. Update it in the
same change.

If you regenerate the README screenshots in `docs/`, use neutral sample
data, never real bookmarks. `raw.githubusercontent.com` caches images
aggressively, so a stale screenshot there is usually CDN cache, not a bad
commit — confirm against the git blob before assuming the push failed.

## Deployment

Runs on a Proxmox LXC. Service is `linkweb.service` (systemd,
`Restart=always`, enabled), code at `/opt/linkweb`, app on port 6999.
Credentials come from `Environment=` in the unit file. Deploy by syncing
files and `systemctl restart linkweb`.
