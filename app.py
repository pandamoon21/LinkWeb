import hmac
import json
import os
import pathlib
import shutil
import sqlite3
import time
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from config import CONFIG

app = Flask(__name__)

BASE_DIR = pathlib.Path(__file__).parent
DB_PATH = CONFIG["db_path"]
BACKUP_DIR = pathlib.Path(CONFIG["backup_dir"])
BACKUP_KEEP = CONFIG["backup_keep"]

# --- Basic auth -----------------------------------------------------------
# Credentials come from config.py, which merges (highest first) real
# environment variables, a .env file, and config.yaml. Leave both empty to
# disable auth entirely for local development.
AUTH_USER = CONFIG["user"]
AUTH_PASSWORD = CONFIG["password"]
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)


def _check_auth(auth):
    return hmac.compare_digest(auth.username, AUTH_USER) and hmac.compare_digest(
        auth.password, AUTH_PASSWORD
    )


@app.before_request
def require_auth():
    if not AUTH_ENABLED:
        return None
    auth = request.authorization
    if auth is None or not _check_auth(auth):
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="LinkWeb"'},
        )
    return None


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --- Tags helpers ---------------------------------------------------------
# Tags are stored as a comma separated string on each row. Keeping it in a
# single column avoids a join table and keeps the existing queries intact.
def normalize_tags(raw):
    seen = []
    for part in (raw or "").replace(";", ",").split(","):
        tag = part.strip()
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen)


def tags_list(value):
    return [t for t in (value or "").split(",") if t]


# --- Backup ---------------------------------------------------------------
def backup_db(reason="auto"):
    """Copy links.db into backups/ and keep only the newest BACKUP_KEEP."""
    if not pathlib.Path(DB_PATH).exists():
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"links-{stamp}-{reason}.db"
    # Flush any pending WAL/journal writes before copying.
    with get_db() as conn:
        conn.execute("VACUUM")
    shutil.copy2(DB_PATH, dest)
    backups = sorted(BACKUP_DIR.glob("links-*.db"), reverse=True)
    for stale in backups[BACKUP_KEEP:]:
        stale.unlink()
    return dest


def list_backups():
    if not BACKUP_DIR.is_dir():
        return []
    return sorted((p.name for p in BACKUP_DIR.glob("links-*.db")), reverse=True)


def is_valid_db(path):
    """A usable LinkWeb database must be a SQLite file with a links table."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return "links" in names


def restore_db(source):
    """Replace links.db with source, snapshotting the current file first."""
    if not is_valid_db(source):
        return False
    backup_db("prerestore")
    shutil.copy2(source, DB_PATH)
    init_db()
    return True


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(links)")}
        if "favorite" not in cols:
            conn.execute("ALTER TABLE links ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
        if "position" not in cols:
            conn.execute("ALTER TABLE links ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
            # Backfill so the existing display order (newest first) is preserved.
            conn.execute(
                """
                UPDATE links SET position = (
                    SELECT COUNT(*) FROM links l2
                    WHERE l2.created_at > links.created_at
                       OR (l2.created_at = links.created_at AND l2.id > links.id)
                )
                """
            )
        if "tags" not in cols:
            conn.execute("ALTER TABLE links ADD COLUMN tags TEXT NOT NULL DEFAULT ''")


def favicon_url(url):
    """Best-effort favicon for a link, served through a public resolver."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        host = None
    if not host:
        return ""
    return f"https://www.google.com/s2/favicons?domain={host}&sz=64"


@app.route("/")
def index():
    with get_db() as conn:
        links = conn.execute(
            """
            SELECT * FROM links
            ORDER BY favorite DESC, position ASC, created_at DESC, id DESC
            """
        ).fetchall()
    all_tags = sorted({t for link in links for t in tags_list(link["tags"])})
    return render_template(
        "index.html",
        links=links,
        all_tags=all_tags,
        favicon_url=favicon_url,
        tags_list=tags_list,
        backups=list_backups(),
    )


@app.route("/add", methods=["POST"])
def add():
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    tags = normalize_tags(request.form.get("tags", ""))
    if name and url:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO links (name, url, position, tags) "
                "VALUES (?, ?, (SELECT COALESCE(MIN(position), 1) - 1 FROM links), ?)",
                (name, url, tags),
            )
        backup_db("add")
    return redirect(url_for("index"))


@app.route("/edit/<int:link_id>", methods=["POST"])
def edit(link_id):
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    tags = normalize_tags(request.form.get("tags", ""))
    if name and url:
        with get_db() as conn:
            conn.execute(
                "UPDATE links SET name = ?, url = ?, tags = ? WHERE id = ?",
                (name, url, tags, link_id),
            )
        backup_db("edit")
    return redirect(url_for("index"))


