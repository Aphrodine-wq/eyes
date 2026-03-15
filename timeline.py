"""
timeline.py — Screen diff narratives and rich timeline reconstruction.

Instead of raw OCR dumps, generates human-readable narratives of what
CHANGED between frames. "A new Slack message appeared from @john about
the API bug" instead of two walls of OCR text.

Also reconstructs a rich timeline suitable for visualization.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional


@dataclass
class ScreenDiff:
    """What changed between two consecutive captures."""
    timestamp: float
    app_name: str
    diff_type: str          # "new_content", "navigation", "app_switch", "minor_change", "notification"
    summary: str            # human-readable 1-line summary
    added_lines: list[str]  # new text that appeared
    removed_lines: list[str]
    change_magnitude: float # 0.0 (identical) to 1.0 (completely different)


@dataclass
class TimelineEvent:
    """A single event on the rich timeline."""
    timestamp: float
    event_type: str         # "app_open", "app_close", "focus_start", "focus_break",
                            # "content_change", "notification", "search", "error"
    app_name: str
    description: str
    duration_seconds: float = 0
    importance: float = 0.5  # 0.0 (trivial) to 1.0 (significant)
    metadata: dict = field(default_factory=dict)


@dataclass
class Timeline:
    """A complete reconstructed timeline."""
    events: list[TimelineEvent]
    start: float
    end: float
    total_events: int
    apps_used: list[str]

    def render(self, max_events: int = 30) -> str:
        """Render timeline as formatted text."""
        if not self.events:
            return "No events in timeline."

        # Filter to most important events
        sorted_events = sorted(self.events, key=lambda e: -e.importance)
        top_events = sorted(sorted_events[:max_events], key=lambda e: e.timestamp)

        lines = [
            f"Timeline: {_ts(self.start)} - {_ts(self.end)} "
            f"({len(self.events)} events, showing top {len(top_events)})",
            "",
        ]

        prev_ts = None
        for event in top_events:
            ts = _ts(event.timestamp)

            # Show time gap if significant
            if prev_ts and (event.timestamp - prev_ts) > 300:
                gap_min = (event.timestamp - prev_ts) / 60
                lines.append(f"         ... {gap_min:.0f}min gap ...")

            icon = _event_icon(event.event_type)
            dur = f" ({event.duration_seconds/60:.0f}min)" if event.duration_seconds > 60 else ""
            lines.append(f"  {ts} {icon} {event.description}{dur}")
            prev_ts = event.timestamp

        return "\n".join(lines)


def compute_screen_diff(prev_text: str, curr_text: str,
                        prev_app: str, curr_app: str,
                        curr_window: str, timestamp: float) -> ScreenDiff:
    """
    Compute a meaningful diff between two consecutive screen captures.
    Returns a human-readable summary of what changed.
    """
    # App switch
    if prev_app != curr_app:
        return ScreenDiff(
            timestamp=timestamp,
            app_name=curr_app,
            diff_type="app_switch",
            summary=f"Switched from {prev_app} to {curr_app}" +
                    (f" ({curr_window})" if curr_window else ""),
            added_lines=[],
            removed_lines=[],
            change_magnitude=1.0,
        )

    # Compute text diff
    prev_lines = set(prev_text.strip().split("\n")) if prev_text else set()
    curr_lines = set(curr_text.strip().split("\n")) if curr_text else set()

    added = curr_lines - prev_lines
    removed = prev_lines - curr_lines

    # Filter noise (very short lines, whitespace)
    added = [l.strip() for l in added if len(l.strip()) > 10]
    removed = [l.strip() for l in removed if len(l.strip()) > 10]

    # Calculate change magnitude
    total = max(len(prev_lines | curr_lines), 1)
    changed = len(added) + len(removed)
    magnitude = min(1.0, changed / total)

    if magnitude < 0.05 and not added:
        return ScreenDiff(
            timestamp=timestamp, app_name=curr_app,
            diff_type="minor_change", summary=f"Minor changes in {curr_app}",
            added_lines=[], removed_lines=[],
            change_magnitude=magnitude,
        )

    # Detect specific change types
    diff_type, summary = _classify_diff(curr_app, curr_window, added, removed, magnitude)

    return ScreenDiff(
        timestamp=timestamp,
        app_name=curr_app,
        diff_type=diff_type,
        summary=summary,
        added_lines=added[:10],
        removed_lines=removed[:10],
        change_magnitude=round(magnitude, 2),
    )


def build_timeline(entries: list, include_minor: bool = False) -> Timeline:
    """
    Build a rich timeline from a sequence of screen captures.
    Compresses consecutive same-app captures into duration events.
    Highlights significant changes, errors, and transitions.
    """
    if not entries:
        return Timeline(events=[], start=0, end=0, total_events=0, apps_used=[])

    events = []
    prev_entry = None
    app_start = entries[0].timestamp if entries else 0
    current_app = entries[0].app_name if entries else ""

    for entry in entries:
        # Detect app transitions
        if entry.app_name != current_app:
            # Close previous app session
            duration = entry.timestamp - app_start
            if duration > 30:  # only log sessions > 30s
                events.append(TimelineEvent(
                    timestamp=app_start,
                    event_type="app_open",
                    app_name=current_app,
                    description=f"Used {current_app}",
                    duration_seconds=duration,
                    importance=min(1.0, duration / 600),  # 10min = max importance
                ))

            # Start new app
            current_app = entry.app_name
            app_start = entry.timestamp

            events.append(TimelineEvent(
                timestamp=entry.timestamp,
                event_type="app_switch",
                app_name=entry.app_name,
                description=f"Switched to {entry.app_name}" +
                           (f" — {entry.window_title}" if entry.window_title else ""),
                importance=0.6,
            ))

        # Detect significant content changes
        if prev_entry and entry.app_name == prev_entry.app_name:
            diff = compute_screen_diff(
                prev_entry.text, entry.text,
                prev_entry.app_name, entry.app_name,
                entry.window_title, entry.timestamp,
            )

            if diff.diff_type == "notification":
                events.append(TimelineEvent(
                    timestamp=entry.timestamp,
                    event_type="notification",
                    app_name=entry.app_name,
                    description=diff.summary,
                    importance=0.7,
                ))
            elif diff.diff_type == "error_appeared":
                events.append(TimelineEvent(
                    timestamp=entry.timestamp,
                    event_type="error",
                    app_name=entry.app_name,
                    description=diff.summary,
                    importance=0.9,
                    metadata={"added": diff.added_lines[:3]},
                ))
            elif diff.diff_type == "new_content" and diff.change_magnitude > 0.3:
                events.append(TimelineEvent(
                    timestamp=entry.timestamp,
                    event_type="content_change",
                    app_name=entry.app_name,
                    description=diff.summary,
                    importance=0.4 + diff.change_magnitude * 0.4,
                ))
            elif include_minor and diff.change_magnitude > 0.1:
                events.append(TimelineEvent(
                    timestamp=entry.timestamp,
                    event_type="content_change",
                    app_name=entry.app_name,
                    description=diff.summary,
                    importance=0.2,
                ))

        prev_entry = entry

    # Close final app session
    if entries:
        duration = entries[-1].timestamp - app_start
        if duration > 30:
            events.append(TimelineEvent(
                timestamp=app_start,
                event_type="app_open",
                app_name=current_app,
                description=f"Used {current_app}",
                duration_seconds=duration,
                importance=min(1.0, duration / 600),
            ))

    apps = list(dict.fromkeys(e.app_name for e in events))

    return Timeline(
        events=events,
        start=entries[0].timestamp if entries else 0,
        end=entries[-1].timestamp if entries else 0,
        total_events=len(events),
        apps_used=apps,
    )


def _classify_diff(app: str, window: str, added: list, removed: list, magnitude: float) -> tuple[str, str]:
    """Classify a diff into a type and generate a summary."""
    added_text = " ".join(added).lower()

    # Error detection
    error_patterns = [
        r'error', r'exception', r'failed', r'fatal', r'crash',
        r'traceback', r'panic', r'segfault', r'errno',
    ]
    for pattern in error_patterns:
        if re.search(pattern, added_text):
            error_line = next((l for l in added if re.search(pattern, l, re.I)), added[0] if added else "")
            return "error_appeared", f"Error in {app}: {error_line[:80]}"

    # Notification detection
    notif_patterns = [r'notification', r'new message', r'@\w+', r'mentioned you']
    if any(re.search(p, added_text) for p in notif_patterns):
        preview = added[0][:60] if added else ""
        return "notification", f"Notification in {app}: {preview}"

    # Navigation (window title change typically)
    if magnitude > 0.7:
        if window:
            return "navigation", f"Navigated in {app} to {window[:60]}"
        return "new_content", f"Major content change in {app} ({len(added)} new lines)"

    # Standard content change
    if added:
        preview = added[0][:60]
        return "new_content", f"New content in {app}: {preview}"

    return "minor_change", f"Minor update in {app}"


def _ts(timestamp: float) -> str:
    """Format timestamp as HH:MM."""
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def _event_icon(event_type: str) -> str:
    """Get a text icon for event type."""
    return {
        "app_open": "[+]",
        "app_close": "[-]",
        "app_switch": "[>]",
        "content_change": "[~]",
        "notification": "[!]",
        "error": "[X]",
        "search": "[?]",
        "focus_start": "[*]",
        "focus_break": "[/]",
    }.get(event_type, "[ ]")
