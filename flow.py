"""
flow.py — Flow state detection and attention analysis.

Detects whether the user is in deep focus, shallow work, or scattered.
Tracks attention patterns over time and identifies what disrupts flow.

Flow detection is based on:
  - App switching frequency (low = focused)
  - Duration in single context (high = focused)
  - Content type consistency (same category = focused)
  - Time between captures with change (steady = focused)
"""

import time
from dataclasses import dataclass, field
from collections import Counter, deque
from typing import Optional
from datetime import datetime


@dataclass
class FlowState:
    """Current flow state assessment."""
    state: str              # "deep_focus", "flow", "working", "shallow", "scattered", "idle"
    score: float            # 0.0 (scattered) to 1.0 (deep focus)
    duration_minutes: float # how long in current state
    primary_app: str        # app driving the flow
    primary_category: str   # content category driving the flow
    disruptions: int        # number of context switches in this period
    streak_minutes: float   # consecutive time in current app

    def description(self) -> str:
        """Human-readable description of flow state."""
        descs = {
            "deep_focus": f"Deep focus in {self.primary_app} for {self.streak_minutes:.0f}min. {self.disruptions} disruptions.",
            "flow": f"In flow — mostly {self.primary_app} ({self.primary_category}). {self.streak_minutes:.0f}min streak.",
            "working": f"Working across apps. Primary: {self.primary_app}. Switching every few minutes.",
            "shallow": f"Shallow work — frequent switching ({self.disruptions} switches). No sustained focus.",
            "scattered": f"Scattered — rapid context switching across {self.disruptions}+ apps.",
            "idle": "Idle or minimal activity.",
        }
        return descs.get(self.state, f"State: {self.state}")


@dataclass
class FlowPeriod:
    """A period of sustained flow."""
    start: float
    end: float
    state: str
    app: str
    category: str
    duration_minutes: float
    score: float


@dataclass
class AttentionProfile:
    """Long-term attention analysis."""
    avg_flow_score: float
    peak_focus_hour: int
    worst_focus_hour: int
    avg_session_before_switch: float  # minutes
    top_flow_apps: list[str]
    top_distraction_apps: list[str]
    flow_periods_today: list[FlowPeriod]
    total_deep_focus_minutes: float
    total_scattered_minutes: float


