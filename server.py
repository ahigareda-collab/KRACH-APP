#!/usr/bin/env python3
"""
KRACH Hockey Ratings - Render Web Server with PostgreSQL + Tournaments
"""

import json
import os
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

sessions = {}


def get_conn():
    url = DATABASE_URL
    if "render.com" in (url or ""):
        return psycopg2.connect(url, sslmode="require")
    return psycopg2.connect(url)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS divisions (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    tier INTEGER DEFAULT NULL
                );
            """)
            cur.execute("""
                ALTER TABLE divisions ADD COLUMN IF NOT EXISTS tier INTEGER DEFAULT NULL;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS teams (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
                    UNIQUE(name, division_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tournaments (
                    id SERIAL PRIMARY KEY,
                    division_id INTEGER REFERENCES divisions(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    date TEXT,
                    cross_tier BOOLEAN NOT NULL DEFAULT FALSE,
                    tier_a INTEGER DEFAULT NULL,
                    tier_b INTEGER DEFAULT NULL
                );
            """)
            cur.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS cross_tier BOOLEAN NOT NULL DEFAULT FALSE;")
            cur.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS tier_a INTEGER DEFAULT NULL;")
            cur.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS tier_b INTEGER DEFAULT NULL;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    id SERIAL PRIMARY KEY,
                    division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
                    tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
                    winner TEXT NOT NULL,
                    loser TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'W',
                    phase TEXT NOT NULL DEFAULT 'pool',
                    winner_division_id INTEGER REFERENCES divisions(id) ON DELETE SET NULL,
                    loser_division_id INTEGER REFERENCES divisions(id) ON DELETE SET NULL,
                    cross_tier BOOLEAN NOT NULL DEFAULT FALSE
                );
            """)
            cur.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS winner_division_id INTEGER REFERENCES divisions(id) ON DELETE SET NULL;")
            cur.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS loser_division_id INTEGER REFERENCES divisions(id) ON DELETE SET NULL;")
            cur.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS cross_tier BOOLEAN NOT NULL DEFAULT FALSE;")
        conn.commit()


def load_divisions():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, tier FROM divisions ORDER BY tier NULLS LAST, name")
            return [{"id": row["id"], "name": row["name"], "tier": row["tier"]} for row in cur.fetchall()]


def load_teams_for_tiers(tier_a, tier_b):
    """Load all teams from divisions matching tier_a or tier_b."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT t.name, t.division_id, d.name as division_name, d.tier
                FROM teams t
                JOIN divisions d ON t.division_id = d.id
                WHERE d.tier IN %s
                ORDER BY d.tier, t.name
            """, ((tier_a, tier_b),))
            return [{"name": r["name"], "division_id": r["division_id"],
                    "division_name": r["division_name"], "tier": r["tier"]} for r in cur.fetchall()]


def load_division_data(division_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM teams WHERE division_id = %s ORDER BY id", (division_id,))
            teams = [row["name"] for row in cur.fetchall()]
            cur.execute("""
                SELECT id, name, date, cross_tier, tier_a, tier_b
                FROM tournaments
                WHERE division_id = %s OR (cross_tier = TRUE AND (tier_a IN (
                    SELECT tier FROM divisions WHERE id = %s
                ) OR tier_b IN (
                    SELECT tier FROM divisions WHERE id = %s
                )))
                ORDER BY date, id
            """, (division_id, division_id, division_id))
            tournaments = [{"id": r["id"], "name": r["name"], "date": r["date"],
                           "cross_tier": r["cross_tier"], "tier_a": r["tier_a"], "tier_b": r["tier_b"]} for r in cur.fetchall()]
            cur.execute("""
                SELECT g.id, g.tournament_id, g.winner, g.loser, g.type, g.phase,
                       g.cross_tier, g.winner_division_id, g.loser_division_id,
                       t.name as tournament_name
                FROM games g
                LEFT JOIN tournaments t ON g.tournament_id = t.id
                WHERE g.division_id = %s
                   OR g.winner_division_id = %s
                   OR g.loser_division_id = %s
                ORDER BY g.id
            """, (division_id, division_id, division_id))
            games = [{
                "id": r["id"], "tournament_id": r["tournament_id"],
                "tournament_name": r["tournament_name"],
                "winner": r["winner"], "loser": r["loser"],
                "type": r["type"], "phase": r["phase"],
                "cross_tier": r["cross_tier"],
                "winner_division_id": r["winner_division_id"],
                "loser_division_id": r["loser_division_id"]
            } for r in cur.fetchall()]
    return {"teams": teams, "tournaments": tournaments, "games": games}


def get_session_token(headers):
    cookie = headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("session="):
            return part[len("session="):]
    return None


def is_authenticated(headers):
    token = get_session_token(headers)
    return token is not None and token in sessions


class KRACHHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
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

    def send_file(self, path, content_type):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def require_auth(self):
        if not is_authenticated(self.headers):
            self.send_json({"error": "Unauthorized"}, 401)
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/standings":
            self.send_file("standings.html", "text/html")
            return
        if path == "/login":
            self.send_file("login.html", "text/html")
            return
        if path == "/api/divisions":
            self.send_json(load_divisions())
            return
        if path == "/api/data":
            div_id = params.get("division_id", [None])[0]
            if not div_id:
                self.send_json({"error": "division_id required"}, 400)
                return
            self.send_json(load_division_data(int(div_id)))
            return
        if path == "/" or path == "/index.html":
            if not is_authenticated(self.headers):
                self.redirect("/login")
                return
            self.send_file("index.html", "text/html")
            return
        if path == "/api/cross-tier-teams":
            tier_a = params.get("tier_a", [None])[0]
            tier_b = params.get("tier_b", [None])[0]
            if not tier_a or not tier_b:
                self.send_json({"error": "tier_a and tier_b required"}, 400)
                return
            self.send_json(load_teams_for_tiers(int(tier_a), int(tier_b)))
            return
        if path == "/api/auth/check":
            self.send_json({"authenticated": is_authenticated(self.headers)})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/auth/login":
            username = body.get("username", "").strip()
            password = body.get("password", "")
            if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                token = secrets.token_hex(32)
                sessions[token] = True
                self.send_json({"ok": True}, extra_headers={
                    "Set-Cookie": f"session={token}; Path=/; HttpOnly; SameSite=Strict"
                })
            else:
                self.send_json({"error": "Invalid username or password"}, 401)
            return

        if not self.require_auth():
            return

        if path == "/api/divisions":
            name = body.get("name", "").strip()
            if not name:
                self.send_json({"error": "Division name required"}, 400)
                return
            tier = body.get("tier", None)
            if tier is not None:
                try: tier = int(tier)
                except: tier = None
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO divisions (name, tier) VALUES (%s, %s) RETURNING id", (name, tier))
                        new_id = cur.fetchone()[0]
                    conn.commit()
                self.send_json({"ok": True, "id": new_id, "name": name, "tier": tier})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Division already exists"}, 400)

        elif path == "/api/teams":
            name = body.get("name", "").strip()
            div_id = body.get("division_id")
            if not name or not div_id:
                self.send_json({"error": "Name and division_id required"}, 400)
                return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO teams (name, division_id) VALUES (%s, %s)", (name, div_id))
                    conn.commit()
                self.send_json({"ok": True, **load_division_data(div_id)})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Team already exists in this division"}, 400)

        elif path == "/api/tournaments":
            div_id = body.get("division_id")
            name = body.get("name", "").strip()
            date = body.get("date", None)
            cross_tier = bool(body.get("cross_tier", False))
            tier_a = body.get("tier_a", None)
            tier_b = body.get("tier_b", None)
            if not name:
                self.send_json({"error": "name required"}, 400)
                return
            if cross_tier and (not tier_a or not tier_b):
                self.send_json({"error": "tier_a and tier_b required for cross-tier tournaments"}, 400)
                return
            if not cross_tier and not div_id:
                self.send_json({"error": "division_id required"}, 400)
                return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO tournaments (division_id, name, date, cross_tier, tier_a, tier_b) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                            (div_id, name, date, cross_tier, tier_a, tier_b)
                        )
                        new_id = cur.fetchone()[0]
                    conn.commit()
                self.send_json({"ok": True, "id": new_id, **load_division_data(div_id)})
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Tournament already exists"}, 400)

        elif path == "/api/games":
            div_id = body.get("division_id")
            tournament_id = body.get("tournament_id")
            winner = body.get("winner")
            loser = body.get("loser")
            result_type = body.get("type", "W")
            phase = body.get("phase", "pool")
            cross_tier = bool(body.get("cross_tier", False))
            winner_division_id = body.get("winner_division_id", None)
            loser_division_id = body.get("loser_division_id", None)
            if not winner or not loser or winner == loser:
                self.send_json({"error": "Invalid game data"}, 400)
                return
            if not cross_tier and not div_id:
                self.send_json({"error": "division_id required"}, 400)
                return
            # For cross-tier games, use winner_division_id as primary division_id
            primary_div = div_id if not cross_tier else (winner_division_id or div_id)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO games (division_id, tournament_id, winner, loser, type, phase, cross_tier, winner_division_id, loser_division_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (primary_div, tournament_id, winner, loser, result_type, phase, cross_tier, winner_division_id, loser_division_id)
                    )
                conn.commit()
            self.send_json({"ok": True, **load_division_data(primary_div)})

        elif path == "/api/divisions/tier":
            div_id = body.get("id")
            tier = body.get("tier", None)
            if tier is not None:
                try: tier = int(tier)
                except: tier = None
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE divisions SET tier = %s WHERE id = %s", (tier, div_id))
                conn.commit()
            self.send_json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/auth/logout":
            token = get_session_token(self.headers)
            if token in sessions:
                del sessions[token]
            self.send_json({"ok": True}, extra_headers={
                "Set-Cookie": "session=; Path=/; HttpOnly; Max-Age=0"
            })
            return

        if not self.require_auth():
            return

        if path == "/api/divisions":
            div_id = body.get("id")
            if not div_id:
                self.send_json({"error": "id required"}, 400)
                return
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM divisions WHERE id = %s", (div_id,))
                conn.commit()
            self.send_json({"ok": True})

        elif path == "/api/tournaments":
            t_id = body.get("id")
            div_id = body.get("division_id")
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM tournaments WHERE id = %s", (t_id,))
                conn.commit()
            self.send_json({"ok": True, **load_division_data(div_id)})

        elif path == "/api/teams":
            name = body.get("name")
            div_id = body.get("division_id")
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM games WHERE division_id = %s AND (winner = %s OR loser = %s)", (div_id, name, name))
                    cur.execute("DELETE FROM teams WHERE name = %s AND division_id = %s", (name, div_id))
                conn.commit()
            self.send_json({"ok": True})

        elif path == "/api/games":
            game_id = body.get("id")
            div_id = body.get("division_id")
            if game_id is not None:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM games WHERE id = %s", (game_id,))
                    conn.commit()
            self.send_json({"ok": True, **load_division_data(div_id)})

        elif path == "/api/all":
            div_id = body.get("division_id")
            if div_id:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM games WHERE division_id = %s", (div_id,))
                        cur.execute("DELETE FROM tournaments WHERE division_id = %s", (div_id,))
                        cur.execute("DELETE FROM teams WHERE division_id = %s", (div_id,))
                    conn.commit()
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "division_id required"}, 400)

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), KRACHHandler)
    print(f"Server running on port {port}")
    server.serve_forever()
