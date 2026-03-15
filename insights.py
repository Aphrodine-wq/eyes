"""
insights.py — Deep behavioral insights and correlations.

Finds non-obvious patterns: habit loops, productivity correlations,
time-of-day effects, and generates actionable recommendations.

This is the engine that knows you better than you know yourself.
"""

import time
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from store import EyesStore, load_config
from classifier import classify_capture


@dataclass
class HabitLoop:
    """A detected recurring behavior pattern."""
    trigger_app: str        # what starts the loop
    routine: list[str]      # the sequence of apps
    frequency: str          # "daily", "multiple_daily", "weekly"
    typical_time: str       # "9:00-10:00"
    avg_duration_min: float
    occurrences: int
    description: str


@dataclass
class Correlation:
    """A discovered correlation between behaviors."""
    factor_a: str
    factor_b: str
    correlation: float      # -1.0 to 1.0
    direction: str          # "positive", "negative"
    description: str
    sample_size: int


@dataclass
class Recommendation:
    """An actionable productivity recommendation."""
    category: str           # "focus", "scheduling", "habits", "environment"
    title: str
    description: str
    evidence: str           # what data supports this
    impact: str             # "high", "medium", "low"


@dataclass
class InsightReport:
    """Complete insights report."""
    habit_loops: list[HabitLoop]
    correlations: list[Correlation]
    recommendations: list[Recommendation]
    peak_hours: list[int]
    dead_hours: list[int]
    most_productive_day: str
    avg_deep_focus_duration: float
    context_switch_cost_minutes: float


