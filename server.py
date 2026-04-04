#!/usr/bin/env python3
"""
KRACH Hockey Ratings - Render Web Server with PostgreSQL
"""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS teams (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    id SERIAL PRIMARY KEY,
                    winner TEXT NOT NULL,
                    loser TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'W',
                    playoff BOOLEAN NOT NULL DEFAULT FALSE,
                    round TEXT
                );
            """)
        conn.commit()


def load_data():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM teams ORDER BY id")
            teams = [row["name"] for row in cur.fetchall()]
            cur.execute("SELECT id, winner, loser, type, playoff, round FROM games ORDER BY id")
            games = [{"id": row["id"], "winner": row["winner"], "loser": row["loser"], "type": row["type"], "playoff": row["playoff"], "round": row["round"]} for row in cur.fetchall()]
    return {"teams": teams, "games": games}


class KRACHHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self.send_file("index.html", "text/html")
        elif path == "/api/data":
            self.send_json(load_data())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/teams":
            name = body.get("name", "").strip()
            if not name:
                self.send_json({"error": "Team name required"}, 400)
                return
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO teams (name) VALUES (%s)", (name,))
                    conn.commit()
            except psycopg2.errors.UniqueViolation:
                self.send_json({"error": "Team already exists"}, 400)
                return
            self.send_json({"ok": True, **load_data()})

        elif path == "/api/games":
            winner = body.get("winner")
            loser = body.get("loser")
            result_type = body.get("type", "W")
            playoff = bool(body.get("playoff", False))
            round_name = body.get("round", None)
            if not winner or not loser or winner == loser:
                self.send_json({"error": "Invalid game data"}, 400)
                return
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO games (winner, loser, type, playoff, round) VALUES (%s, %s, %s, %s, %s)",
                        (winner, loser, result_type, playoff, round_name)
                    )
                conn.commit()
            self.send_json({"ok": True, **load_data()})

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/teams":
            name = body.get("name")
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM games WHERE winner = %s OR loser = %s", (name, name))
                    cur.execute("DELETE FROM teams WHERE name = %s", (name,))
                conn.commit()
            self.send_json({"ok": True})

        elif path == "/api/games":
            game_id = body.get("id")
            if game_id is not None:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM games WHERE id = %s", (game_id,))
                    conn.commit()
            self.send_json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), KRACHHandler)
    print(f"Server running on port {port}")
    server.serve_forever()
