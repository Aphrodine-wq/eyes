"""
store.py — SQLite storage with FTS5 full-text search.
Stores only parsed text, never images. ~2KB per entry.
"""

import sqlite3
import time
import os
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


DB_PATH = Path.home() / ".claude-eyes" / "eyes.db"


@dataclass
class ScreenEntry:
    id: int
    timestamp: float
    app_name: str
    window_title: str
    text: str
    extra_context: str


class EyesStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")       # faster concurrent reads
        self.conn.execute("PRAGMA synchronous=NORMAL")      # good enough durability
        self.conn.execute("PRAGMA cache_size=-8000")         # 8MB cache
        self._init_db()

    def _init_db(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                app_name TEXT DEFAULT '',
                window_title TEXT DEFAULT '',
                text TEXT NOT NULL,
                extra_context TEXT DEFAULT '',
                phash TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_frames_timestamp ON frames(timestamp);
            CREATE INDEX IF NOT EXISTS idx_frames_app ON frames(app_name);

            -- FTS5 virtual table for full-text search
            CREATE VIRTUAL TABLE IF NOT EXISTS frames_fts USING fts5(
                text,
                extra_context,
                app_name,
                window_title,
                content='frames',
                content_rowid='id',
                tokenize='porter unicode61'
            );

            -- Triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS frames_ai AFTER INSERT ON frames BEGIN
                INSERT INTO frames_fts(rowid, text, extra_context, app_name, window_title)
                VALUES (new.id, new.text, new.extra_context, new.app_name, new.window_title);
            END;

            CREATE TRIGGER IF NOT EXISTS frames_ad AFTER DELETE ON frames BEGIN
                INSERT INTO frames_fts(frames_fts, rowid, text, extra_context, app_name, window_title)
                VALUES ('delete', old.id, old.text, old.extra_context, old.app_name, old.window_title);
            END;
        """)
        self.conn.commit()

    def insert(self, timestamp: float, app_name: str, window_title: str,
               text: str, extra_context: str = "", phash: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO frames (timestamp, app_name, window_title, text, extra_context, phash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (timestamp, app_name, window_title, text, extra_context, phash)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_recent(self, minutes: int = 30, limit: int = 50) -> list[ScreenEntry]:
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context "
            "FROM frames WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (cutoff, limit)
        ).fetchall()
        return [ScreenEntry(*r) for r in rows]

    def get_latest(self) -> Optional[ScreenEntry]:
        row = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context "
            "FROM frames ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        return ScreenEntry(*row) if row else None

    def search(self, query: str, limit: int = 20) -> list[ScreenEntry]:
        """Full-text search across all screen captures."""
        rows = self.conn.execute(
            "SELECT f.id, f.timestamp, f.app_name, f.window_title, "
            "       snippet(frames_fts, 0, '>>>', '<<<', '...', 40) as text, "
            "       f.extra_context "
            "FROM frames_fts "
            "JOIN frames f ON f.id = frames_fts.rowid "
            "WHERE frames_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit)
        ).fetchall()
        return [ScreenEntry(*r) for r in rows]

    def search_by_app(self, app_name: str, minutes: int = 60, limit: int = 20) -> list[ScreenEntry]:
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context "
            "FROM frames WHERE app_name LIKE ? AND timestamp > ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"%{app_name}%", cutoff, limit)
        ).fetchall()
        return [ScreenEntry(*r) for r in rows]

    def prune(self, days: int = 7) -> int:
        """Delete entries older than N days. Returns count deleted."""
        cutoff = time.time() - (days * 86400)
        cur = self.conn.execute("DELETE FROM frames WHERE timestamp < ?", (cutoff,))
        self.conn.execute("INSERT INTO frames_fts(frames_fts) VALUES('rebuild')")
        self.conn.commit()
        self.conn.execute("VACUUM")
        return cur.rowcount

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM frames"
        ).fetchone()
        db_size = os.path.getsize(self.db_path) if self.db_path.exists() else 0
        return {
            "total_frames": row[0],
            "oldest_timestamp": row[1],
            "newest_timestamp": row[2],
            "db_size_mb": round(db_size / (1024 * 1024), 2),
        }

    def close(self):
        self.conn.close()
