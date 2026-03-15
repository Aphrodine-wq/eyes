"""
export.py — Multi-format export engine for Claude Eyes.

Generates reports from screen capture data as JSON, CSV, Markdown,
or a self-contained HTML dashboard with pure CSS charts.
"""

import csv
import io
import json
import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from store import EyesStore, load_config
from classifier import classify_capture, classify_batch
from digest import generate_daily_digest
from flow import FlowDetector


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_json(store: EyesStore, minutes: int = 60, path: Optional[str] = None) -> str:
    """
    Export recent captures as structured JSON.

    Each entry includes timestamp, app, window title, text preview,
    classification, and flow metadata.  Returns the JSON string and
    optionally writes it to *path*.
    """
    entries = store.get_recent(minutes=minutes, limit=5000)
    config = load_config()
    interval = config.get("capture_interval", 10)

    records = []
    for e in entries:
        c = classify_capture(e.app_name, e.window_title, e.text)
        records.append({
            "id": e.id,
            "timestamp": e.timestamp,
            "datetime": datetime.fromtimestamp(e.timestamp).isoformat(),
            "app_name": e.app_name,
            "window_title": e.window_title,
            "text_preview": e.text[:300] if e.text else "",
            "category": c.category,
            "subcategory": c.subcategory,
            "is_productive": c.is_productive,
            "keywords": c.keywords,
        })

    payload = {
        "export_time": datetime.now().isoformat(),
        "period_minutes": minutes,
        "capture_interval_sec": interval,
        "total_entries": len(records),
        "entries": records,
    }

    text = json.dumps(payload, indent=2, ensure_ascii=False)

    if path:
        Path(path).write_text(text, encoding="utf-8")

    return text


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(store: EyesStore, minutes: int = 60, path: Optional[str] = None) -> str:
    """
    Export recent captures as CSV.

    Columns: timestamp, datetime, app, window, text_preview, category
    Returns the CSV string and optionally writes it to *path*.
    """
    entries = store.get_recent(minutes=minutes, limit=5000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "datetime", "app", "window", "text_preview", "category"])

    for e in entries:
        c = classify_capture(e.app_name, e.window_title, e.text)
        writer.writerow([
            e.timestamp,
            datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            e.app_name,
            e.window_title,
            (e.text[:120].replace("\n", " ") if e.text else ""),
            c.category,
        ])

    text = buf.getvalue()

    if path:
        Path(path).write_text(text, encoding="utf-8")

    return text


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def export_markdown(store: EyesStore, minutes: int = 60) -> str:
    """
    Generate a Markdown activity report for the last *minutes*.

    Includes a summary table, category breakdown, timeline, and
    focus statistics.
    """
    entries = store.get_recent(minutes=minutes, limit=5000)

    if not entries:
        return f"# Claude Eyes Report\n\nNo activity in the last {minutes} minutes.\n"

    config = load_config()
    interval = config.get("capture_interval", 10)
    now = datetime.now()

    # Classify everything
    categories = classify_batch(entries)
    app_counts = Counter(e.app_name for e in entries)
    total = len(entries)
    active_min = round((total * interval) / 60, 1)

    productive_frames = sum(c["productive_frames"] for c in categories.values())
    prod_pct = round((productive_frames / total) * 100, 1) if total else 0

    lines = [
        f"# Claude Eyes Report",
        f"",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M')}  ",
        f"**Period:** last {minutes} min  ",
        f"**Active time:** ~{active_min} min ({total} captures)  ",
        f"**Productivity:** {prod_pct}%",
        "",
        "---",
        "",
        "## Category Breakdown",
        "",
        "| Category | Time (min) | % | Productive |",
        "|----------|-----------|---|------------|",
    ]

    total_cat = sum(c["count"] for c in categories.values())
    for cat, info in sorted(categories.items(), key=lambda x: -x[1]["count"]):
        pct = round((info["count"] / total_cat) * 100, 1)
        est_min = round((info["count"] * interval) / 60, 1)
        prod = "Yes" if info["productive_frames"] > info["count"] * 0.5 else "No"
        lines.append(f"| {cat} | {est_min} | {pct}% | {prod} |")

    lines += ["", "## Top Apps", "", "| App | Time (min) | % |", "|-----|-----------|---|"]

    for app, count in app_counts.most_common(10):
        est_min = round((count * interval) / 60, 1)
        pct = round((count / total) * 100, 1)
        lines.append(f"| {app} | {est_min} | {pct}% |")

    # Timeline (group consecutive same-app blocks)
    lines += ["", "## Timeline", ""]
    entries_asc = sorted(entries, key=lambda e: e.timestamp)
    if entries_asc:
        seg_app = entries_asc[0].app_name
        seg_start = entries_asc[0].timestamp
        seg_count = 1

        for e in entries_asc[1:]:
            if e.app_name == seg_app:
                seg_count += 1
            else:
                start_s = datetime.fromtimestamp(seg_start).strftime("%H:%M")
                est = round((seg_count * interval) / 60, 1)
                lines.append(f"- **{start_s}** {seg_app} (~{est} min)")
                seg_app = e.app_name
                seg_start = e.timestamp
                seg_count = 1

        start_s = datetime.fromtimestamp(seg_start).strftime("%H:%M")
        est = round((seg_count * interval) / 60, 1)
        lines.append(f"- **{start_s}** {seg_app} (~{est} min)")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML dashboard export
