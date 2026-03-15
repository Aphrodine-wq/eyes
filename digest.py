"""
digest.py — Daily and weekly digest generation.

Generates structured reports of screen activity over longer periods.
Designed to give Claude (or the user) a high-level view of
how time was spent.
"""

import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import Optional

from store import EyesStore, load_config
from classifier import classify_capture, classify_batch


def generate_daily_digest(store: EyesStore, date: Optional[datetime] = None) -> str:
    """
    Generate a daily activity digest.

    Returns a structured text report covering:
    - Total active time and session count
    - Hourly activity heatmap
    - Category breakdown (code, chat, browser, etc.)
    - Top apps and windows
    - Productivity score
    - Key topics/keywords detected
    """
    if date is None:
        date = datetime.now()

    day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    entries = store.get_by_time_range(
        day_start.timestamp(),
        min(day_end.timestamp(), time.time()),
        limit=10000,
    )

    if not entries:
        return f"No activity recorded for {day_start.strftime('%Y-%m-%d')}."

    config = load_config()
    interval = config.get("capture_interval", 10)

    # === Hourly heatmap ===
    hourly = defaultdict(int)
    for e in entries:
        hour = datetime.fromtimestamp(e.timestamp).hour
        hourly[hour] += 1

    # === App breakdown ===
    app_counts = Counter(e.app_name for e in entries)

    # === Category classification ===
    categories = classify_batch(entries)

    # === Session detection ===
    sessions = store.get_sessions(hours=24)
    today_sessions = [
        s for s in sessions
        if s.start >= day_start.timestamp()
    ]

    # === Top windows ===
    window_counts = Counter(
        e.window_title for e in entries if e.window_title
    )

    # === Build report ===
    total_frames = len(entries)
    active_minutes = round((total_frames * interval) / 60, 1)
    active_hours = round(active_minutes / 60, 1)
    date_str = day_start.strftime("%A, %B %d, %Y")

    lines = [
        f"Daily Digest: {date_str}",
        "=" * 50,
        "",
        f"Active time: ~{active_hours}h ({active_minutes} min, {total_frames} captures)",
        f"Sessions: {len(today_sessions)}",
        "",
    ]

    # Hourly heatmap
    lines.append("Hourly Activity:")
    max_hour_count = max(hourly.values()) if hourly else 1
    for hour in range(6, 24):  # 6 AM to midnight
        count = hourly.get(hour, 0)
        if max_hour_count > 0:
            bar_len = int((count / max_hour_count) * 20)
        else:
            bar_len = 0
        bar = "█" * bar_len
        est_min = round((count * interval) / 60, 0)
        label = f"{hour:02d}:00"
        if count > 0:
            lines.append(f"  {label} {bar:20s} {est_min:.0f}min")
        else:
            lines.append(f"  {label} {'·':20s}")
    lines.append("")

    # Category breakdown
    lines.append("Content Categories:")
    total_cat = sum(c["count"] for c in categories.values())
    for cat, info in sorted(categories.items(), key=lambda x: -x[1]["count"]):
        pct = round((info["count"] / total_cat) * 100, 1)
        est_min = round((info["count"] * interval) / 60, 1)
        prod_marker = " [productive]" if info["productive_frames"] > info["count"] * 0.5 else ""
        lines.append(f"  {cat:12s} {pct:5.1f}% (~{est_min}min){prod_marker}")

        # Show subcategories if interesting
        subs = info.get("subcategories", {})
        if len(subs) > 1:
            for sub, sub_count in sorted(subs.items(), key=lambda x: -x[1])[:3]:
                if sub:
                    lines.append(f"    └ {sub}: {sub_count} frames")

        # Show top keywords
        top_kw = info.get("top_keywords", [])
        if top_kw:
            lines.append(f"    keywords: {', '.join(top_kw[:5])}")
    lines.append("")

    # Productivity score
    productive_frames = sum(
        info["productive_frames"] for info in categories.values()
    )
    productivity_pct = round((productive_frames / total_frames) * 100, 1) if total_frames else 0
    lines.append(f"Productivity: {productivity_pct}% ({productive_frames}/{total_frames} frames classified as productive)")
    lines.append("")

    # Top apps
    lines.append("Top Apps:")
    for app, count in app_counts.most_common(8):
        est_min = round((count * interval) / 60, 1)
        pct = round((count / total_frames) * 100, 1)
        lines.append(f"  {app:25s} ~{est_min}min ({pct}%)")
    lines.append("")

    # Top windows
    lines.append("Top Windows:")
    for window, count in window_counts.most_common(8):
        if window:
            est_min = round((count * interval) / 60, 1)
            truncated = window[:50] + "..." if len(window) > 50 else window
            lines.append(f"  {truncated:55s} ~{est_min}min")
    lines.append("")

    # Sessions
    if today_sessions:
        lines.append("Sessions:")
        for i, s in enumerate(today_sessions, 1):
            start_str = datetime.fromtimestamp(s.start).strftime("%H:%M")
            end_str = datetime.fromtimestamp(s.end).strftime("%H:%M")
            lines.append(f"  {i}. {start_str}-{end_str} ({s.duration_minutes}min) — {s.top_app}")
        lines.append("")

    return "\n".join(lines)