class FlowDetector:
    """
    Real-time flow state detection.

    Uses a sliding window of recent captures to determine
    the user's current cognitive state. No ML — just smart
    heuristics based on switching patterns.
    """

    # Flow state thresholds
    DEEP_FOCUS_THRESHOLD = 0.85
    FLOW_THRESHOLD = 0.65
    WORKING_THRESHOLD = 0.4
    SHALLOW_THRESHOLD = 0.2

    def __init__(self, window_minutes: int = 15):
        self.window_minutes = window_minutes
        self.captures: deque = deque(maxlen=500)
        self.flow_periods: list[FlowPeriod] = []
        self._current_state = "idle"
        self._state_start = time.time()
        self._current_app_start = time.time()
        self._current_app = ""

    def record(self, timestamp: float, app_name: str, category: str = ""):
        """Record a capture for flow analysis."""
        self.captures.append({
            "timestamp": timestamp,
            "app": app_name,
            "category": category,
        })

        # Track app streak
        if app_name != self._current_app:
            self._current_app = app_name
            self._current_app_start = timestamp

    def get_flow_state(self) -> FlowState:
        """Analyze current flow state from recent captures."""
        now = time.time()
        cutoff = now - (self.window_minutes * 60)

        recent = [c for c in self.captures if c["timestamp"] > cutoff]

        if len(recent) < 3:
            return FlowState(
                state="idle", score=0.0, duration_minutes=0,
                primary_app="", primary_category="",
                disruptions=0, streak_minutes=0,
            )

        # Count app switches
        switches = 0
        prev_app = recent[0]["app"]
        for c in recent[1:]:
            if c["app"] != prev_app:
                switches += 1
            prev_app = c["app"]

        # Calculate app concentration
        app_counts = Counter(c["app"] for c in recent)
        total = len(recent)
        top_app, top_count = app_counts.most_common(1)[0]
        concentration = top_count / total  # 0-1, higher = more focused

        # Category consistency
        cat_counts = Counter(c["category"] for c in recent if c["category"])
        top_cat = cat_counts.most_common(1)[0][0] if cat_counts else ""
        cat_consistency = (cat_counts.get(top_cat, 0) / total) if top_cat else 0

        # Switch rate (switches per minute)
        window_duration = max((recent[-1]["timestamp"] - recent[0]["timestamp"]) / 60, 1)
        switch_rate = switches / window_duration

        # Calculate flow score
        score = self._compute_flow_score(concentration, cat_consistency, switch_rate, len(recent))

        # Determine state
        if score >= self.DEEP_FOCUS_THRESHOLD:
            state = "deep_focus"
        elif score >= self.FLOW_THRESHOLD:
            state = "flow"
        elif score >= self.WORKING_THRESHOLD:
            state = "working"
        elif score >= self.SHALLOW_THRESHOLD:
            state = "shallow"
        else:
            state = "scattered"

        # Track state transitions
        if state != self._current_state:
            self._record_flow_period(now)
            self._current_state = state
            self._state_start = now

        # Current app streak
        streak = (now - self._current_app_start) / 60

        return FlowState(
            state=state,
            score=round(score, 2),
            duration_minutes=round((now - self._state_start) / 60, 1),
            primary_app=top_app,
            primary_category=top_cat,
            disruptions=switches,
            streak_minutes=round(streak, 1),
        )

    def _compute_flow_score(self, concentration: float, cat_consistency: float,
                            switch_rate: float, sample_size: int) -> float:
        """
        Compute a 0-1 flow score from multiple signals.

        High concentration + low switch rate + category consistency = deep flow
        """
        # Normalize switch rate (0 switches/min = 1.0, 3+/min = 0.0)
        switch_factor = max(0, 1.0 - (switch_rate / 3.0))

        # Weight the factors
        score = (
            concentration * 0.4 +
            switch_factor * 0.35 +
            cat_consistency * 0.15 +
            min(sample_size / 20, 1.0) * 0.1  # bonus for having enough data
        )

        return max(0.0, min(1.0, score))

    def _record_flow_period(self, end_time: float):
        """Record a completed flow period."""
        if self._current_state == "idle":
            return

        duration = (end_time - self._state_start) / 60
        if duration < 1:
            return

        # Get primary app for this period
        period_captures = [
            c for c in self.captures
            if c["timestamp"] >= self._state_start and c["timestamp"] <= end_time
        ]
        apps = Counter(c["app"] for c in period_captures)
        cats = Counter(c["category"] for c in period_captures if c["category"])

        period = FlowPeriod(
            start=self._state_start,
            end=end_time,
            state=self._current_state,
            app=apps.most_common(1)[0][0] if apps else "",
            category=cats.most_common(1)[0][0] if cats else "",
            duration_minutes=round(duration, 1),
            score=0,
        )
        self.flow_periods.append(period)

        if len(self.flow_periods) > 200:
            self.flow_periods = self.flow_periods[-100:]

    def get_attention_profile(self, hours: int = 8) -> AttentionProfile:
        """Generate a comprehensive attention profile."""
        now = time.time()
        cutoff = now - (hours * 3600)

        recent = [c for c in self.captures if c["timestamp"] > cutoff]

        if not recent:
            return AttentionProfile(
                avg_flow_score=0, peak_focus_hour=9, worst_focus_hour=14,
                avg_session_before_switch=0, top_flow_apps=[], top_distraction_apps=[],
                flow_periods_today=[], total_deep_focus_minutes=0,
                total_scattered_minutes=0,
            )

        # Hourly flow analysis
        hourly_scores = {}
        hourly_data = {}
        for c in recent:
            hour = datetime.fromtimestamp(c["timestamp"]).hour
            if hour not in hourly_data:
                hourly_data[hour] = []
            hourly_data[hour].append(c)

        for hour, captures in hourly_data.items():
            if len(captures) < 3:
                continue
            apps = Counter(c["app"] for c in captures)
            total = len(captures)
            top_count = apps.most_common(1)[0][1] if apps else 0
            concentration = top_count / total

            switches = sum(1 for i in range(1, len(captures)) if captures[i]["app"] != captures[i-1]["app"])
            switch_rate = switches / max(1, total)

            hourly_scores[hour] = concentration * 0.6 + (1 - switch_rate) * 0.4

        peak_hour = max(hourly_scores, key=hourly_scores.get) if hourly_scores else 9
        worst_hour = min(hourly_scores, key=hourly_scores.get) if hourly_scores else 14

        # App flow contribution
        app_sessions = {}
        current_app = recent[0]["app"]
        session_start = recent[0]["timestamp"]

        for c in recent[1:]:
            if c["app"] != current_app:
                duration = (c["timestamp"] - session_start) / 60
                if current_app not in app_sessions:
                    app_sessions[current_app] = []
                app_sessions[current_app].append(duration)
                current_app = c["app"]
                session_start = c["timestamp"]

        # Apps with longest average sessions = flow apps
        app_avg = {}
        for app, sessions in app_sessions.items():
            if len(sessions) >= 2:
                app_avg[app] = sum(sessions) / len(sessions)

        sorted_apps = sorted(app_avg.items(), key=lambda x: -x[1])
        flow_apps = [a for a, _ in sorted_apps[:5]]
        distraction_apps = [a for a, _ in sorted_apps[-5:]] if len(sorted_apps) > 5 else []

        # Overall avg session length
        all_sessions = [s for sessions in app_sessions.values() for s in sessions]
        avg_session = sum(all_sessions) / len(all_sessions) if all_sessions else 0

        # Flow period totals
        today_periods = [p for p in self.flow_periods if p.start > cutoff]
        deep_minutes = sum(p.duration_minutes for p in today_periods if p.state in ("deep_focus", "flow"))
        scattered_minutes = sum(p.duration_minutes for p in today_periods if p.state in ("scattered", "shallow"))

        # Average flow score
        if hourly_scores:
            avg_score = sum(hourly_scores.values()) / len(hourly_scores)
        else:
            avg_score = 0

        return AttentionProfile(
            avg_flow_score=round(avg_score, 2),
            peak_focus_hour=peak_hour,
            worst_focus_hour=worst_hour,
            avg_session_before_switch=round(avg_session, 1),
            top_flow_apps=flow_apps,
            top_distraction_apps=distraction_apps,
            flow_periods_today=today_periods,
            total_deep_focus_minutes=round(deep_minutes, 1),
            total_scattered_minutes=round(scattered_minutes, 1),
        )

    def detect_flow_breakers(self, minutes: int = 60) -> list[dict]:
        """
        Identify what broke flow states in the recent past.
        Returns a list of flow-breaking events (the app switch that
        ended a deep focus period).
        """
        now = time.time()
        cutoff = now - (minutes * 60)
        recent = [c for c in self.captures if c["timestamp"] > cutoff]

        if len(recent) < 5:
            return []

        breakers = []
        # Find transitions from long single-app runs to something else
        run_start = recent[0]["timestamp"]
        run_app = recent[0]["app"]
        run_length = 1

        for c in recent[1:]:
            if c["app"] == run_app:
                run_length += 1
            else:
                run_duration = (c["timestamp"] - run_start) / 60
                if run_duration >= 5 and run_length >= 5:
                    # This was a focus period that got broken
                    breakers.append({
                        "broken_app": run_app,
                        "broken_duration_min": round(run_duration, 1),
                        "breaker_app": c["app"],
                        "timestamp": c["timestamp"],
                    })
                run_start = c["timestamp"]
                run_app = c["app"]
                run_length = 1

        return breakers[-10:]