# ---------------------------------------------------------------------------

def export_html_dashboard(store: EyesStore, hours: int = 8) -> str:
    """
    Generate a single self-contained HTML file with a dark-themed
    activity dashboard.  Pure CSS charts, zero JS dependencies.

    Returns the full HTML string.
    """
    minutes = hours * 60
    entries = store.get_recent(minutes=minutes, limit=10000)
    config = load_config()
    interval = config.get("capture_interval", 10)
    now = datetime.now()

    total = len(entries)
    active_min = round((total * interval) / 60, 1)
    active_hrs = round(active_min / 60, 1)

    # --- data aggregation ---
    categories = classify_batch(entries) if entries else {}
    app_counts = Counter(e.app_name for e in entries)
    total_cat = sum(c["count"] for c in categories.values()) or 1

    productive_frames = sum(c["productive_frames"] for c in categories.values())
    prod_pct = round((productive_frames / total) * 100, 1) if total else 0

    # Hourly heatmap data (last N hours)
    hourly = defaultdict(int)
    for e in entries:
        h = datetime.fromtimestamp(e.timestamp).hour
        hourly[h] += 1
    max_hourly = max(hourly.values()) if hourly else 1

    # Focus score (simplified)
    focus_stats = store.get_focus_stats(minutes=minutes)
    switches = focus_stats.get("switches", 0)
    switches_per_hr = round(switches / max(hours, 1), 1)
    focus_score = max(0, min(100, int(prod_pct * 0.6 + max(0, 100 - switches_per_hr * 5) * 0.4)))

    # --- build HTML pieces ---

    # Heatmap cells
    heatmap_cells = ""
    for h in range(24):
        count = hourly.get(h, 0)
        intensity = count / max_hourly if max_hourly else 0
        if intensity == 0:
            bg = "#151e2d"
        elif intensity < 0.25:
            bg = "#0f2a3d"
        elif intensity < 0.5:
            bg = "#133a52"
        elif intensity < 0.75:
            bg = "#1a5a7a"
        else:
            bg = "#36d2ff"
        est = round((count * interval) / 60, 1)
        label = f"{h:02d}"
        heatmap_cells += (
            f'<div class="heat-cell" style="background:{bg}" '
            f'title="{label}:00 — {est} min">'
            f'<span class="heat-label">{label}</span></div>\n'
        )

    # Pie chart via conic-gradient
    pie_segments = []
    cum = 0.0
    palette = ["#36d2ff", "#ff6b6b", "#ffd93d", "#6bff9e", "#c084fc",
               "#ff9f43", "#54a0ff", "#ee5a24", "#7ed6df", "#a29bfe"]
    cat_list = sorted(categories.items(), key=lambda x: -x[1]["count"])
    legend_items = ""
    for i, (cat, info) in enumerate(cat_list):
        pct = (info["count"] / total_cat) * 100
        color = palette[i % len(palette)]
        start = cum
        cum += pct
        pie_segments.append(f"{color} {start:.1f}% {cum:.1f}%")
        est_min = round((info["count"] * interval) / 60, 1)
        legend_items += (
            f'<div class="legend-item">'
            f'<span class="legend-dot" style="background:{color}"></span>'
            f'{cat} ({pct:.0f}% / {est_min}m)</div>\n'
        )

    conic = ", ".join(pie_segments) if pie_segments else "#151e2d 0% 100%"

    # App timeline (top 8)
    app_bars = ""
    top_apps = app_counts.most_common(8)
    top_app_max = top_apps[0][1] if top_apps else 1
    for app, count in top_apps:
        width_pct = (count / top_app_max) * 100
        est = round((count * interval) / 60, 1)
        pct_of_total = round((count / total) * 100, 1) if total else 0
        app_bars += (
            f'<div class="bar-row">'
            f'<span class="bar-label">{_esc(app)}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width_pct:.0f}%"></div></div>'
            f'<span class="bar-value">{est}m ({pct_of_total}%)</span>'
            f'</div>\n'
        )

    # Focus gauge
    gauge_angle = focus_score * 1.8  # 0-180 degrees

    # Timeline entries (group consecutive)
    timeline_html = ""
    if entries:
        entries_asc = sorted(entries, key=lambda e: e.timestamp)
        seg_app = entries_asc[0].app_name
        seg_start = entries_asc[0].timestamp
        seg_count = 1
        segments = []

        for e in entries_asc[1:]:
            if e.app_name == seg_app:
                seg_count += 1
            else:
                segments.append((seg_start, seg_app, seg_count))
                seg_app = e.app_name
                seg_start = e.timestamp
                seg_count = 1
        segments.append((seg_start, seg_app, seg_count))

        for ts, app, cnt in segments[-20:]:
            t = datetime.fromtimestamp(ts).strftime("%H:%M")
            est = round((cnt * interval) / 60, 1)
            c = classify_capture(app, "", "")
            dot_color = palette[list(categories.keys()).index(c.category) % len(palette)] if c.category in categories else "#36d2ff"
            timeline_html += (
                f'<div class="tl-item">'
                f'<span class="tl-dot" style="background:{dot_color}"></span>'
                f'<span class="tl-time">{t}</span>'
                f'<span class="tl-app">{_esc(app)}</span>'
                f'<span class="tl-dur">{est}m</span>'
                f'</div>\n'
            )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Eyes Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0b1220;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,monospace;padding:24px}}
