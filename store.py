"""
store.py — SQLite storage with FTS5 full-text search.
Stores only parsed text, never images. ~2KB per entry.
"""

import sqlite3
import time
import os
import json
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from collections import Counter
from datetime import datetime, timedelta


DB_PATH = Path.home() / ".claude-eyes" / "eyes.db"
CONFIG_PATH = Path.home() / ".claude-eyes" / "config.json"

DEFAULT_CONFIG = {
    "ignore_apps": ["1Password", "Keychain Access", "LastPass", "Bitwarden"],
    "session_gap_minutes": 5,
    "capture_interval": 10,
}


def load_config() -> dict:
    """Load config from ~/.claude-eyes/config.json, creating defaults if missing."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULT_CONFIG, **cfg}
            return merged
        except Exception:
            return DEFAULT_CONFIG.copy()
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return DEFAULT_CONFIG.copy()


def is_app_ignored(app_name: str, config: dict = None) -> bool:
    """Check if an app is on the ignore list."""
    if config is None:
        config = load_config()
    ignored = [a.lower() for a in config.get("ignore_apps", [])]
    return app_name.lower() in ignored


def parse_natural_time(expression: str) -> tuple[float, float]:
    """
    Parse natural language time expressions into (start, end) timestamps.
    Supports: 'this morning', 'yesterday', 'last 2 hours', 'today', etc.
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    expression = expression.lower().strip()

    if expression in ("this morning", "morning"):
        start = today_start.replace(hour=6)
        end = today_start.replace(hour=12)
    elif expression in ("this afternoon", "afternoon"):
        start = today_start.replace(hour=12)
        end = today_start.replace(hour=17)
    elif expression in ("this evening", "evening", "tonight"):
        start = today_start.replace(hour=17)
        end = today_start.replace(hour=23, minute=59)
    elif expression == "today":
        start = today_start
        end = now
    elif expression == "yesterday":
        start = today_start - timedelta(days=1)
        end = today_start
    elif expression == "yesterday morning":
        yesterday = today_start - timedelta(days=1)
        start = yesterday.replace(hour=6)
        end = yesterday.replace(hour=12)
    elif expression == "yesterday afternoon":
        yesterday = today_start - timedelta(days=1)
        start = yesterday.replace(hour=12)
        end = yesterday.replace(hour=17)
    elif expression.startswith("last "):
        # Parse "last N hours/minutes/days"
        parts = expression.split()
        if len(parts) >= 3:
            try:
                n = int(parts[1])
            except ValueError:
                n = 1
            unit = parts[2].rstrip("s")  # strip plural
            if unit == "hour":
                start = now - timedelta(hours=n)
            elif unit == "minute" or unit == "min":
                start = now - timedelta(minutes=n)
            elif unit == "day":
                start = now - timedelta(days=n)
            elif unit == "week":
                start = now - timedelta(weeks=n)
            else:
                start = now - timedelta(hours=1)
        else:
            start = now - timedelta(hours=1)
        end = now
    elif expression == "this week":
        # Monday of this week
        start = today_start - timedelta(days=now.weekday())
        end = now
    elif expression == "last week":
        this_monday = today_start - timedelta(days=now.weekday())
        start = this_monday - timedelta(weeks=1)
        end = this_monday
    else:
        # Default: last hour
        start = now - timedelta(hours=1)
        end = now

    return (start.timestamp(), end.timestamp())


@dataclass
class ScreenEntry:
    id: int
    timestamp: float
    app_name: str
    window_title: str
    text: str
    extra_context: str