@app.route("/favorite/<int:link_id>", methods=["POST"])
def favorite(link_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE links SET favorite = 1 - favorite WHERE id = ?",
            (link_id,),
        )
    return redirect(url_for("index"))


@app.route("/reorder", methods=["POST"])
def reorder():
    ids = request.get_json(silent=True)
    if not isinstance(ids, list):
        return jsonify(ok=False), 400

    with get_db() as conn:
        existing = [r["id"] for r in conn.execute("SELECT id FROM links")]
        known = set(existing)
        if any(not isinstance(i, int) or i not in known for i in ids):
            return jsonify(ok=False), 400

        # Links missing from the payload keep their relative order at the end.
        order = ids + [i for i in existing if i not in set(ids)]

        # Favorites always occupy the top block, so the first N items in the
        # new order become the favorites.
        favorite_count = conn.execute(
            "SELECT COUNT(*) AS c FROM links WHERE favorite = 1"
        ).fetchone()["c"]

        for index, link_id in enumerate(order):
            conn.execute(
                "UPDATE links SET position = ?, favorite = ? WHERE id = ?",
                (index, 1 if index < favorite_count else 0, link_id),
            )
    return jsonify(ok=True)


@app.route("/delete/<int:link_id>", methods=["POST"])
def delete(link_id):
    with get_db() as conn:
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
    backup_db("delete")
    return redirect(url_for("index"))


@app.route("/export")
def export():
    """Download every link as a JSON document."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name, url, favorite, position, tags FROM links "
            "ORDER BY favorite DESC, position ASC, id ASC"
        ).fetchall()
    payload = {
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "links": [dict(r) for r in rows],
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=links.json"},
    )


@app.route("/import", methods=["POST"])
def import_links():
    """Import links from an uploaded JSON export (merge by name+url)."""
    upload = request.files.get("file")
    raw = upload.read().decode("utf-8") if upload else request.form.get("json", "")
    try:
        data = json.loads(raw or "{}")
    except (ValueError, UnicodeDecodeError):
        return jsonify(ok=False, error="invalid json"), 400

    items = data.get("links") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return jsonify(ok=False, error="no links array"), 400

    added = 0
    with get_db() as conn:
        existing = {(r["name"], r["url"]) for r in conn.execute("SELECT name, url FROM links")}
        next_pos = conn.execute("SELECT COALESCE(MIN(position), 1) FROM links").fetchone()[0]
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            if not name or not url or (name, url) in existing:
                continue
            conn.execute(
                "INSERT INTO links (name, url, favorite, position, tags) VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    url,
                    1 if item.get("favorite") else 0,
                    next_pos - 1,
                    normalize_tags(item.get("tags", "")),
                ),
            )
            existing.add((name, url))
            next_pos -= 1
            added += 1
    if added:
        backup_db("import")
    return jsonify(ok=True, added=added), 200


@app.route("/backup", methods=["POST"])
def backup_now():
    """Trigger a manual backup from the UI."""
    dest = backup_db("manual")
    return jsonify(ok=bool(dest), backups=list_backups())


@app.route("/restore", methods=["POST"])
def restore_now():
    """Restore links.db from a snapshot in backups/."""
    name = request.form.get("name", "")
    # Only ever touch files inside backups/, never a caller-supplied path.
    candidate = BACKUP_DIR / pathlib.Path(name).name
    if not name or candidate.parent != BACKUP_DIR or not candidate.is_file():
        return jsonify(ok=False, error="unknown backup"), 400
    if not restore_db(candidate):
        return jsonify(ok=False, error="not a valid database"), 400
    backup_db("restored")
    return jsonify(ok=True, restored=candidate.name, backups=list_backups())


@app.route("/restore-upload", methods=["POST"])
def restore_upload():
    """Restore links.db from an uploaded .db file."""
    upload = request.files.get("file")
    if upload is None:
        return jsonify(ok=False, error="no file"), 400
    BACKUP_DIR.mkdir(exist_ok=True)
    tmp = BACKUP_DIR / "upload-incoming.db"
    upload.save(tmp)
    if not restore_db(tmp):
        tmp.unlink(missing_ok=True)
        return jsonify(ok=False, error="not a valid database"), 400
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp.replace(BACKUP_DIR / f"links-{stamp}-uploaded.db")
    backup_db("restored")
    return jsonify(ok=True, backups=list_backups())


@app.route("/backups")
def backups_json():
    """List available snapshots for the restore picker."""
    return jsonify(ok=True, backups=list_backups())


init_db()

if __name__ == "__main__":
    extra = []
    for folder in ("templates", "static"):
        p = BASE_DIR / folder
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    extra.append(str(f))

    app.run(
        debug=True,
        host="0.0.0.0",
        port=6999,
        extra_files=extra,
        exclude_patterns=["*.db"],
    )
