#!/usr/bin/env python3
"""
mcp_server.py — MCP server that gives Claude access to your screen history.

Add to Claude Desktop config:
{
    "mcpServers": {
        "claude-eyes": {
            "command": "python",
            "args": ["/path/to/claude-eyes/mcp_server.py"]
        }
    }
}
"""

import json
import time
import sys
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from store import EyesStore, parse_natural_time, load_config
from capture import capture_frame
from classifier import classify_batch, classify_capture
from digest import generate_daily_digest, generate_weekly_digest, compare_days
from flow import FlowDetector
from context_chain import ContextTracker
from patterns import PatternEngine

# Persistent engines (survive across MCP calls)
_flow_detector = FlowDetector(window_minutes=15)
_context_tracker = ContextTracker(window_size=200)
_pattern_engine = PatternEngine()

server = Server("claude-eyes")


def format_entries(entries, max_per_entry=600) -> str:
    """Format screen entries into a concise context block."""
    if not entries:
        return "No screen captures found."

    parts = []
    for e in entries:
        ts = datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        text = e.text[:max_per_entry] + "..." if len(e.text) > max_per_entry else e.text
        block = f"[{ts}] App: {e.app_name}"
        if e.window_title:
            block += f" | Window: {e.window_title}"
        block += f"\n{text}"
        if e.extra_context:
            block += f"\nContext: {e.extra_context}"
        parts.append(block)

    return "\n\n---\n\n".join(parts)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="see_screen_now",
            description=(
                "Capture what's currently on the user's screen right now. "
                "Returns OCR text and active app info. Use this when the user "
                "asks 'what am I looking at' or 'what's on my screen'."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_recent_screen_context",
            description=(
                "Get the user's recent screen activity. Returns parsed text from "
                "recent screenshots. Use this to understand what the user has been "
                "working on, or to get context for their current question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "How many minutes of history to retrieve (default: 15)",
                        "default": 15,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of captures to return (default: 10)",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="search_screen_history",
            description=(
                "Search the user's full screen history using full-text search. "
                "Use this when the user asks about something they saw earlier, "
                "a specific topic, or to find when they were looking at something."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (supports FTS5 syntax: AND, OR, NOT, phrases)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_app_activity",
            description=(
                "Get screen captures from a specific app. Use when the user asks "
                "about what they were doing in a particular application."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "App name to filter by (partial match, e.g. 'Obsidian', 'Chrome', 'VS Code')",
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "How far back to look in minutes (default: 60)",
                        "default": 60,
                    },
                },
                "required": ["app_name"],
            },
        ),
        Tool(
            name="screen_stats",
            description="Get statistics about the screen capture database.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_activity_summary",
            description=(
                "Get a narrative summary of the user's recent screen activity. "
                "Shows the flow of work — which apps, how long, what windows. "
                "Use this when the user asks 'what have I been doing' or 'summarize my activity'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "How many minutes to summarize (default: 60)",
                        "default": 60,
                    },
                },
            },
        ),
        Tool(
            name="get_focus_stats",
            description=(
                "Get app focus time breakdown — how long in each app, context switches, "
                "and percentages. Use when the user asks about productivity, screen time, "
                "or which apps they've been using most."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "How far back to analyze (default: 60)",
                        "default": 60,
                    },
                },
            },
        ),
        Tool(
            name="get_sessions",
            description=(
                "Detect work sessions — contiguous periods of activity separated by gaps. "
                "Shows when you started/stopped working and what you focused on in each session. "
                "Use when the user asks 'how was my day' or 'when did I start working'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "How many hours back to look for sessions (default: 8)",
                        "default": 8,
                    },
                },
            },
        ),
        Tool(
            name="get_screen_at_time",
            description=(
                "Get screen captures from a specific time period using natural language. "
                "Supports: 'this morning', 'yesterday afternoon', 'last 2 hours', "
                "'today', 'this week', 'yesterday'. Use when the user references a "
                "specific time period."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "Natural language time expression (e.g., 'this morning', 'yesterday', 'last 3 hours')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 15)",
                        "default": 15,
                    },
                },
                "required": ["when"],
            },
        ),
        Tool(
            name="classify_activity",
            description=(
                "Classify recent screen activity into content categories "
                "(code, chat, browser, docs, media, terminal, design, email). "
                "Shows productivity score, subcategories, and extracted keywords. "
                "Use when the user asks 'what kind of work have I been doing' or "
                "'how productive was I'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "How far back to classify (default: 60)",
                        "default": 60,
                    },
                },
            },
        ),
        Tool(
            name="get_daily_digest",
            description=(
                "Generate a comprehensive daily activity digest with hourly heatmap, "
                "category breakdown, productivity score, top apps, and session timeline. "
                "Use when the user asks 'how was my day' or 'daily report'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format (default: today)",
                    },
                },
            },
        ),
        Tool(
            name="get_weekly_digest",
            description=(
                "Generate a weekly activity digest comparing all 7 days — "
                "active time, productivity, top apps per day. "
                "Use when the user asks about their week or weekly patterns."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="compare_days",
            description=(
                "Compare activity between two days — active time, productivity, "
                "captures. Use when the user asks 'how does today compare to yesterday' "
                "or wants to compare any two dates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "day1": {
                        "type": "string",
                        "description": "First date (YYYY-MM-DD or 'today', 'yesterday')",
                    },
                    "day2": {
                        "type": "string",
                        "description": "Second date (YYYY-MM-DD or 'today', 'yesterday')",
                    },
                },
                "required": ["day1", "day2"],
            },
        ),
        Tool(
            name="get_trigger_events",
            description=(
                "Get recent trigger events — patterns detected on screen that matched "
                "configured trigger rules. Use when the user asks about alerts or "
                "automated detections."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max events to return (default: 20)",
                        "default": 20,
                    },
                },
            },
        ),
        Tool(
            name="detect_flow_state",
            description=(
                "Detect the user's current cognitive flow state — deep focus, flow, "
                "working, shallow, or scattered. Shows focus score, primary app, "
                "disruption count, and current streak. Use when the user asks about "
                "their focus, productivity, or flow."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_attention_profile",
            description=(
                "Get a comprehensive attention profile — peak focus hours, "
                "flow-inducing apps, distraction patterns, total deep focus time. "
                "Use when the user asks about their attention patterns or focus habits."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to analyze (default: 8)",
                        "default": 8,
                    },
                },
            },
        ),
        Tool(
            name="get_context_chain",
            description=(
                "Get the chain of contexts that led to the user's current screen. "
                "Tracks how information flowed across apps — e.g., saw a bug in Slack, "
                "searched in Chrome, opened file in VS Code. Shows shared terms that "
                "carried across transitions. Use when the user asks 'how did I get here' "
                "or when you need to understand the full context of what they're doing."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="find_forgotten_context",
            description=(
                "Surface things the user SAW on screen hours ago that are relevant to "
                "what's on screen RIGHT NOW — but they may have forgotten. This is "
                "phantom memory: Claude knows things the user forgot they saw. "
                "Use proactively when helping with a task to surface relevant past context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "current_text": {
                        "type": "string",
                        "description": "Current screen text or topic to find forgotten context for",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "How far back to search (default: 4)",
                        "default": 4,
                    },
                },
                "required": ["current_text"],
            },
        ),
        Tool(
            name="predict_next_app",
            description=(
                "Predict what app the user will likely switch to next, based on "
                "historical transition patterns and time-of-day habits. "
                "Use when anticipating what the user needs."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="detect_workflows",
            description=(
                "Detect recurring workflow patterns — app sequences that repeat regularly. "
                "E.g., 'research-to-code' (Chrome -> VS Code -> Terminal). "
                "Shows frequency, typical time of day, and confidence. "
                "Use when the user asks about their work patterns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Days of history to analyze (default: 7)",
                        "default": 7,
                    },
                },
            },
        ),
        Tool(
            name="detect_anomalies",
            description=(
                "Compare today's behavior to the 7-day baseline and flag anomalies. "
                "Detects unusual app usage, abnormal activity levels, high context "
                "switching, and new apps. Use when the user asks 'anything weird today' "
                "or to proactively flag behavioral changes."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_flow_breakers",
            description=(
                "Identify what broke the user's focus — which apps interrupted deep focus "
                "periods. Shows the app that was interrupted, how long the focus lasted, "
                "and what broke it. Use when the user asks about distractions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "How far back to look (default: 120)",
                        "default": 120,
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    store = EyesStore()

    try:
        if name == "see_screen_now":
            frame = capture_frame()
            if frame:
                ts = datetime.fromtimestamp(frame.timestamp).strftime("%H:%M:%S")
                result = f"[{ts}] Active app: {frame.app_name}"
                if frame.window_title:
                    result += f" — {frame.window_title}"
                result += f"\n\nScreen text:\n{frame.text}"
                # Also store it
                store.insert(
                    timestamp=frame.timestamp,
                    app_name=frame.app_name,
                    window_title=frame.window_title,
                    text=frame.text,
                    phash=frame.phash,
                )
            else:
                result = "Could not capture screen (may be locked or permission denied)."

        elif name == "get_recent_screen_context":
            minutes = arguments.get("minutes", 15)
            limit = arguments.get("limit", 10)
            entries = store.get_recent(minutes=minutes, limit=limit)
            result = f"Screen activity (last {minutes} min):\n\n"
            result += format_entries(entries)

        elif name == "search_screen_history":
            query = arguments["query"]
            limit = arguments.get("limit", 10)
            entries = store.search(query, limit=limit)
            result = f"Search results for '{query}':\n\n"
            result += format_entries(entries)

        elif name == "get_app_activity":
            app_name = arguments["app_name"]
            minutes = arguments.get("minutes", 60)
            entries = store.search_by_app(app_name, minutes=minutes)
            result = f"Activity in {app_name} (last {minutes} min):\n\n"
            result += format_entries(entries)

        elif name == "screen_stats":
            stats = store.stats()
            result = json.dumps(stats, indent=2)

        elif name == "get_activity_summary":
            minutes = arguments.get("minutes", 60)
            result = store.get_activity_summary(minutes=minutes)

        elif name == "get_focus_stats":
            minutes = arguments.get("minutes", 60)
            focus = store.get_focus_stats(minutes=minutes)
            lines = [f"Focus stats (last {minutes} min):\n"]
            lines.append(f"Total captures: {focus['total_frames']}")
            lines.append(f"Context switches: {focus['switches']}\n")
            for app, info in focus["apps"].items():
                lines.append(
                    f"  {app}: ~{info['estimated_minutes']}min "
                    f"({info['percent']}%, {info['frames']} frames)"
                )
            result = "\n".join(lines)

        elif name == "get_sessions":
            hours = arguments.get("hours", 8)
            sessions = store.get_sessions(hours=hours)
            if not sessions:
                result = f"No sessions detected in the last {hours} hours."
            else:
                lines = [f"Work sessions (last {hours} hours):\n"]
                for i, s in enumerate(sessions, 1):
                    start_str = datetime.fromtimestamp(s.start).strftime("%H:%M")
                    end_str = datetime.fromtimestamp(s.end).strftime("%H:%M")
                    lines.append(f"Session {i}: {start_str} - {end_str}")
                    lines.append(f"  {s.summary}")
                    lines.append(f"  Apps: {', '.join(s.apps)}")
                    lines.append(f"  Frames: {s.frame_count}\n")
                result = "\n".join(lines)

        elif name == "get_screen_at_time":
            when = arguments["when"]
            limit = arguments.get("limit", 15)
            start_ts, end_ts = parse_natural_time(when)
            entries = store.get_by_time_range(start_ts, end_ts, limit=limit)
            start_str = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M")
            end_str = datetime.fromtimestamp(end_ts).strftime("%H:%M")
            result = f"Screen activity for '{when}' ({start_str} - {end_str}):\n\n"
            result += format_entries(entries)

        elif name == "classify_activity":
            minutes = arguments.get("minutes", 60)
            entries = store.get_recent(minutes=minutes, limit=5000)
            if not entries:
                result = f"No captures in the last {minutes} minutes."
            else:
                categories = classify_batch(entries)
                config = load_config()
                interval = config.get("capture_interval", 10)
                total = sum(c["count"] for c in categories.values())
                productive = sum(c["productive_frames"] for c in categories.values())
                prod_pct = round((productive / total) * 100, 1) if total else 0

                lines = [f"Content classification (last {minutes} min, {len(entries)} captures):\n"]
                for cat, info in sorted(categories.items(), key=lambda x: -x[1]["count"]):
                    pct = round((info["count"] / total) * 100, 1)
                    est_min = round((info["count"] * interval) / 60, 1)
                    prod_mark = " [productive]" if info["productive_frames"] > info["count"] * 0.5 else ""
                    lines.append(f"  {cat}: {pct}% (~{est_min}min){prod_mark}")
                    subs = info.get("subcategories", {})
                    for sub, count in sorted(subs.items(), key=lambda x: -x[1])[:3]:
                        if sub:
                            lines.append(f"    - {sub}: {count} frames")
                    kws = info.get("top_keywords", [])
                    if kws:
                        lines.append(f"    keywords: {', '.join(kws[:6])}")
                lines.append(f"\nProductivity: {prod_pct}%")
                result = "\n".join(lines)

        elif name == "get_daily_digest":
            date_str = arguments.get("date")
            if date_str:
                from datetime import datetime as dt_cls
                date = dt_cls.strptime(date_str, "%Y-%m-%d")
            else:
                date = None
            result = generate_daily_digest(store, date)

        elif name == "get_weekly_digest":
            result = generate_weekly_digest(store)

        elif name == "compare_days":
            from datetime import datetime as dt_cls, timedelta as td
            def parse_day(s):
                s = s.strip().lower()
                today = dt_cls.now().replace(hour=0, minute=0, second=0, microsecond=0)
                if s == "today":
                    return today
                elif s == "yesterday":
                    return today - td(days=1)
                else:
                    return dt_cls.strptime(s, "%Y-%m-%d")
            day1 = parse_day(arguments["day1"])
            day2 = parse_day(arguments["day2"])
            result = compare_days(store, day1, day2)

        elif name == "get_trigger_events":
            from pathlib import Path
            limit = arguments.get("limit", 20)
            log_path = Path.home() / ".claude-eyes" / "triggers.log"
            if log_path.exists():
                lines = log_path.read_text().strip().split("\n")
                recent = lines[-limit:] if len(lines) > limit else lines
                result = f"Recent trigger events ({len(recent)}):\n\n" + "\n".join(recent)
            else:
                result = "No trigger events recorded yet."

        elif name == "detect_flow_state":
            # Hydrate flow detector from recent captures if empty
            if len(_flow_detector.captures) < 5:
                recent = store.get_recent(minutes=20, limit=100)
                for e in reversed(recent):
                    cls = classify_capture(e.app_name, e.window_title, e.text)
                    _flow_detector.record(e.timestamp, e.app_name, cls.category)

            flow = _flow_detector.get_flow_state()
            result = (
                f"Flow State: {flow.state.upper()} (score: {flow.score})\n\n"
                f"{flow.description()}\n\n"
                f"Score: {flow.score}/1.0\n"
                f"State duration: {flow.duration_minutes}min\n"
                f"Current app streak: {flow.streak_minutes}min in {flow.primary_app}\n"
                f"Disruptions (last 15min): {flow.disruptions}\n"
                f"Primary category: {flow.primary_category}"
            )

        elif name == "get_attention_profile":
            hours = arguments.get("hours", 8)
            recent = store.get_recent(minutes=hours * 60, limit=5000)
            for e in reversed(recent):
                cls = classify_capture(e.app_name, e.window_title, e.text)
                _flow_detector.record(e.timestamp, e.app_name, cls.category)

            profile = _flow_detector.get_attention_profile(hours)
            lines = [
                f"Attention Profile (last {hours}h):\n",
                f"Average flow score: {profile.avg_flow_score}",
                f"Peak focus hour: {profile.peak_focus_hour}:00",
                f"Worst focus hour: {profile.worst_focus_hour}:00",
                f"Avg session before switching: {profile.avg_session_before_switch}min",
                f"Total deep focus: {profile.total_deep_focus_minutes}min",
                f"Total scattered: {profile.total_scattered_minutes}min",
            ]
            if profile.top_flow_apps:
                lines.append(f"\nFlow-inducing apps: {', '.join(profile.top_flow_apps)}")
            if profile.top_distraction_apps:
                lines.append(f"Distraction-prone apps: {', '.join(profile.top_distraction_apps)}")
            result = "\n".join(lines)

        elif name == "get_context_chain":
            # Hydrate context tracker
            if len(_context_tracker.window) < 5:
                recent = store.get_recent(minutes=30, limit=100)
                for e in reversed(recent):
                    cls = classify_capture(e.app_name, e.window_title, e.text)
                    _context_tracker.record(
                        e.timestamp, e.app_name, e.window_title, e.text, cls.category
                    )

            chain = _context_tracker.get_current_chain()
            if chain:
                result = chain.narrative()
            else:
                chains = _context_tracker.get_recent_chains(5)
                if chains:
                    result = "No active chain. Recent chains:\n\n"
                    result += "\n\n---\n\n".join(c.narrative() for c in chains)
                else:
                    result = "No context chains detected yet. Need more app transitions."

        elif name == "find_forgotten_context":
            current_text = arguments["current_text"]
            hours = arguments.get("hours", 4)

            # Hydrate tracker
            if len(_context_tracker.window) < 10:
                recent = store.get_recent(minutes=hours * 60, limit=500)
                for e in reversed(recent):
                    cls = classify_capture(e.app_name, e.window_title, e.text)
                    _context_tracker.record(
                        e.timestamp, e.app_name, e.window_title, e.text, cls.category
                    )

            forgotten = _context_tracker.find_forgotten_context(current_text, hours)
            if forgotten:
                lines = [f"Forgotten context (things you saw in the last {hours}h that are relevant now):\n"]
                for node in forgotten:
                    ts = datetime.fromtimestamp(node.timestamp).strftime("%H:%M")
                    lines.append(f"  [{ts}] {node.app_name}: {node.text_fingerprint}")
                    if node.keywords:
                        lines.append(f"    terms: {', '.join(node.keywords[:5])}")
                result = "\n".join(lines)
            else:
                result = "No forgotten context found matching the current screen."

        elif name == "predict_next_app":
            prediction = _pattern_engine.predict_next_app(store)
            if prediction.predicted_app:
                lines = [
                    f"Predicted next app: {prediction.predicted_app} ({prediction.confidence:.0%} confidence)",
                    f"Reasoning: {prediction.reasoning}",
                ]
                if prediction.alternatives:
                    alts = [f"{a} ({c:.0%})" for a, c in prediction.alternatives]
                    lines.append(f"Alternatives: {', '.join(alts)}")
                result = "\n".join(lines)
            else:
                result = prediction.reasoning

        elif name == "detect_workflows":
            days = arguments.get("days", 7)
            workflows = _pattern_engine.detect_workflows(store, days)
            if workflows:
                lines = [f"Detected workflows (last {days} days):\n"]
                for wf in workflows:
                    lines.append(f"  {wf.name}")
                    lines.append(f"    Sequence: {' -> '.join(wf.app_sequence)}")
                    lines.append(f"    Seen {wf.occurrences}x, typically in the {wf.time_of_day}")
                    lines.append(f"    Confidence: {wf.confidence:.0%}")
                    last = datetime.fromtimestamp(wf.last_seen).strftime("%Y-%m-%d %H:%M")
                    lines.append(f"    Last seen: {last}\n")
                result = "\n".join(lines)
            else:
                result = "No recurring workflows detected yet. Need more history."

        elif name == "detect_anomalies":
            anomalies = _pattern_engine.detect_anomalies(store)
            if anomalies:
                lines = ["Behavioral anomalies detected today:\n"]
                for a in anomalies:
                    icon = {"info": "i", "notable": "!", "unusual": "!!", "significant": "!!!"}
                    lines.append(f"  [{icon.get(a.severity, '?')}] {a.description}")
                    lines.append(f"      Expected: {a.expected_value} | Actual: {a.actual_value}")
                result = "\n".join(lines)
            else:
                result = "No anomalies detected. Today looks normal compared to your 7-day baseline."

        elif name == "get_flow_breakers":
            minutes = arguments.get("minutes", 120)
            recent = store.get_recent(minutes=minutes, limit=2000)
            for e in reversed(recent):
                cls = classify_capture(e.app_name, e.window_title, e.text)
                _flow_detector.record(e.timestamp, e.app_name, cls.category)

            breakers = _flow_detector.detect_flow_breakers(minutes)
            if breakers:
                lines = [f"Flow breakers (last {minutes}min):\n"]
                for b in breakers:
                    ts = datetime.fromtimestamp(b["timestamp"]).strftime("%H:%M")
                    lines.append(
                        f"  {ts}: {b['breaker_app']} broke {b['broken_duration_min']}min "
                        f"focus in {b['broken_app']}"
                    )
                result = "\n".join(lines)
            else:
                result = f"No flow interruptions detected in the last {minutes} minutes."

        else:
            result = f"Unknown tool: {name}"

    finally:
        store.close()

    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
