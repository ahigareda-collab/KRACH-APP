#!/usr/bin/env python3
"""
KRACH Hockey Ratings
Hierarchy: Year -> Tournament -> Division -> Teams & Games
"""

import json
import os
import secrets
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL   = os.environ.get("DATABASE_URL")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

sessions = {}  # token -> role


# ── DB ────────────────────────────────────────────────────────────────────────

def get_conn():
    url = DATABASE_URL
    if "render.com" in (url or ""):
        return psycopg2.connect(url, sslmode="require")
    return psycopg2.connect(url)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS years (
                    id   SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tournaments (
                    id      SERIAL PRIMARY KEY,
                    year_id INTEGER NOT NULL REFERENCES years(id) ON DELETE CASCADE,
                    name    TEXT NOT NULL,
                    date    TEXT,
                    UNIQUE(year_id, name)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS divisions (
                    id            SERIAL PRIMARY KEY,
                    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                    name          TEXT NOT NULL,
                    tier          INTEGER DEFAULT NULL,
                    UNIQUE(tournament_id, name)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS teams (
                    id   SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS division_teams (
                    division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
                    team_name   TEXT NOT NULL REFERENCES teams(name) ON DELETE CASCADE,
                    PRIMARY KEY (division_id, team_name)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    id          SERIAL PRIMARY KEY,
                    division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
                    winner      TEXT NOT NULL,
                    loser       TEXT NOT NULL,
                    type        TEXT NOT NULL DEFAULT 'W',
                    phase       TEXT NOT NULL DEFAULT 'pool'
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            SERIAL PRIMARY KEY,
                    username      TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL DEFAULT 'editor'
                );
            """)
        conn.commit()


def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()


# ── Loaders ──────────────────────────────────────────────────────────────────

def load_years():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM years ORDER BY name DESC")
            return [dict(r) for r in cur.fetchall()]


def load_year(year_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM tournaments WHERE year_id=%s ORDER BY date, id", (year_id,))
            tournaments = []
            for t in cur.fetchall():
                cur2 = conn.cursor(cursor_factory=RealDictCursor)
                cur2.execute("SELECT id, name, tier FROM divisions WHERE tournament_id=%s ORDER BY tier NULLS LAST, name", (t["id"],))
                divs = []
                for d in cur2.fetchall():
                    cur3 = conn.cursor(cursor_factory=RealDictCursor)
                    cur3.execute("SELECT team_name FROM division_teams WHERE division_id=%s ORDER BY team_name", (d["id"],))
                    teams = [r["team_name"] for r in cur3.fetchall()]
                    cur3.execute("""
                        SELECT id, winner, loser, type, phase
                        FROM games WHERE division_id=%s ORDER BY id
                    """, (d["id"],))
                    games = [dict(r) for r in cur3.fetchall()]
                    divs.append({"id": d["id"], "name": d["name"], "tier": d["tier"],
                                 "teams": teams, "games": games})
                tournaments.append({"id": t["id"], "name": t["name"], "date": t.get("date"),
                                    "divisions": divs})
            return tournaments


def load_all_teams():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM teams ORDER BY name")
            return [r["name"] for r in cur.fetchall()]


def load_users():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, username, role FROM users ORDER BY id")
            return [dict(r) for r in cur.fetchall()]


def get_user(username):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE username=%s", (username,))
            return cur.fetchone()


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_session_token(headers):
    for part in headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith("session="):
            return part[len("session="):]
    return None


def is_authenticated(headers):
    t = get_session_token(headers)
    return t and t in sessions


def get_role(headers):
    t = get_session_token(headers)
    return sessions.get(t) if t else None


def is_superadmin(headers):
    return get_role(headers) == "superadmin"


def can_edit(headers):
    return get_role(headers) in ("superadmin", "editor")


# ── Handler ───────────────────────────────────────────────────────────────────

class KRACHHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200, extra_headers=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ct):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def require_auth(self):
        if not is_authenticated(self.headers):
            self.send_json({"error": "Unauthorized"}, 401)
            return False
        return True

    def require_edit(self):
        if not self.require_auth():
            return False
        if not can_edit(self.headers):
            self.send_json({"error": "Permission denied"}, 403)
            return False
        return True

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        # Public
        if path == "/standings":
            self.send_file("standings.html", "text/html"); return
        if path == "/login":
            self.send_file("login.html", "text/html"); return
        if path == "/manifest.json":
            self.send_file("manifest.json", "application/manifest+json"); return
        if path == "/standings-manifest.json":
            self.send_file("standings-manifest.json", "application/manifest+json"); return
        if path == "/icon-192.png":
            self.send_file("icon-192.png", "image/png"); return
        if path == "/icon-512.png":
            self.send_file("icon-512.png", "image/png"); return

        # Public API
        if path == "/api/years":
            self.send_json(load_years()); return
        if path == "/api/year":
            yid = params.get("year_id", [None])[0]
            if not yid:
                self.send_json({"error": "year_id required"}, 400); return
            self.send_json(load_year(int(yid))); return
        if path == "/api/teams":
            self.send_json(load_all_teams()); return

        # Auth required
        if path == "/" or path == "/index.html":
            if not is_authenticated(self.headers):
                self.redirect("/login"); return
            self.send_file("index.html", "text/html"); return
        if path == "/api/me":
            self.send_json({"role": get_role(self.headers) or "none"}); return
        if path == "/api/users":
            if not is_superadmin(self.headers):
                self.send_json({"error": "Unauthorized"}, 401); return
            self.send_json(load_users()); return

        self.send_response(404); self.end_headers()

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        # Login
        if path == "/api/auth/login":
            username = body.get("username", "").strip()
            password = body.get("password", "")
            role = None
            if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                role = "superadmin"
            else:
                u = get_user(username)
                if u and u["password_hash"] == hash_password(password):
                    role = u["role"]
            if role:
                token = secrets.token_hex(32)
                sessions[token] = role
                self.send_json({"ok": True, "role": role}, extra_headers={
                    "Set-Cookie": f"session={token}; Path=/; HttpOnly; SameSite=Strict"
                })
            else:
                self.send_json({"error": "Invalid username or password"}, 401)
            return

        # Users (superadmin only)
        if path == "/api/users":
            if not is_superadmin(self.headers):
                self.send_json({"error": "Unauthorized"}, 401); return
            uname = body.get("username", "").strip()
            pwd   = body.get("password", "")
            role  = body.get("role", "editor")
            if not uname or not pwd:
                self.send_json({"error": "Username and password required"}, 400); return
            if role not in ("editor", "viewer"):
                self.send_json({"error": "Invalid role"}, 400); return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)",
                                    (uname, hash_password(pwd), role))
                    conn.commit()
                self.send_json({"ok": True, "users": load_users()})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Username already exists"}, 400)
            return

        if not self.require_edit():
            return

        # Years
        if path == "/api/years":
            name = body.get("name", "").strip()
            if not name:
                self.send_json({"error": "Name required"}, 400); return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO years (name) VALUES (%s) RETURNING id", (name,))
                        new_id = cur.fetchone()[0]
                    conn.commit()
                self.send_json({"ok": True, "id": new_id, "name": name})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Year already exists"}, 400)

        # Tournaments
        elif path == "/api/tournaments":
            year_id = body.get("year_id")
            name    = body.get("name", "").strip()
            date    = body.get("date", None)
            if not year_id or not name:
                self.send_json({"error": "year_id and name required"}, 400); return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO tournaments (year_id, name, date) VALUES (%s,%s,%s) RETURNING id",
                                    (year_id, name, date))
                        new_id = cur.fetchone()[0]
                    conn.commit()
                self.send_json({"ok": True, "id": new_id, "tournaments": load_year(year_id)})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Tournament already exists in this year"}, 400)

        # Divisions
        elif path == "/api/divisions":
            tourn_id = body.get("tournament_id")
            name     = body.get("name", "").strip()
            tier     = body.get("tier", None)
            if not tourn_id or not name:
                self.send_json({"error": "tournament_id and name required"}, 400); return
            if tier is not None:
                try: tier = int(tier)
                except: tier = None
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO divisions (tournament_id, name, tier) VALUES (%s,%s,%s) RETURNING id",
                                    (tourn_id, name, tier))
                        new_id = cur.fetchone()[0]
                    conn.commit()
                self.send_json({"ok": True, "id": new_id})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Division already exists in this tournament"}, 400)

        # Teams (global)
        elif path == "/api/teams":
            name = body.get("name", "").strip()
            if not name:
                self.send_json({"error": "Name required"}, 400); return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO teams (name) VALUES (%s)", (name,))
                    conn.commit()
                self.send_json({"ok": True, "teams": load_all_teams()})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Team already exists"}, 400)

        # Assign team to division
        elif path == "/api/division-teams":
            div_id    = body.get("division_id")
            team_name = body.get("team_name", "").strip()
            if not div_id or not team_name:
                self.send_json({"error": "division_id and team_name required"}, 400); return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO division_teams (division_id, team_name) VALUES (%s,%s)",
                                    (div_id, team_name))
                    conn.commit()
                self.send_json({"ok": True})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Team already in this division"}, 400)

        # Games
        elif path == "/api/games":
            div_id = body.get("division_id")
            winner = body.get("winner")
            loser  = body.get("loser")
            rtype  = body.get("type", "W")
            phase  = body.get("phase", "pool")
            if not div_id or not winner or not loser or winner == loser:
                self.send_json({"error": "Invalid game data"}, 400); return
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO games (division_id, winner, loser, type, phase) VALUES (%s,%s,%s,%s,%s)",
                                (div_id, winner, loser, rtype, phase))
                conn.commit()
            self.send_json({"ok": True})

        else:
            self.send_response(404); self.end_headers()

    # ── DELETE ───────────────────────────────────────────────────────────────

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/auth/logout":
            t = get_session_token(self.headers)
            if t in sessions: del sessions[t]
            self.send_json({"ok": True}, extra_headers={
                "Set-Cookie": "session=; Path=/; HttpOnly; Max-Age=0"
            }); return

        if path == "/api/users":
            if not is_superadmin(self.headers):
                self.send_json({"error": "Unauthorized"}, 401); return
            uid = body.get("id")
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM users WHERE id=%s", (uid,))
                conn.commit()
            self.send_json({"ok": True, "users": load_users()}); return

        if not self.require_edit():
            return

        if path == "/api/years":
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM years WHERE id=%s", (body.get("id"),))
                conn.commit()
            self.send_json({"ok": True})

        elif path == "/api/tournaments":
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM tournaments WHERE id=%s", (body.get("id"),))
                conn.commit()
            self.send_json({"ok": True})

        elif path == "/api/divisions":
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM divisions WHERE id=%s", (body.get("id"),))
                conn.commit()
            self.send_json({"ok": True})

        elif path == "/api/teams":
            name = body.get("name")
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM teams WHERE name=%s", (name,))
                conn.commit()
            self.send_json({"ok": True, "teams": load_all_teams()})

        elif path == "/api/division-teams":
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM division_teams WHERE division_id=%s AND team_name=%s",
                                (body.get("division_id"), body.get("team_name")))
                conn.commit()
            self.send_json({"ok": True})

        elif path == "/api/games":
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM games WHERE id=%s", (body.get("id"),))
                conn.commit()
            self.send_json({"ok": True})

        else:
            self.send_response(404); self.end_headers()


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), KRACHHandler)
    print(f"Server running on port {port}")
    server.serve_forever()
