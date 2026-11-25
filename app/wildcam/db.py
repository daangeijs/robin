"""SQLite storage for clips and their classifications."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT NOT NULL UNIQUE,
    original_name   TEXT NOT NULL,
    media_path      TEXT NOT NULL,          -- relative to media_dir
    kind            TEXT NOT NULL,          -- image | video
    captured_at     TEXT NOT NULL,          -- ISO 8601, local time as recorded by camera if known
    ingested_at     TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'upload',
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    error           TEXT,
    thumb           TEXT,                   -- file name in thumbs_dir
    best_frame      TEXT,                   -- file name in thumbs_dir (frame the label came from)
    taxon           TEXT,                   -- full SpeciesNet label string
    common_name     TEXT,                   -- English common name
    name_nl         TEXT,                   -- Dutch display name
    score           REAL,
    is_blank        INTEGER NOT NULL DEFAULT 0,
    detections_json TEXT,                   -- [{label, conf, bbox:[x,y,w,h] normalized}]
    top_json        TEXT,                   -- [[name, score], ...] alternatives
    manual_label    TEXT,                   -- user correction (Dutch name), overrides model
    notified        INTEGER NOT NULL DEFAULT 0,
    review          TEXT,                   -- bevestigd | twijfel, set by the user
    frame_times_json TEXT,                  -- [seconds, ...] stills chosen by the user (NULL = spread evenly)
    votes_json      TEXT                    -- [{t, thumb, by: {model: {name, score, taxon, blank, detections}}}, ...]
);
CREATE INDEX IF NOT EXISTS idx_clips_captured ON clips(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                     -- JSON
);
"""

# Columns added after the first release, for databases created before them.
ADDED_COLUMNS = {"review": "TEXT", "frame_times_json": "TEXT", "votes_json": "TEXT", "temperature": "REAL",
                 "weather_code": "INTEGER", "is_day": "INTEGER", "air_temp": "REAL"}

JSON_COLUMNS = ("detections_json", "top_json", "votes_json", "frame_times_json")

REVIEWS = ("bevestigd", "twijfel")
# Review state for the timeline filter: confirmed (or named by hand, which confirms) or still open.
STATES = {"bevestigd": "review = 'bevestigd'", "open": "(review IS NULL OR review != 'bevestigd')"}

