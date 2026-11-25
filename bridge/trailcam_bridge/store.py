"""Clip index (SQLite) and bridge state (JSON) under the data dir."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id          INTEGER PRIMARY KEY,   -- camera mediaId
    filename    TEXT NOT NULL,
    kind        TEXT NOT NULL,         -- video | photo
    captured_at TEXT,                  -- ISO, Europe/Amsterdam
    size        INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL,         -- ISO, Europe/Amsterdam
    duration    REAL,
    removed_at  TEXT                   -- set by DELETE /clips/<id>; row kept so the id stays known
);
"""

COLUMNS = ("id", "filename", "kind", "captured_at", "size", "fetched_at", "duration")


def parse_iso(text, tz):
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.clips_dir = os.path.join(data_dir, "clips")
        os.makedirs(self.clips_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "index.db")
        self.state_path = os.path.join(data_dir, "state.json")
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:                          # commit or roll back
                yield db
        finally:
            db.close()

    # ---- clips
    def known(self, media_id):
        with self._lock, self._db() as db:
            return db.execute("SELECT 1 FROM clips WHERE id=?", (media_id,)).fetchone() is not None

    def add(self, clip):
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO clips (id, filename, kind, captured_at, size, fetched_at,"
                       " duration, removed_at) VALUES (?,?,?,?,?,?,?,NULL)",
                       tuple(clip.get(c) for c in COLUMNS))

    def get(self, media_id):
        with self._lock, self._db() as db:
            r = db.execute("SELECT * FROM clips WHERE id=? AND removed_at IS NULL", (media_id,)).fetchone()
        return dict(r) if r else None

    def list(self):
        with self._lock, self._db() as db:
            rows = db.execute("SELECT * FROM clips WHERE removed_at IS NULL"
                              " ORDER BY captured_at DESC, id DESC").fetchall()
        return [dict(r) for r in rows]

    def count(self):
        with self._lock, self._db() as db:
            n, size = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM clips"
                                 " WHERE removed_at IS NULL").fetchone()
        return n, size

    def remove(self, media_id, when):
        clip = self.get(media_id)
        if not clip:
            return False
        try:
            os.remove(self.path(clip))
        except FileNotFoundError:
            pass
        with self._lock, self._db() as db:
            db.execute("UPDATE clips SET removed_at=? WHERE id=?", (when, media_id))
        return True

    def path(self, clip):
        return os.path.join(self.clips_dir, clip["filename"])

    # ---- state (last status / last session), small JSON written atomically
    def load_state(self):
        try:
            with open(self.state_path) as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}

    def save_state(self, state):
        tmp = self.state_path + ".tmp"
        with self._lock:
            with open(tmp, "w") as fh:
                json.dump(state, fh, indent=1)
            os.replace(tmp, self.state_path)