@dataclass
class Session:
    """A work session — contiguous period of activity."""
    start: float
    end: float
    duration_minutes: float
    apps: list[str]
    top_app: str
    frame_count: int
    summary: str


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

    def get_by_time_range(self, start: float, end: float, limit: int = 100) -> list[ScreenEntry]:
        """Get entries within an absolute time range."""
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context "
            "FROM frames WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (start, end, limit)
        ).fetchall()
        return [ScreenEntry(*r) for r in rows]

    def get_focus_stats(self, minutes: int = 60) -> dict:
        """
        Get app focus time breakdown for the last N minutes.
        Returns time per app, context switches, and top apps.
        """
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT timestamp, app_name FROM frames "
            "WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if not rows:
            return {"apps": {}, "switches": 0, "total_frames": 0}

        app_frames = Counter()
        switches = 0
        prev_app = None

        for ts, app in rows:
            app_frames[app] += 1
            if prev_app and app != prev_app:
                switches += 1
            prev_app = app

        total = sum(app_frames.values())
        config = load_config()
        interval = config.get("capture_interval", 10)

        apps = {}
        for app, count in app_frames.most_common():
            est_minutes = round((count * interval) / 60, 1)
            apps[app] = {
                "frames": count,
                "estimated_minutes": est_minutes,
                "percent": round((count / total) * 100, 1),
            }

        return {
            "apps": apps,
            "switches": switches,
            "total_frames": total,
            "period_minutes": minutes,
        }

    def get_sessions(self, hours: int = 8) -> list[Session]:
        """
        Detect work sessions — contiguous periods of activity
        separated by gaps (default 5 min with no captures).
        """
        cutoff = time.time() - (hours * 3600)
        rows = self.conn.execute(
            "SELECT timestamp, app_name FROM frames "
            "WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if not rows:
            return []

        config = load_config()
        gap_threshold = config.get("session_gap_minutes", 5) * 60

        sessions = []
        session_start = rows[0][0]
        session_apps = [rows[0][1]]
        prev_ts = rows[0][0]

        for ts, app in rows[1:]:
            if ts - prev_ts > gap_threshold:
                # End current session, start new one
                sessions.append(self._build_session(session_start, prev_ts, session_apps))
                session_start = ts
                session_apps = [app]
            else:
                session_apps.append(app)
            prev_ts = ts

        # Final session
        sessions.append(self._build_session(session_start, prev_ts, session_apps))
        return sessions

    def _build_session(self, start: float, end: float, apps: list[str]) -> Session:
        duration = max((end - start) / 60, 0.5)
        app_counts = Counter(apps)
        top_app = app_counts.most_common(1)[0][0] if app_counts else "Unknown"
        unique_apps = list(app_counts.keys())

        # Build a short summary
        parts = []
        for app, count in app_counts.most_common(3):
            pct = round((count / len(apps)) * 100)
            parts.append(f"{app} ({pct}%)")
        summary = f"{duration:.0f}min — " + ", ".join(parts)

        return Session(
            start=start,
            end=end,
            duration_minutes=round(duration, 1),
            apps=unique_apps,
            top_app=top_app,
            frame_count=len(apps),
            summary=summary,
        )

    def get_activity_summary(self, minutes: int = 60) -> str:
        """
        Generate a narrative summary of recent activity.
        Groups by app and describes the flow of work.
        """
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT timestamp, app_name, window_title, "
            "SUBSTR(text, 1, 200) as text_preview "
            "FROM frames WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if not rows:
            return f"No activity in the last {minutes} minutes."

        # Group into app segments (consecutive same-app captures)
        segments = []
        current_app = rows[0][1]
        current_start = rows[0][0]
        current_windows = set()
        current_texts = []

        for ts, app, window, text in rows:
            if app != current_app:
                segments.append({
                    "app": current_app,
                    "start": current_start,
                    "end": ts,
                    "windows": list(current_windows),
                    "sample_text": current_texts[-1] if current_texts else "",
                })
                current_app = app
                current_start = ts
                current_windows = set()
                current_texts = []
            if window:
                current_windows.add(window)
            if text.strip():
                current_texts.append(text.strip()[:150])

        # Final segment
        segments.append({
            "app": current_app,
            "start": current_start,
            "end": rows[-1][0],
            "windows": list(current_windows),
            "sample_text": current_texts[-1] if current_texts else "",
        })

        # Build narrative
        lines = [f"Activity summary (last {minutes} min, {len(rows)} captures):\n"]
        for seg in segments:
            start_str = datetime.fromtimestamp(seg["start"]).strftime("%H:%M")
            dur = max((seg["end"] - seg["start"]) / 60, 0.15)
            line = f"  {start_str} | {seg['app']} ({dur:.0f}min)"
            if seg["windows"]:
                wins = [w for w in seg["windows"] if w][:2]
                if wins:
                    line += f" — {', '.join(wins)}"
            lines.append(line)

        # Focus stats
        app_counts = Counter(r[1] for r in rows)
        config = load_config()
        interval = config.get("capture_interval", 10)
        lines.append(f"\nFocus breakdown:")
        for app, count in app_counts.most_common(5):
            est_min = round((count * interval) / 60, 1)
            lines.append(f"  {app}: ~{est_min}min ({round(count/len(rows)*100)}%)")

        return "\n".join(lines)

    def close(self):
        self.conn.close()