# Effective label: manual correction wins over the model.
# A clip where some model could not read some still (its message sits under "errors").
FAILED_READINGS_SQL = (
    "votes_json IS NOT NULL AND EXISTS (SELECT 1 FROM json_each(votes_json) v "
    "WHERE json_extract(v.value, '$.errors') NOT IN ('{}', '[]'))"
)
LABEL_SQL = "COALESCE(manual_label, CASE WHEN is_blank = 1 THEN 'leeg' ELSE name_nl END)"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(clips)")}
        if "frames" in have:
            # Short-lived column holding a frame count, with votes stored as pairs.
            self._conn.execute("ALTER TABLE clips DROP COLUMN frames")
            self._conn.execute("UPDATE clips SET votes_json = NULL")
        for name, kind in ADDED_COLUMNS.items():
            if name not in have:
                self._conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {kind}")

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    # ---- writes -----------------------------------------------------------------

    def insert_clip(self, **fields: Any) -> int | None:
        """Insert a clip. Returns the new id, or None if the sha256 already exists."""
        fields.setdefault("ingested_at", now_iso())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._cursor() as cur:
            try:
                cur.execute(f"INSERT INTO clips ({cols}) VALUES ({marks})", tuple(fields.values()))
            except sqlite3.IntegrityError:
                return None
            return cur.lastrowid

    def update_clip(self, clip_id: int, **fields: Any) -> None:
        for key in JSON_COLUMNS:
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._cursor() as cur:
            cur.execute(f"UPDATE clips SET {sets} WHERE id = ?", (*fields.values(), clip_id))

    def retranslate(self, to_dutch) -> None:
        """Bring stored Dutch names in line with the current name table."""
        with self._cursor() as cur:
            rows = cur.execute("SELECT DISTINCT common_name, name_nl FROM clips WHERE common_name IS NOT NULL").fetchall()
            for r in rows:
                name = to_dutch(r["common_name"])
                if name != r["name_nl"]:
                    cur.execute("UPDATE clips SET name_nl = ? WHERE common_name = ?", (name, r["common_name"]))

    def requeue_all(self) -> int:
        """Have every clip judged again, for instance after the choice of models changed."""
        with self._cursor() as cur:
            return cur.execute("UPDATE clips SET status = 'pending', error = NULL").rowcount

    def requeue_failed_readings(self) -> int:
        """Send back clips where a model failed on some still, so it gets another go."""
        with self._cursor() as cur:
            return cur.execute(f"UPDATE clips SET status = 'pending' WHERE status = 'done' AND {FAILED_READINGS_SQL}").rowcount

    def count_failed_readings(self) -> int:
        with self._cursor() as cur:
            return cur.execute(f"SELECT COUNT(*) FROM clips WHERE {FAILED_READINGS_SQL}").fetchone()[0]

    def forget_all_readings(self) -> int:
        """Have every model read every still again, as if the clips came in just now."""
        with self._cursor() as cur:
            cur.execute("UPDATE clips SET review = NULL WHERE manual_label IS NULL")
            return cur.execute("UPDATE clips SET status = 'pending', error = NULL, votes_json = '[]'").rowcount

    def setting(self, key: str, default: Any) -> Any:
        with self._cursor() as cur:
            row = cur.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def delete_clip(self, clip_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM clips WHERE id = ?", (clip_id,))

    # ---- reads ------------------------------------------------------------------

    def get(self, clip_id: int) -> dict | None:
        with self._cursor() as cur:
            row = cur.execute(f"SELECT *, {LABEL_SQL} AS label FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return _row(row) if row else None

    def clips_without_weather(self, limit: int) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT id, captured_at FROM clips WHERE weather_code IS NULL ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rename_source(self, old: str, new: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE clips SET source = ? WHERE source = ?", (new, old))

    def reset_weather(self) -> None:
        """Look the weather up again, e.g. after the location changed."""
        with self._cursor() as cur:
            cur.execute("UPDATE clips SET weather_code = NULL, is_day = NULL, air_temp = NULL")

    def exists_sha(self, sha: str) -> bool:
        with self._cursor() as cur:
            return cur.execute("SELECT 1 FROM clips WHERE sha256 = ?", (sha,)).fetchone() is not None

    def next_pending(self) -> dict | None:
        with self._cursor() as cur:
            row = cur.execute(
                f"SELECT *, {LABEL_SQL} AS label FROM clips WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
        return _row(row) if row else None

    def list_clips(
        self,
        *,
        label: str | None = None,
        include_blank: bool = False,
        review: str | None = None,
        state: str | None = None,
        limit: int = 60,
        offset: int = 0,
    ) -> list[dict]:
        where, args = self._filters(label, include_blank, review, state)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT *, {LABEL_SQL} AS label FROM clips {where} "
                "ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_row(r) for r in rows]

    def count_clips(self, *, label: str | None = None, include_blank: bool = False, review: str | None = None,
                    state: str | None = None) -> int:
        where, args = self._filters(label, include_blank, review, state)
        with self._cursor() as cur:
            return cur.execute(f"SELECT COUNT(*) FROM clips {where}", args).fetchone()[0]

    def label_counts(self, include_blank: bool = False, state: str | None = None) -> list[tuple[str, int]]:
        blank = "" if include_blank else f"AND {LABEL_SQL} != 'leeg'"
        reviewed = f"AND {STATES[state]}" if state in STATES else ""
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT {LABEL_SQL} AS label, COUNT(*) AS n FROM clips "
                f"WHERE status = 'done' AND {LABEL_SQL} IS NOT NULL {blank} {reviewed} "
                "GROUP BY label ORDER BY n DESC, label"
            ).fetchall()
        return [(r["label"], r["n"]) for r in rows]

    def english_names(self) -> dict[str, str]:
        """Dutch name -> the English common name the models gave it most often."""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT name_nl, common_name, COUNT(*) AS n FROM clips "
                "WHERE name_nl IS NOT NULL AND common_name IS NOT NULL GROUP BY name_nl, common_name ORDER BY n, common_name"
            ).fetchall()
        return {r["name_nl"]: r["common_name"] for r in rows}

    def review_counts(self) -> dict[str, int]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT review, COUNT(*) AS n FROM clips WHERE review IS NOT NULL GROUP BY review").fetchall()
        return {r["review"]: r["n"] for r in rows}

    def status_counts(self) -> dict[str, int]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def label_seen_before(self, label: str, exclude_id: int) -> bool:
        with self._cursor() as cur:
            return (
                cur.execute(
                    f"SELECT 1 FROM clips WHERE id != ? AND status = 'done' AND {LABEL_SQL} = ? LIMIT 1",
                    (exclude_id, label),
                ).fetchone()
                is not None
            )

    def hourly_activity(self, label: str | None = None) -> list[int]:
        """Number of non-blank clips per hour of day (0..23)."""
        where, args = self._filters(label, include_blank=False)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT CAST(substr(captured_at, 12, 2) AS INTEGER) AS h, COUNT(*) AS n FROM clips {where} GROUP BY h",
                args,
            ).fetchall()
        hours = [0] * 24
        for r in rows:
            if r["h"] is not None and 0 <= r["h"] < 24:
                hours[r["h"]] = r["n"]
        return hours

    def daily_counts(self, days: int = 30) -> list[tuple[str, int]]:
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT substr(captured_at, 1, 10) AS d, COUNT(*) AS n FROM clips "
                f"WHERE status = 'done' AND {LABEL_SQL} != 'leeg' "
                "GROUP BY d ORDER BY d DESC LIMIT ?",
                (days,),
            ).fetchall()
        return [(r["d"], r["n"]) for r in reversed(rows)]

    def _filters(self, label: str | None, include_blank: bool, review: str | None = None,
                 state: str | None = None) -> tuple[str, tuple]:
        clauses: list[str] = []
        args: list[Any] = []
        if state in STATES:
            clauses.append(STATES[state])
        if review:
            # A review filter shows every clip with that mark, blank ones included.
            clauses.append("review = ?")
            args.append(review)
        if label:
            clauses.append(f"{LABEL_SQL} = ?")
            args.append(label)
        elif not include_blank and not review:
            clauses.append(f"({LABEL_SQL} IS NULL OR {LABEL_SQL} != 'leeg')")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, tuple(args)


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in JSON_COLUMNS:
        raw = d.pop(key, None)
        d[key.removesuffix("_json")] = json.loads(raw) if raw else []
    for vote in d["votes"]:
        if "by" not in vote:
            # Stored before there was a choice of models: one reading, by SpeciesNet.
            reading = {k: vote.pop(k, None) for k in ("name", "score", "taxon", "blank", "detections")}
            vote["by"] = {"speciesnet": reading} if reading["taxon"] else {}
    return d
