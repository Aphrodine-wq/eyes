"""
patterns.py — Temporal pattern learning and behavioral analysis.

Learns your recurring workflows, detects anomalies, and predicts
what you'll do next based on historical patterns.

This is the module that makes Claude feel prescient.
"""

import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from store import EyesStore, load_config


@dataclass
class WorkflowFingerprint:
    """A detected recurring workflow pattern."""
    name: str                   # auto-generated name
    app_sequence: list[str]     # e.g., ["Slack", "Chrome", "VS Code"]
    typical_duration_min: float
    occurrences: int
    last_seen: float
    time_of_day: str            # "morning", "afternoon", "evening"
    confidence: float


@dataclass
class BehavioralAnomaly:
    """A detected deviation from normal patterns."""
    description: str
    severity: str              # "info", "notable", "unusual", "significant"
    metric: str                # what was measured
    expected_value: str
    actual_value: str
    timestamp: float


@dataclass
class Prediction:
    """A predicted next action."""
    predicted_app: str
    confidence: float
    reasoning: str
    alternatives: list[tuple[str, float]]  # (app, confidence)


class PatternEngine:
    """
    Learns patterns from screen capture history.
    Operates on stored data — no real-time state needed.
    """

    def detect_workflows(self, store: EyesStore, days: int = 7) -> list[WorkflowFingerprint]:
        """
        Detect recurring app-transition workflows.
        A workflow is a sequence of 3+ apps that appears multiple times.
        """
        # Get all captures from the period
        cutoff = time.time() - (days * 86400)
        rows = store.conn.execute(
            "SELECT timestamp, app_name FROM frames "
            "WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if len(rows) < 20:
            return []

        config = load_config()
        gap_threshold = config.get("session_gap_minutes", 5) * 60

        # Extract app transition sequences (within sessions)
        sequences = []
        current_seq = [rows[0][1]]
        prev_ts = rows[0][0]
        prev_app = rows[0][1]

        for ts, app in rows[1:]:
            if ts - prev_ts > gap_threshold:
                # Session break — save current sequence, start new one
                if len(current_seq) >= 3:
                    sequences.append((current_seq, prev_ts))
                current_seq = [app]
            elif app != prev_app:
                current_seq.append(app)
            prev_ts = ts
            prev_app = app

        if len(current_seq) >= 3:
            sequences.append((current_seq, prev_ts))

        # Find recurring subsequences of length 3-5
        subseq_counts = Counter()
        subseq_times = defaultdict(list)

        for seq, last_ts in sequences:
            for length in range(3, min(6, len(seq) + 1)):
                for i in range(len(seq) - length + 1):
                    sub = tuple(seq[i:i + length])
                    subseq_counts[sub] += 1
                    subseq_times[sub].append(last_ts)

        # Filter to sequences that appear 2+ times
        workflows = []
        seen_prefixes = set()

        for seq, count in subseq_counts.most_common(20):
            if count < 2:
                continue

            # Deduplicate — skip if this is a subset of an already-found workflow
            seq_set = frozenset(enumerate(seq))
            is_subset = False
            for prefix in seen_prefixes:
                if len(prefix) > len(seq_set) and seq_set.issubset(prefix):
                    is_subset = True
                    break
            if is_subset:
                continue
            seen_prefixes.add(seq_set)

            # Determine typical time of day
            times = subseq_times[seq]
            hours = [datetime.fromtimestamp(t).hour for t in times]
            avg_hour = sum(hours) / len(hours)

            if avg_hour < 12:
                tod = "morning"
            elif avg_hour < 17:
                tod = "afternoon"
            else:
                tod = "evening"

            # Generate a name
            name = _generate_workflow_name(list(seq))

            workflows.append(WorkflowFingerprint(
                name=name,
                app_sequence=list(seq),
                typical_duration_min=round(len(seq) * 3, 1),  # rough estimate
                occurrences=count,
                last_seen=max(times),
                time_of_day=tod,
                confidence=min(1.0, count * 0.15 + 0.2),
            ))

        return workflows

    def detect_anomalies(self, store: EyesStore) -> list[BehavioralAnomaly]:
        """
        Compare today's behavior to the 7-day average.
        Flag anything that's significantly different.
        """
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        config = load_config()
        interval = config.get("capture_interval", 10)
        anomalies = []

        # Get today's data
        today_rows = store.conn.execute(
            "SELECT timestamp, app_name FROM frames WHERE timestamp > ?",
            (today_start.timestamp(),)
        ).fetchall()

        if not today_rows:
            return []

        # Get 7-day baseline (excluding today)
        week_start = today_start - timedelta(days=7)
        baseline_rows = store.conn.execute(
            "SELECT timestamp, app_name FROM frames "
            "WHERE timestamp > ? AND timestamp < ?",
            (week_start.timestamp(), today_start.timestamp())
        ).fetchall()

        if len(baseline_rows) < 50:
            return []  # not enough baseline data

        # Metric 1: Total activity
        today_count = len(today_rows)
        hours_elapsed = max((now - today_start).seconds / 3600, 1)
        today_rate = today_count / hours_elapsed

        baseline_daily = len(baseline_rows) / 7
        baseline_rate = baseline_daily / 10  # assume ~10 active hours

        if today_rate > baseline_rate * 1.5:
            anomalies.append(BehavioralAnomaly(
                description=f"Unusually high screen activity today ({today_rate:.0f}/hr vs avg {baseline_rate:.0f}/hr)",
                severity="notable",
                metric="capture_rate",
                expected_value=f"~{baseline_rate:.0f} captures/hr",
                actual_value=f"{today_rate:.0f} captures/hr",
                timestamp=time.time(),
            ))
        elif today_rate < baseline_rate * 0.5 and hours_elapsed > 2:
            anomalies.append(BehavioralAnomaly(
                description=f"Unusually low activity today ({today_rate:.0f}/hr vs avg {baseline_rate:.0f}/hr)",
                severity="info",
                metric="capture_rate",
                expected_value=f"~{baseline_rate:.0f} captures/hr",
                actual_value=f"{today_rate:.0f} captures/hr",
                timestamp=time.time(),
            ))

        # Metric 2: App distribution anomalies
        today_apps = Counter(r[1] for r in today_rows)
        baseline_apps = Counter(r[1] for r in baseline_rows)

        baseline_total = sum(baseline_apps.values())
        today_total = sum(today_apps.values())

        for app, today_count in today_apps.most_common(5):
            today_pct = (today_count / today_total) * 100
            baseline_pct = (baseline_apps.get(app, 0) / baseline_total) * 100

            if today_pct > baseline_pct * 2 and today_pct > 15:
                est_min = round((today_count * interval) / 60)
                anomalies.append(BehavioralAnomaly(
                    description=f"Spending {today_pct:.0f}% of time in {app} (normally {baseline_pct:.0f}%) — ~{est_min}min today",
                    severity="notable",
                    metric="app_usage",
                    expected_value=f"~{baseline_pct:.0f}%",
                    actual_value=f"{today_pct:.0f}%",
                    timestamp=time.time(),
                ))

        # Metric 3: Context switching rate
        today_switches = sum(1 for i in range(1, len(today_rows)) if today_rows[i][1] != today_rows[i-1][1])
        today_switch_rate = today_switches / max(hours_elapsed, 1)

        baseline_switches = sum(1 for i in range(1, len(baseline_rows)) if baseline_rows[i][1] != baseline_rows[i-1][1])
        baseline_switch_rate = baseline_switches / max(7 * 10, 1)  # per hour over 7 days

        if today_switch_rate > baseline_switch_rate * 2:
            anomalies.append(BehavioralAnomaly(
                description=f"High context switching today ({today_switch_rate:.0f}/hr vs avg {baseline_switch_rate:.0f}/hr)",
                severity="notable",
                metric="switch_rate",
                expected_value=f"~{baseline_switch_rate:.0f} switches/hr",
                actual_value=f"{today_switch_rate:.0f} switches/hr",
                timestamp=time.time(),
            ))

        # Metric 4: New app (never/rarely seen before)
        for app in today_apps:
            if app not in baseline_apps and today_apps[app] >= 5:
                anomalies.append(BehavioralAnomaly(
                    description=f"New app detected: {app} (not seen in last 7 days, {today_apps[app]} captures today)",
                    severity="info",
                    metric="new_app",
                    expected_value="not present",
                    actual_value=f"{today_apps[app]} captures",
                    timestamp=time.time(),
                ))

        return anomalies

    def predict_next_app(self, store: EyesStore) -> Prediction:
        """
        Predict what app the user will likely switch to next,
        based on historical transition probabilities and current context.
        """
        # Get recent captures for current context
        recent = store.get_recent(minutes=5, limit=20)
        if not recent:
            return Prediction("", 0, "No recent activity", [])

        current_app = recent[0].app_name
        current_hour = datetime.now().hour

        # Build transition probability matrix from last 7 days
        cutoff = time.time() - (7 * 86400)
        rows = store.conn.execute(
            "SELECT app_name FROM frames WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if len(rows) < 50:
            return Prediction("", 0, "Not enough history for prediction", [])

        # Count transitions from current app
        transitions = Counter()
        for i in range(len(rows) - 1):
            if rows[i][0] == current_app and rows[i + 1][0] != current_app:
                transitions[rows[i + 1][0]] += 1

        if not transitions:
            return Prediction("", 0, f"No recorded transitions from {current_app}", [])

        total = sum(transitions.values())
        ranked = transitions.most_common(5)

        predicted_app = ranked[0][0]
        confidence = ranked[0][1] / total

        alternatives = [(app, round(count / total, 2)) for app, count in ranked[1:4]]

        # Time-of-day boost
        hour_transitions = Counter()
        for i in range(len(rows) - 1):
            row_ts = store.conn.execute(
                "SELECT timestamp FROM frames WHERE app_name = ? LIMIT 1",
                (rows[i][0],)
            ).fetchone()
            if row_ts:
                row_hour = datetime.fromtimestamp(row_ts[0]).hour
                if abs(row_hour - current_hour) <= 1 and rows[i][0] == current_app:
                    if rows[i + 1][0] != current_app:
                        hour_transitions[rows[i + 1][0]] += 1

        # Blend time-of-day signal
        if hour_transitions:
            tod_top = hour_transitions.most_common(1)[0]
            if tod_top[0] != predicted_app and tod_top[1] >= 3:
                # Time-of-day pattern is different — note it
                alternatives.insert(0, (predicted_app, round(confidence, 2)))
                predicted_app = tod_top[0]
                confidence = min(1.0, confidence * 0.7 + 0.3)

        reasoning = f"Based on {total} historical transitions from {current_app}"
        if hour_transitions:
            reasoning += f" (time-of-day adjusted for {current_hour}:00)"

        return Prediction(
            predicted_app=predicted_app,
            confidence=round(confidence, 2),
            reasoning=reasoning,
            alternatives=alternatives,
        )


def _generate_workflow_name(apps: list[str]) -> str:
    """Generate a human-readable workflow name from an app sequence."""
    # Simplify app names
    short = []
    for app in apps:
        name = app.lower().replace("google ", "").replace("microsoft ", "")
        short.append(name)

    # Common patterns
    if any("slack" in s or "discord" in s for s in short):
        if any("code" in s or "xcode" in s or "cursor" in s for s in short):
            return "chat-to-code"
        if any("chrome" in s or "safari" in s or "arc" in s for s in short):
            return "chat-then-browse"

    if any("chrome" in s or "safari" in s or "arc" in s for s in short):
        if any("code" in s or "xcode" in s or "cursor" in s for s in short):
            return "research-to-code"

    if any("iterm" in s or "terminal" in s or "warp" in s for s in short):
        if any("code" in s or "xcode" in s or "cursor" in s for s in short):
            return "code-and-run"

    if any("mail" in s or "outlook" in s or "spark" in s for s in short):
        return "email-workflow"

    if any("figma" in s or "sketch" in s for s in short):
        if any("code" in s or "xcode" in s for s in short):
            return "design-to-code"

    # Fallback: first and last app
    return f"{apps[0].lower()}-to-{apps[-1].lower()}"