h1{{color:#36d2ff;font-size:1.6rem;margin-bottom:4px}}
h2{{color:#36d2ff;font-size:1.1rem;margin-bottom:12px;border-bottom:1px solid #1e2d40;padding-bottom:6px}}
.meta{{color:#6b7b8d;font-size:0.85rem;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}}
.card{{background:#111b2a;border:1px solid #1e2d40;border-radius:10px;padding:20px}}
.card-full{{grid-column:1/-1}}
.stat-row{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
.stat{{background:#111b2a;border:1px solid #1e2d40;border-radius:10px;padding:16px 20px;flex:1;min-width:140px;text-align:center}}
.stat-val{{font-size:1.8rem;font-weight:700;color:#36d2ff}}
.stat-lbl{{font-size:0.75rem;color:#6b7b8d;margin-top:2px;text-transform:uppercase;letter-spacing:0.05em}}
.heat-grid{{display:grid;grid-template-columns:repeat(24,1fr);gap:3px}}
.heat-cell{{aspect-ratio:1;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:0.6rem;color:#6b7b8d;position:relative}}
.heat-cell:hover{{outline:2px solid #36d2ff}}
.heat-label{{pointer-events:none}}
.pie{{width:180px;height:180px;border-radius:50%;background:conic-gradient({conic});margin:0 auto 16px}}
.legend-item{{display:flex;align-items:center;gap:6px;font-size:0.82rem;margin-bottom:4px}}
.legend-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.bar-row{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.bar-label{{width:140px;font-size:0.82rem;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bar-track{{flex:1;height:14px;background:#151e2d;border-radius:7px;overflow:hidden}}
.bar-fill{{height:100%;background:linear-gradient(90deg,#36d2ff,#1a8aaa);border-radius:7px}}
.bar-value{{width:100px;font-size:0.78rem;color:#6b7b8d}}
.gauge-wrap{{text-align:center}}
.gauge{{width:160px;height:80px;border-radius:160px 160px 0 0;background:#151e2d;position:relative;overflow:hidden;margin:0 auto}}
.gauge-fill{{position:absolute;bottom:0;left:0;width:160px;height:80px;border-radius:160px 160px 0 0;background:#36d2ff;transform-origin:bottom center;transform:rotate({gauge_angle - 180:.0f}deg)}}
.gauge-cover{{position:absolute;bottom:0;left:16px;width:128px;height:64px;border-radius:128px 128px 0 0;background:#111b2a}}
.gauge-val{{position:absolute;bottom:6px;left:0;right:0;font-size:1.6rem;font-weight:700;color:#36d2ff;text-align:center}}
.gauge-lbl{{text-align:center;font-size:0.75rem;color:#6b7b8d;margin-top:8px;text-transform:uppercase;letter-spacing:0.05em}}
.tl-item{{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid #151e2d;font-size:0.82rem}}
.tl-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.tl-time{{width:44px;color:#6b7b8d}}
.tl-app{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tl-dur{{color:#6b7b8d;width:48px;text-align:right}}
.footer{{text-align:center;color:#2a3a4d;font-size:0.72rem;margin-top:32px}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}.bar-label{{width:90px}}}}
</style>
</head>
<body>
<h1>Claude Eyes</h1>
<p class="meta">Dashboard generated {now.strftime('%Y-%m-%d %H:%M')} | Last {hours}h</p>

<div class="stat-row">
  <div class="stat"><div class="stat-val">{active_hrs}h</div><div class="stat-lbl">Active Time</div></div>
  <div class="stat"><div class="stat-val">{total}</div><div class="stat-lbl">Captures</div></div>
  <div class="stat"><div class="stat-val">{prod_pct:.0f}%</div><div class="stat-lbl">Productivity</div></div>
  <div class="stat"><div class="stat-val">{switches_per_hr}</div><div class="stat-lbl">Switches/hr</div></div>
</div>

<div class="grid">
  <div class="card card-full">
    <h2>Hourly Activity</h2>
    <div class="heat-grid">
{heatmap_cells}
    </div>
  </div>

  <div class="card">
    <h2>Categories</h2>
    <div class="pie"></div>
{legend_items}
  </div>

  <div class="card">
    <h2>Focus Score</h2>
    <div class="gauge-wrap">
      <div class="gauge">
        <div class="gauge-fill"></div>
        <div class="gauge-cover"></div>
        <div class="gauge-val">{focus_score}</div>
      </div>
      <div class="gauge-lbl">Focus Score (0-100)</div>
    </div>
  </div>

  <div class="card card-full">
    <h2>Top Apps</h2>
{app_bars}
  </div>

  <div class="card card-full">
    <h2>Timeline</h2>
{timeline_html}
  </div>
</div>

<div class="footer">Claude Eyes — screen awareness for Claude Code</div>
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
