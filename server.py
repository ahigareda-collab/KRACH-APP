#!/usr/bin/env python3
"""
KRACH Hockey Ratings - Render Web Server with PostgreSQL + Auth
"""

import json
import os
import secrets
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# In-memory session store: token -> True
sessions = {}


def get_conn():
    url = DATABASE_URL
    # Use SSL on Render (hosted), skip it for local connections
    if "render.com" in (url or ""):
        return psycopg2.connect(url, sslmode="require")
    return psycopg2.connect(url)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS divisions (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS games (
                    id SERIAL PRIMARY KEY,
                    division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
                    winner TEXT NOT NULL,
                    loser TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'W',
                    playoff BOOLEAN NOT NULL DEFAULT FALSE,
                    round TEXT
                );
            """)
        conn.commit()


def load_divisions():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM divisions ORDER BY id")
            return [{"id": row["id"], "name": row["name"]} for row in cur.fetchall()]


def load_division_data(division_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM teams WHERE division_id = %s ORDER BY id", (division_id,))
            teams = [row["name"] for row in cur.fetchall()]
            cur.execute("""
                SELECT id, winner, loser, type, playoff, round
                FROM games WHERE division_id = %s ORDER BY id
            """, (division_id,))
            games = [{
                "id": row["id"], "winner": row["winner"], "loser": row["loser"],
                "type": row["type"], "playoff": row["playoff"], "round": row["round"]
            } for row in cur.fetchall()]
    return {"teams": teams, "games": games}


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
        """Returns True if authenticated, otherwise sends 401 and returns False."""
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

        # Public routes
        if path == "/standings":
            self.send_file("standings.html", "text/html")
            return

        if path == "/login":
            self.send_file("login.html", "text/html")
            return

        # Public API (read-only, used by standings page)
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

        # Protected admin routes
        if path == "/" or path == "/index.html":
            if not is_authenticated(self.headers):
                self.redirect("/login")
                return
            self.send_file("index.html", "text/html")
            return

        # Check auth status (used by frontend)
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

        # Login endpoint — public
        if path == "/api/auth/login":
            username = body.get("username", "").strip()
            password = body.get("password", "")
            if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                token = secrets.token_hex(32)
                sessions[token] = True
                self.send_json(
                    {"ok": True},
                    extra_headers={"Set-Cookie": f"session={token}; Path=/; HttpOnly; SameSite=Strict"}
                )
            else:
                self.send_json({"error": "Invalid username or password"}, 401)
            return

        # All other POST routes require auth
        if not self.require_auth():
            return

        if path == "/api/divisions":
            name = body.get("name", "").strip()
            if not name:
                self.send_json({"error": "Division name required"}, 400)
                return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO divisions (name) VALUES (%s) RETURNING id", (name,))
                        new_id = cur.fetchone()[0]
                    conn.commit()
                self.send_json({"ok": True, "id": new_id, "name": name})
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

        elif path == "/api/games":
            div_id = body.get("division_id")
            winner = body.get("winner")
            loser = body.get("loser")
            result_type = body.get("type", "W")
            playoff = bool(body.get("playoff", False))
            round_name = body.get("round", None)
            if not div_id or not winner or not loser or winner == loser:
                self.send_json({"error": "Invalid game data"}, 400)
                return
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO games (division_id, winner, loser, type, playoff, round) VALUES (%s, %s, %s, %s, %s, %s)",
                        (div_id, winner, loser, result_type, playoff, round_name)
                    )
                conn.commit()
            self.send_json({"ok": True, **load_division_data(div_id)})

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        # Logout
        if path == "/api/auth/logout":
            token = get_session_token(self.headers)
            if token in sessions:
                del sessions[token]
            self.send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": "session=; Path=/; HttpOnly; Max-Age=0"}
            )
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
