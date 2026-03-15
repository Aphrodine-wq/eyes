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

from store import EyesStore
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
