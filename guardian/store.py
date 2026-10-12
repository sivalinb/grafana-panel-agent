import contextlib
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS incidents_fingerprint ON incidents(fingerprint);
                CREATE TABLE IF NOT EXISTS checks (key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL, body TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def put_incident(self, item):
        with self.connection() as db:
            db.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, body=excluded.body",
                       (item["id"], item["fingerprint"], item["status"], item["created_at"], json.dumps(item)))

    def get_incident(self, incident_id):
        with self.connection() as db:
            row = db.execute("SELECT body FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not row:
                raise KeyError(incident_id)
            item = json.loads(row[0])
            item["events"] = [dict(row, body=json.loads(row["body"])) for row in
                              db.execute("SELECT at, kind, body FROM events WHERE incident_id=? ORDER BY id", (incident_id,))]
            return item

    def incidents(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM incidents ORDER BY created_at DESC")]

    def active(self, fingerprint):
        with self.connection() as db:
            row = db.execute("SELECT body FROM incidents WHERE fingerprint=? AND status NOT IN ('resolved','closed') ORDER BY created_at DESC LIMIT 1", (fingerprint,)).fetchone()
            return json.loads(row[0]) if row else None

    def event(self, incident_id, kind, body=None):
        with self.connection() as db:
            db.execute("INSERT INTO events(incident_id, at, kind, body) VALUES (?, ?, ?, ?)",
                       (incident_id, now(), kind, json.dumps(body or {})))

    def check(self, key):
        with self.connection() as db:
            row = db.execute("SELECT body FROM checks WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else {}

    def put_check(self, key, body):
        with self.connection() as db:
            db.execute("INSERT INTO checks VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET body=excluded.body", (key, json.dumps(body)))

    def checks(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM checks ORDER BY key")]

    def run(self, body):
        with self.connection() as db:
            db.execute("INSERT INTO runs(at, body) VALUES (?, ?)", (now(), json.dumps(body)))

    def last_run(self):
        with self.connection() as db:
            row = db.execute("SELECT at, body FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(at=row["at"], **json.loads(row["body"])) if row else None