class InsightsEngine:
    """
    Generates deep behavioral insights from screen capture history.
    Needs at least 3-5 days of data to produce meaningful results.
    """

    def generate_report(self, store: EyesStore, days: int = 7) -> InsightReport:
        """Generate a full insights report."""
        cutoff = time.time() - (days * 86400)
        config = load_config()
        interval = config.get("capture_interval", 10)

        rows = store.conn.execute(
            "SELECT timestamp, app_name, window_title, SUBSTR(text, 1, 200) "
            "FROM frames WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if len(rows) < 100:
            return InsightReport(
                habit_loops=[], correlations=[], recommendations=[],
                peak_hours=[], dead_hours=[], most_productive_day="",
                avg_deep_focus_duration=0, context_switch_cost_minutes=0,
            )

        habit_loops = self._detect_habit_loops(rows, interval)
        correlations = self._find_correlations(rows, interval)
        peak_hours, dead_hours = self._find_peak_hours(rows, interval)
        best_day = self._most_productive_day(rows, interval)
        avg_focus = self._avg_focus_duration(rows, interval)
        switch_cost = self._context_switch_cost(rows, interval)
        recommendations = self._generate_recommendations(
            rows, interval, peak_hours, dead_hours, avg_focus, switch_cost, habit_loops
        )

        return InsightReport(
            habit_loops=habit_loops,
            correlations=correlations,
            recommendations=recommendations,
            peak_hours=peak_hours,
            dead_hours=dead_hours,
            most_productive_day=best_day,
            avg_deep_focus_duration=avg_focus,
            context_switch_cost_minutes=switch_cost,
        )

    def _detect_habit_loops(self, rows: list, interval: int) -> list[HabitLoop]:
        """Find recurring daily patterns — same sequence at roughly same time."""
        # Group by day
        daily_sequences = defaultdict(list)
        for ts, app, _, _ in rows:
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            hour = datetime.fromtimestamp(ts).hour
            daily_sequences[day].append((hour, app))

        # Find app transitions that repeat across days at similar times
        hourly_transitions = defaultdict(Counter)  # hour -> (from_app, to_app) -> count

        for day, events in daily_sequences.items():
            prev = None
            for hour, app in events:
                if prev and prev[1] != app:
                    hourly_transitions[hour][(prev[1], app)] += 1
                prev = (hour, app)

        loops = []
        for hour, transitions in hourly_transitions.items():
            for (from_app, to_app), count in transitions.most_common(3):
                if count >= 3:  # appears 3+ times at this hour
                    loops.append(HabitLoop(
                        trigger_app=from_app,
                        routine=[from_app, to_app],
                        frequency="daily" if count >= 5 else "multiple_daily",
                        typical_time=f"{hour:02d}:00-{hour+1:02d}:00",
                        avg_duration_min=round((count * interval) / 60 / len(daily_sequences), 1),
                        occurrences=count,
                        description=f"You typically switch from {from_app} to {to_app} around {hour:02d}:00 ({count}x in the data)",
                    ))

        return sorted(loops, key=lambda l: -l.occurrences)[:10]

    def _find_correlations(self, rows: list, interval: int) -> list[Correlation]:
        """Find correlations between app usage patterns."""
        # Daily app usage vectors
        daily_usage = defaultdict(Counter)
        for ts, app, _, _ in rows:
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            daily_usage[day][app] += 1

        if len(daily_usage) < 3:
            return []

        # Get top apps
        total_counts = Counter()
        for day_counts in daily_usage.values():
            total_counts.update(day_counts)
        top_apps = [app for app, _ in total_counts.most_common(8)]

        # Compute pairwise Pearson correlation
        correlations = []
        days = sorted(daily_usage.keys())

        for i in range(len(top_apps)):
            for j in range(i + 1, len(top_apps)):
                app_a = top_apps[i]
                app_b = top_apps[j]

                vec_a = [daily_usage[d].get(app_a, 0) for d in days]
                vec_b = [daily_usage[d].get(app_b, 0) for d in days]

                r = _pearson(vec_a, vec_b)
                if abs(r) > 0.4:  # meaningful correlation
                    direction = "positive" if r > 0 else "negative"
                    if r > 0:
                        desc = f"Days with more {app_a} also have more {app_b}"
                    else:
                        desc = f"Days with more {app_a} tend to have less {app_b}"

                    correlations.append(Correlation(
                        factor_a=app_a,
                        factor_b=app_b,
                        correlation=round(r, 2),
                        direction=direction,
                        description=desc,
                        sample_size=len(days),
                    ))

        return sorted(correlations, key=lambda c: -abs(c.correlation))[:10]

    def _find_peak_hours(self, rows: list, interval: int) -> tuple[list[int], list[int]]:
        """Find peak productive hours and dead hours."""
        hourly_productive = Counter()
        hourly_total = Counter()

        for ts, app, window, text in rows:
            hour = datetime.fromtimestamp(ts).hour
            hourly_total[hour] += 1
            cls = classify_capture(app, window or "", text or "")
            if cls.is_productive:
                hourly_productive[hour] += 1

        if not hourly_total:
            return [], []

        # Calculate productivity ratio per hour
        hourly_ratio = {}
        for hour in range(6, 24):
            total = hourly_total.get(hour, 0)
            if total >= 5:  # need enough data
                productive = hourly_productive.get(hour, 0)
                hourly_ratio[hour] = productive / total

        if not hourly_ratio:
            return [], []

        # Top 3 peak hours and bottom 3
        sorted_hours = sorted(hourly_ratio.items(), key=lambda x: -x[1])
        peak = [h for h, _ in sorted_hours[:3]]
        dead = [h for h, _ in sorted_hours[-3:]]

        return peak, dead

    def _most_productive_day(self, rows: list, interval: int) -> str:
        """Find which day of the week has highest productive ratio."""
        day_productive = Counter()
        day_total = Counter()

        for ts, app, window, text in rows:
            dow = datetime.fromtimestamp(ts).strftime("%A")
            day_total[dow] += 1
            cls = classify_capture(app, window or "", text or "")
            if cls.is_productive:
                day_productive[dow] += 1

        best_day = ""
        best_ratio = 0
        for dow, total in day_total.items():
            if total >= 10:
                ratio = day_productive.get(dow, 0) / total
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_day = dow

        return best_day

    def _avg_focus_duration(self, rows: list, interval: int) -> float:
        """Average duration of uninterrupted single-app usage."""
        if len(rows) < 5:
            return 0

        sessions = []
        session_start = rows[0][0]
        current_app = rows[0][1]

        for ts, app, _, _ in rows[1:]:
            if app != current_app:
                duration = (ts - session_start) / 60
                if duration >= 2:  # only count 2+ minute sessions
                    sessions.append(duration)
                session_start = ts
                current_app = app

        return round(sum(sessions) / len(sessions), 1) if sessions else 0

    def _context_switch_cost(self, rows: list, interval: int) -> float:
        """
        Estimate the "cost" of context switching.
        Measured as: after a switch, how long until the next sustained
        focus period (5+ captures in same app)?
        """
        if len(rows) < 20:
            return 0

        recovery_times = []
        i = 0
        while i < len(rows) - 1:
            # Detect a switch
            if rows[i][1] != rows[i + 1][1]:
                switch_time = rows[i + 1][0]
                # Find next sustained period (5+ same app in a row)
                j = i + 1
                streak = 1
                while j < len(rows) - 1:
                    if rows[j][1] == rows[j + 1][1]:
                        streak += 1
                        if streak >= 5:
                            recovery = (rows[j][0] - switch_time) / 60
                            if recovery > 0 and recovery < 30:
                                recovery_times.append(recovery)
                            break
                    else:
                        streak = 1
                    j += 1
            i += 1

        return round(sum(recovery_times) / len(recovery_times), 1) if recovery_times else 0

    def _generate_recommendations(self, rows, interval, peak_hours, dead_hours,
                                   avg_focus, switch_cost, habit_loops) -> list[Recommendation]:
        """Generate actionable recommendations from the data."""
        recs = []

        # Peak hours
        if peak_hours:
            peak_str = ", ".join(f"{h}:00" for h in peak_hours)
            recs.append(Recommendation(
                category="scheduling",
                title="Protect your peak hours",
                description=f"Your most productive hours are {peak_str}. Block these for deep work — no meetings, no Slack.",
                evidence=f"Highest productive-to-total capture ratio at these hours",
                impact="high",
            ))

        # Dead hours
        if dead_hours:
            dead_str = ", ".join(f"{h}:00" for h in dead_hours)
            recs.append(Recommendation(
                category="scheduling",
                title="Schedule shallow work for dead hours",
                description=f"Your least productive hours are {dead_str}. Use these for email, meetings, and admin.",
                evidence=f"Lowest productive capture ratio at these hours",
                impact="medium",
            ))

        # Context switch cost
        if switch_cost > 3:
            recs.append(Recommendation(
                category="focus",
                title=f"Each context switch costs you ~{switch_cost:.0f} minutes",
                description=f"After switching apps, it takes an average of {switch_cost:.0f} minutes to re-enter sustained focus. Batch your app switches.",
                evidence=f"Measured recovery time to 5+ consecutive same-app captures after a switch",
                impact="high",
            ))

        # Focus duration
        if avg_focus < 10:
            recs.append(Recommendation(
                category="focus",
                title="Your focus sessions are short",
                description=f"Average uninterrupted focus: {avg_focus:.0f} minutes. Try the Pomodoro technique (25 min blocks) to extend focus.",
                evidence=f"Mean single-app session duration across all data",
                impact="high",
            ))
        elif avg_focus > 30:
            recs.append(Recommendation(
                category="habits",
                title="You're a deep focus worker",
                description=f"Average focus duration: {avg_focus:.0f} minutes. You do well with long uninterrupted blocks. Protect this.",
                evidence=f"Mean single-app session duration",
                impact="low",
            ))

        # Habit loops
        if habit_loops:
            worst_loop = max(habit_loops, key=lambda l: l.occurrences)
            if "chat" in worst_loop.trigger_app.lower() or "slack" in worst_loop.trigger_app.lower():
                recs.append(Recommendation(
                    category="habits",
                    title=f"You have a {worst_loop.trigger_app} habit loop",
                    description=f"You switch to {worst_loop.routine[-1]} after {worst_loop.trigger_app} about {worst_loop.occurrences}x. Consider batching {worst_loop.trigger_app} checks.",
                    evidence=f"Detected {worst_loop.occurrences} occurrences at {worst_loop.typical_time}",
                    impact="medium",
                ))

        return recs


def _pearson(x: list, y: list) -> float:
    """Compute Pearson correlation coefficient."""
    n = len(x)
    if n < 3:
        return 0.0

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))

    if den_x == 0 or den_y == 0:
        return 0.0

    return num / (den_x * den_y)