def generate_weekly_digest(store: EyesStore) -> str:
    """
    Generate a weekly activity digest.
    Compares days and shows trends.
    """
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    config = load_config()
    interval = config.get("capture_interval", 10)

    lines = [
        f"Weekly Digest: {(today - timedelta(days=6)).strftime('%b %d')} - {today.strftime('%b %d, %Y')}",
        "=" * 50,
        "",
    ]

    daily_stats = []
    for days_ago in range(6, -1, -1):
        day = today - timedelta(days=days_ago)
        day_end = day + timedelta(days=1)
        entries = store.get_by_time_range(
            day.timestamp(),
            min(day_end.timestamp(), time.time()),
            limit=10000,
        )

        total = len(entries)
        active_min = round((total * interval) / 60, 1)

        if entries:
            categories = classify_batch(entries)
            productive = sum(c["productive_frames"] for c in categories.values())
            prod_pct = round((productive / total) * 100, 1) if total else 0
            apps = Counter(e.app_name for e in entries)
            top_app = apps.most_common(1)[0][0] if apps else "N/A"
        else:
            prod_pct = 0
            top_app = "N/A"

        daily_stats.append({
            "date": day,
            "frames": total,
            "active_min": active_min,
            "productivity": prod_pct,
            "top_app": top_app,
        })

    # Daily comparison
    lines.append("Daily Breakdown:")
    lines.append(f"  {'Day':<12s} {'Active':>8s} {'Prod%':>6s} {'Top App':<20s}")
    lines.append(f"  {'---':<12s} {'------':>8s} {'-----':>6s} {'-------':<20s}")

    for ds in daily_stats:
        day_label = ds["date"].strftime("%a %m/%d")
        active = f"{ds['active_min']:.0f}min"
        prod = f"{ds['productivity']:.0f}%"
        lines.append(f"  {day_label:<12s} {active:>8s} {prod:>6s} {ds['top_app']:<20s}")
    lines.append("")

    # Totals
    total_min = sum(d["active_min"] for d in daily_stats)
    avg_min = total_min / 7
    avg_prod = sum(d["productivity"] for d in daily_stats) / max(1, sum(1 for d in daily_stats if d["frames"] > 0))
    lines.append(f"Total: ~{total_min:.0f}min ({total_min/60:.1f}h)")
    lines.append(f"Daily average: ~{avg_min:.0f}min")
    lines.append(f"Avg productivity: {avg_prod:.0f}%")

    return "\n".join(lines)


def compare_days(store: EyesStore, day1: datetime, day2: datetime) -> str:
    """Compare activity between two specific days."""
    config = load_config()
    interval = config.get("capture_interval", 10)

    results = []
    for day in [day1, day2]:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        entries = store.get_by_time_range(
            day_start.timestamp(),
            min(day_end.timestamp(), time.time()),
            limit=10000,
        )
        categories = classify_batch(entries) if entries else {}
        apps = Counter(e.app_name for e in entries)

        productive = sum(c["productive_frames"] for c in categories.values()) if categories else 0
        prod_pct = round((productive / len(entries)) * 100, 1) if entries else 0

        results.append({
            "date": day_start,
            "frames": len(entries),
            "active_min": round((len(entries) * interval) / 60, 1),
            "categories": categories,
            "top_apps": apps.most_common(5),
            "productivity": prod_pct,
        })

    d1, d2 = results
    lines = [
        f"Day Comparison: {d1['date'].strftime('%b %d')} vs {d2['date'].strftime('%b %d')}",
        "=" * 50,
        "",
        f"  {'Metric':<20s} {d1['date'].strftime('%a'):>10s} {d2['date'].strftime('%a'):>10s} {'Delta':>10s}",
        f"  {'------':<20s} {'---':>10s} {'---':>10s} {'-----':>10s}",
    ]

    # Active time
    delta_min = d2["active_min"] - d1["active_min"]
    sign = "+" if delta_min >= 0 else ""
    lines.append(f"  {'Active time':<20s} {d1['active_min']:>8.0f}m {d2['active_min']:>8.0f}m {sign}{delta_min:>8.0f}m")

    # Productivity
    delta_prod = d2["productivity"] - d1["productivity"]
    sign = "+" if delta_prod >= 0 else ""
    lines.append(f"  {'Productivity':<20s} {d1['productivity']:>9.0f}% {d2['productivity']:>9.0f}% {sign}{delta_prod:>8.0f}%")

    # Captures
    delta_frames = d2["frames"] - d1["frames"]
    sign = "+" if delta_frames >= 0 else ""
    lines.append(f"  {'Captures':<20s} {d1['frames']:>10d} {d2['frames']:>10d} {sign}{delta_frames:>10d}")

    return "\n".join(lines)
