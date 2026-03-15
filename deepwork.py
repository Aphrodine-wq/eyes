"""
deepwork.py — Deep work tracking, scoring, and coaching.

Tracks focus sessions, computes daily deep-work scores, maintains
streaks, and fires contextual coaching nudges.  Scores are persisted
in the eyes.db SQLite database.
"""

import json
import time
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from store import EyesStore, DB_PATH, load_config
from classifier import classify_capture, PRODUCTIVE_CATEGORIES
from flow import FlowDetector


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DeepWorkScore:
    """Daily deep-work score snapshot."""
    date: str                   # YYYY-MM-DD
    score: int                  # 0-100
    deep_minutes: float
    shallow_minutes: float
    distraction_minutes: float
    sessions: int
    longest_session_min: float
    grade: str                  # A+, A, B+, B, C, D, F

    @staticmethod
    def grade_for(score: int) -> str:
        if score >= 95:
            return "A+"
        elif score >= 85:
            return "A"
        elif score >= 75:
            return "B+"
        elif score >= 65:
            return "B"
        elif score >= 50:
            return "C"
        elif score >= 35:
            return "D"
        return "F"


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------

DEEP_CATEGORIES = {"code", "terminal", "docs", "design"}
SHALLOW_CATEGORIES = {"email", "browser", "system", "unknown"}
DISTRACTION_CATEGORIES = {"media", "chat"}

TARGET_DEEP_MINUTES = 240.0  # 4 hours


def _category_bucket(category: str) -> str:
    """Map a classifier category to deep / shallow / distraction."""
    if category in DEEP_CATEGORIES:
        return "deep"
    if category in DISTRACTION_CATEGORIES:
        return "distraction"
    return "shallow"


# ---------------------------------------------------------------------------
# DeepWorkTracker
# ---------------------------------------------------------------------------

