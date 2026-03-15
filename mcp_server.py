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

from store import EyesStore, parse_natural_time
from capture import capture_frame

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
