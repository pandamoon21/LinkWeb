import hmac
import os
import pathlib
from flask import Flask, jsonify, render_template, request, redirect, url_for, Response
import sqlite3

app = Flask(__name__)

DB_PATH = "links.db"

# --- Basic auth -----------------------------------------------------------
# Set these via environment variables (e.g. in the systemd unit). If
# LINKWEB_USER / LINKWEB_PASSWORD are unset, auth is disabled so local
# development still works out of the box.
AUTH_USER = os.environ.get("LINKWEB_USER", "")
AUTH_PASSWORD = os.environ.get("LINKWEB_PASSWORD", "")
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


@app.route("/")
def index():
    with get_db() as conn:
        links = conn.execute(
            """
            SELECT * FROM links
            ORDER BY favorite DESC, position ASC, created_at DESC, id DESC
            """
        ).fetchall()
    return render_template("index.html", links=links)


@app.route("/add", methods=["POST"])
def add():
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    if name and url:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO links (name, url, position) VALUES (?, ?, (SELECT COALESCE(MIN(position), 1) - 1 FROM links))",
                (name, url),
            )
    return redirect(url_for("index"))


@app.route("/edit/<int:link_id>", methods=["POST"])
def edit(link_id):
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    if name and url:
        with get_db() as conn:
            conn.execute(
                "UPDATE links SET name = ?, url = ? WHERE id = ?",
                (name, url, link_id),
            )
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
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    extra = []
    for folder in ("templates", "static"):
        p = pathlib.Path(__file__).parent / folder
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