class DeepWorkTracker:
    """
    Persistent deep-work tracker.

    Records focus sessions, computes a daily score (0-100), maintains
    streaks, and provides contextual coaching nudges.
    """

    def __init__(self, store: Optional[EyesStore] = None, db_path: Optional[str] = None):
        self.store = store or EyesStore(db_path)
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn = self.store.conn
        self._init_table()

        # In-memory session log for today (rebuilt from DB on init)
        self._sessions: list[dict] = []
        self._last_nudge_time: float = 0.0
        self._load_today_sessions()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_table(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS deep_work_scores (
                date TEXT PRIMARY KEY,
                score INTEGER,
                deep_minutes REAL,
                shallow_minutes REAL,
                sessions INTEGER,
                longest_session REAL,
                data TEXT
            );

            CREATE TABLE IF NOT EXISTS deep_work_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                app TEXT NOT NULL,
                duration_min REAL NOT NULL,
                category TEXT NOT NULL,
                bucket TEXT NOT NULL,
                flow_score REAL NOT NULL DEFAULT 0,
                timestamp REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_dws_date
                ON deep_work_sessions(date);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Session recording
    # ------------------------------------------------------------------

    def record_session(self, app: str, duration_min: float,
                       category: str, flow_score: float = 0.0):
        """Record a completed focus session."""
        today = datetime.now().strftime("%Y-%m-%d")
        bucket = _category_bucket(category)
        now = time.time()

        self._conn.execute(
            "INSERT INTO deep_work_sessions "
            "(date, app, duration_min, category, bucket, flow_score, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (today, app, duration_min, category, bucket, flow_score, now),
        )
        self._conn.commit()

        self._sessions.append({
            "app": app,
            "duration_min": duration_min,
            "category": category,
            "bucket": bucket,
            "flow_score": flow_score,
            "timestamp": now,
        })

        # Recompute and persist today's score
        self._persist_daily_score(today)

    def _load_today_sessions(self):
        """Load today's sessions from DB into memory."""
        today = datetime.now().strftime("%Y-%m-%d")
        rows = self._conn.execute(
            "SELECT app, duration_min, category, bucket, flow_score, timestamp "
            "FROM deep_work_sessions WHERE date = ? ORDER BY timestamp ASC",
            (today,),
        ).fetchall()
        self._sessions = [
            {
                "app": r[0], "duration_min": r[1], "category": r[2],
                "bucket": r[3], "flow_score": r[4], "timestamp": r[5],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def get_daily_score(self, date: Optional[str] = None) -> DeepWorkScore:
        """
        Compute (or retrieve) the deep-work score for a given date.
        Defaults to today.
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # Try cached score first (for past days)
        row = self._conn.execute(
            "SELECT score, deep_minutes, shallow_minutes, sessions, "
            "longest_session, data FROM deep_work_scores WHERE date = ?",
            (date,),
        ).fetchone()

        if row:
            data = json.loads(row[5]) if row[5] else {}
            distraction_min = data.get("distraction_minutes", 0.0)
            score = row[0]
            return DeepWorkScore(
                date=date,
                score=score,
                deep_minutes=row[1],
                shallow_minutes=row[2],
                distraction_minutes=distraction_min,
                sessions=row[3],
                longest_session_min=row[4],
                grade=DeepWorkScore.grade_for(score),
            )

        # Compute from sessions
        return self._compute_score(date)

    def _compute_score(self, date: str) -> DeepWorkScore:
        """Compute score from raw sessions for *date*."""
        rows = self._conn.execute(
            "SELECT app, duration_min, category, bucket, flow_score "
            "FROM deep_work_sessions WHERE date = ?",
            (date,),
        ).fetchall()

        deep_min = 0.0
        shallow_min = 0.0
        distraction_min = 0.0
        deep_sessions = 0
        longest = 0.0

        for app, dur, cat, bucket, flow in rows:
            if bucket == "deep":
                deep_min += dur
                deep_sessions += 1
                longest = max(longest, dur)
            elif bucket == "distraction":
                distraction_min += dur
            else:
                shallow_min += dur

        # --- Scoring formula ---
        # Base: fraction of target deep minutes (max 60 pts)
        base = min(deep_min / TARGET_DEEP_MINUTES, 1.0) * 60

        # Bonus: 3+ deep sessions
        bonus_sessions = 10 if deep_sessions >= 3 else 0

        # Bonus: longest session > 45 min
        bonus_longest = 10 if longest >= 45 else 0

        # Bonus: low context switches (use store focus stats)
        config = load_config()
        interval = config.get("capture_interval", 10)
        total_hours = max((deep_min + shallow_min + distraction_min) / 60, 1)
        focus_stats = self.store.get_focus_stats(minutes=int(total_hours * 60))
        switches = focus_stats.get("switches", 0)
        switches_per_hr = switches / max(total_hours, 1)
        bonus_switches = 10 if switches_per_hr < 10 else 0

        # Penalty: distraction beyond 30 min
        excess_distraction_hrs = max(0, distraction_min - 30) / 60
        penalty = int(excess_distraction_hrs * 5)

        raw = base + bonus_sessions + bonus_longest + bonus_switches - penalty
        score = max(0, min(100, int(round(raw))))

        return DeepWorkScore(
            date=date,
            score=score,
            deep_minutes=round(deep_min, 1),
            shallow_minutes=round(shallow_min, 1),
            distraction_minutes=round(distraction_min, 1),
            sessions=deep_sessions,
            longest_session_min=round(longest, 1),
            grade=DeepWorkScore.grade_for(score),
        )

    def _persist_daily_score(self, date: str):
        """Recompute and upsert the daily score row."""
        ds = self._compute_score(date)
        data = json.dumps({
            "distraction_minutes": ds.distraction_minutes,
            "grade": ds.grade,
        })
        self._conn.execute(
            "INSERT OR REPLACE INTO deep_work_scores "
            "(date, score, deep_minutes, shallow_minutes, sessions, longest_session, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, ds.score, ds.deep_minutes, ds.shallow_minutes,
             ds.sessions, ds.longest_session_min, data),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Streaks
    # ------------------------------------------------------------------

    def get_streak(self) -> int:
        """
        Count consecutive days (ending today or yesterday) where the
        user logged 2+ hours of deep work.
        """
        rows = self._conn.execute(
            "SELECT date, deep_minutes FROM deep_work_scores "
            "ORDER BY date DESC LIMIT 90"
        ).fetchall()

        if not rows:
            return 0

        streak = 0
        expected = datetime.now().date()

        for date_str, deep_min in rows:
            row_date = datetime.strptime(date_str, "%Y-%m-%d").date()

            # Allow starting from today or yesterday
            if streak == 0 and (expected - row_date).days > 1:
                return 0
            if streak == 0:
                expected = row_date

            if row_date != expected:
                break
            if deep_min >= 120:
                streak += 1
                expected -= timedelta(days=1)
            else:
                break

        return streak

    # ------------------------------------------------------------------
    # Trends
    # ------------------------------------------------------------------

    def get_weekly_trend(self) -> list[float]:
        """Return 7-day score history (oldest first). Missing days = 0."""
        today = datetime.now().date()
        scores = []
        for days_ago in range(6, -1, -1):
            d = (today - timedelta(days=days_ago)).isoformat()
            row = self._conn.execute(
                "SELECT score FROM deep_work_scores WHERE date = ?", (d,)
            ).fetchone()
            scores.append(float(row[0]) if row else 0.0)
        return scores

    # ------------------------------------------------------------------
    # Coaching nudges
    # ------------------------------------------------------------------

    def should_nudge(self) -> Optional[str]:
        """
        Check whether a coaching nudge should fire right now.

        Returns the nudge message, or None if no nudge is appropriate.
        Rate-limited to one nudge per 10 minutes.
        """
        now = time.time()
        if now - self._last_nudge_time < 600:
            return None

        # Gather recent sessions (last 60 min)
        recent = [s for s in self._sessions if now - s["timestamp"] < 3600]
        if not recent:
            return None

        # --- Nudge: extended shallow work ---
        shallow_tail = self._tail_bucket_minutes(recent, "shallow")
        if shallow_tail >= 30:
            self._last_nudge_time = now
            return "You've been in shallow work for 30 min -- ready for a focus block?"

        # --- Nudge: great focus session just ended ---
        if len(recent) >= 1:
            last = recent[-1]
            if last["bucket"] == "deep" and 40 <= last["duration_min"] <= 55:
                age = now - last["timestamp"]
                if age < 300:  # within last 5 min
                    self._last_nudge_time = now
                    return "Great 45-min focus session! Take a 5-min break."

        # --- Nudge: excessive chat / messaging ---
        chat_tail = self._tail_app_minutes(recent, "chat")
        if chat_tail >= 20:
            avg_chat = self._avg_daily_chat_minutes()
            if avg_chat > 0 and chat_tail >= avg_chat * 2:
                self._last_nudge_time = now
                return f"You've been in Slack for 20 min -- that's 2x your usual."

        return None

    def _tail_bucket_minutes(self, sessions: list[dict], bucket: str) -> float:
        """Minutes of consecutive *bucket* work at the tail of *sessions*."""
        total = 0.0
        for s in reversed(sessions):
            if s["bucket"] == bucket:
                total += s["duration_min"]
            else:
                break
        return total

    def _tail_app_minutes(self, sessions: list[dict], category: str) -> float:
        """Minutes of consecutive *category* work at the tail of *sessions*."""
        total = 0.0
        for s in reversed(sessions):
            if s["category"] == category:
                total += s["duration_min"]
            else:
                break
        return total

    def _avg_daily_chat_minutes(self) -> float:
        """Average daily chat minutes over the last 7 days."""
        today = datetime.now().date()
        totals = []
        for days_ago in range(1, 8):
            d = (today - timedelta(days=days_ago)).isoformat()
            row = self._conn.execute(
                "SELECT SUM(duration_min) FROM deep_work_sessions "
                "WHERE date = ? AND category = 'chat'",
                (d,),
            ).fetchone()
            totals.append(row[0] if row and row[0] else 0.0)
        return sum(totals) / max(len([t for t in totals if t > 0]), 1)

    # ------------------------------------------------------------------
    # Leaderboard / personal bests
    # ------------------------------------------------------------------

    def get_leaderboard(self) -> dict:
        """
        Personal bests, current streak, and milestones.
        """
        streak = self.get_streak()
        today_score = self.get_daily_score()
        trend = self.get_weekly_trend()

        # All-time best score
        row = self._conn.execute(
            "SELECT date, score FROM deep_work_scores ORDER BY score DESC LIMIT 1"
        ).fetchone()
        best_day = {"date": row[0], "score": row[1]} if row else {"date": "N/A", "score": 0}

        # Best streak (scan backward)
        all_rows = self._conn.execute(
            "SELECT date, deep_minutes FROM deep_work_scores ORDER BY date ASC"
        ).fetchall()
        best_streak = 0
        current_run = 0
        prev_date = None
        for date_str, deep_min in all_rows:
            row_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if deep_min >= 120:
                if prev_date and (row_date - prev_date).days == 1:
                    current_run += 1
                else:
                    current_run = 1
                best_streak = max(best_streak, current_run)
            else:
                current_run = 0
            prev_date = row_date

        # Longest single session
        row = self._conn.execute(
            "SELECT MAX(duration_min) FROM deep_work_sessions WHERE bucket = 'deep'"
        ).fetchone()
        longest_session = round(row[0], 1) if row and row[0] else 0.0

        # Total deep hours all time
        row = self._conn.execute(
            "SELECT SUM(deep_minutes) FROM deep_work_scores"
        ).fetchone()
        total_deep_hrs = round((row[0] or 0) / 60, 1)

        # Milestones
        milestones = []
        if total_deep_hrs >= 100:
            milestones.append("100h deep work")
        if total_deep_hrs >= 500:
            milestones.append("500h deep work")
        if best_streak >= 7:
            milestones.append("7-day streak")
        if best_streak >= 30:
            milestones.append("30-day streak")
        if longest_session >= 90:
            milestones.append("90-min focus session")
        if longest_session >= 120:
            milestones.append("2-hour focus marathon")

        return {
            "today": asdict(today_score),
            "streak": streak,
            "best_streak": best_streak,
            "best_day": best_day,
            "longest_session_min": longest_session,
            "total_deep_hours": total_deep_hrs,
            "weekly_trend": trend,
            "milestones": milestones,
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def backfill_from_captures(self, days: int = 7):
        """
        Backfill deep-work sessions from raw screen captures.

        Walks through stored frames, detects sustained app usage blocks,
        classifies each block, and records it as a session.  Useful when
        running the tracker for the first time on an existing database.
        """
        now = time.time()
        cutoff = now - (days * 86400)
        config = load_config()
        interval = config.get("capture_interval", 10)

        rows = self._conn.execute(
            "SELECT timestamp, app_name, window_title, SUBSTR(text, 1, 200) "
            "FROM frames WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,),
        ).fetchall()

        if not rows:
            return

        # Group into app blocks (consecutive same-app captures)
        blocks: list[dict] = []
        block_app = rows[0][1]
        block_start = rows[0][0]
        block_count = 1
        block_cat = ""

        for ts, app, win, text in rows[1:]:
            if app == block_app and (ts - block_start) < 1800:
                block_count += 1
            else:
                dur = (block_count * interval) / 60
                if dur >= 2:
                    c = classify_capture(block_app, "", "")
                    blocks.append({
                        "app": block_app,
                        "duration_min": round(dur, 1),
                        "category": c.category,
                        "bucket": _category_bucket(c.category),
                        "timestamp": block_start,
                        "date": datetime.fromtimestamp(block_start).strftime("%Y-%m-%d"),
                    })
                block_app = app
                block_start = ts
                block_count = 1

        # Final block
        dur = (block_count * interval) / 60
        if dur >= 2:
            c = classify_capture(block_app, "", "")
            blocks.append({
                "app": block_app,
                "duration_min": round(dur, 1),
                "category": c.category,
                "bucket": _category_bucket(c.category),
                "timestamp": block_start,
                "date": datetime.fromtimestamp(block_start).strftime("%Y-%m-%d"),
            })

        # Insert blocks as sessions
        for b in blocks:
            self._conn.execute(
                "INSERT INTO deep_work_sessions "
                "(date, app, duration_min, category, bucket, flow_score, timestamp) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (b["date"], b["app"], b["duration_min"],
                 b["category"], b["bucket"], b["timestamp"]),
            )
        self._conn.commit()

        # Recompute daily scores for affected dates
        dates = sorted(set(b["date"] for b in blocks))
        for d in dates:
            self._persist_daily_score(d)
