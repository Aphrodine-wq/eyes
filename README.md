# Eyes

Ambient screen awareness for Claude Code. A lightweight MCP server that continuously captures what's on your screen via OCR and stores it locally — giving Claude persistent memory of what you've been looking at.

**No cloud. No images saved. Just text in a local SQLite database.**

## Why

Every other AI assistant only knows what you paste into it. Eyes runs a background watcher that captures screen text every 10 seconds, skips duplicates, and stores ~2KB per frame. Then Claude can answer questions like:

- "What was I looking at 20 minutes ago?"
- "Find that thing I saw about embeddings"
- "What have I been doing in VS Code today?"

It's the difference between an assistant you have to feed context to and one that already knows what you've been working on.

## How It Works

```
Screen --> JPEG capture --> Downscale --> Perceptual hash (changed?)
                                              |
                                         no = skip
                                         yes = OCR (macOS Vision framework)
                                              |
                                         SQLite + FTS5 (text only, ~2KB)
                                              |
                                         MCP Server --> Claude
```

Screenshots are never saved. Only the extracted text hits disk.

## Quick Start

```bash
git clone https://github.com/Aphrodine-wq/eyes.git
cd eyes
bash install.sh
```

The installer will:
1. Create a Python venv and install dependencies
2. Test screen recording permissions
3. Optionally set up auto-start on login (LaunchAgent)
4. Optionally configure Claude Desktop MCP

**Manual setup:**
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python eyes.py watch
```

> You'll need Screen Recording permission: System Preferences -> Privacy & Security -> Screen Recording -> add your terminal app.

## MCP Integration

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "eyes": {
      "command": "/path/to/eyes/venv/bin/python3",
      "args": ["/path/to/eyes/mcp_server.py"]
    }
  }
}
```

### MCP Tools (14 tools)

**Core:**
| Tool | What it does |
|---|---|
| `see_screen_now` | Live screenshot + OCR of current screen |
| `get_recent_screen_context` | What's been on screen in the last N minutes |
| `search_screen_history` | Full-text search across all captured screen text |
| `get_app_activity` | Screen captures filtered by app name |
| `get_screen_at_time` | Natural language time queries ("this morning", "yesterday") |
| `screen_stats` | Database size, capture count, date range |

**Analytics:**
| Tool | What it does |
|---|---|
| `get_activity_summary` | Narrative summary of recent work — apps, flow, time per app |
| `get_focus_stats` | App focus breakdown — time, percentages, context switches |
| `get_sessions` | Detect work sessions with gaps, show start/end and focus |
| `classify_activity` | Classify captures into categories (code, chat, browser, etc.) with productivity score |

**Reports:**
| Tool | What it does |
|---|---|
| `get_daily_digest` | Full daily report — hourly heatmap, categories, productivity, sessions |
| `get_weekly_digest` | 7-day comparison — trends, daily averages, productivity patterns |
| `compare_days` | Side-by-side comparison of any two days |
| `get_trigger_events` | Recent screen content trigger matches |

## CLI

```bash
# Core
python eyes.py watch                    # start watcher (10s interval)
python eyes.py watch --interval 5       # faster polling
python eyes.py watch --accurate         # accurate OCR (slower, better text)
python eyes.py watch --adaptive         # adaptive rate (fast when active, slow when idle)
python eyes.py now                      # what's on screen right now

# Search & history
python eyes.py history 30               # last 30 minutes of activity
python eyes.py search "react hooks"     # full-text search
python eyes.py app Safari 60            # Safari activity, last hour

# Analytics
python eyes.py summary 60              # narrative summary of last hour
python eyes.py focus 120               # focus breakdown with visual bars
python eyes.py sessions                 # detect work sessions (gaps = breaks)
python eyes.py classify 60             # content classification (code/chat/browser/etc.)

# Reports
python eyes.py digest                   # daily digest (today)
python eyes.py digest --date 2026-03-14 # digest for specific date
python eyes.py digest --weekly          # 7-day weekly digest

# Triggers
python eyes.py triggers                 # show recent trigger events

# Management
python eyes.py stats                    # storage stats
python eyes.py prune 7                  # delete entries older than 7 days
python eyes.py benchmark                # test OCR speed on your machine
python eyes.py config --show            # view config
python eyes.py config --ignore-add "1Password"    # skip capturing this app
python eyes.py config --ignore-remove "1Password" # re-enable capturing
```

## Performance

Optimized for Intel Macs but works on Apple Silicon too.

| Optimization | Effect |
|---|---|
| Fast OCR mode | ~3x faster than Accurate |
| 50% downscale before OCR | ~2x speedup |
| JPEG capture (not PNG) | ~1.5x faster I/O |
| Perceptual hashing | Skips duplicate frames instantly |
| Threaded OCR | Watcher loop never blocks |

**Result: ~0.5-1.5s per frame** on Intel, faster on Apple Silicon.

## Storage

Text only. No images.

| Timeframe | Size |
|---|---|
| 1 hour | ~720KB |
| 1 day | ~17MB |
| 1 week | ~120MB |
| 1 month | ~500MB |

Auto-prune with `python eyes.py prune 7` (keeps last 7 days).

## Config

Eyes uses `~/.claude-eyes/config.json` for settings. Created automatically on first run.

```json
{
  "ignore_apps": ["1Password", "Keychain Access", "LastPass", "Bitwarden"],
  "session_gap_minutes": 5,
  "capture_interval": 10
}
```

- **ignore_apps** — apps that will never be captured (password managers by default)
- **session_gap_minutes** — how long a gap before it's a new work session
- **capture_interval** — seconds between captures (used for time estimates)

## Content Classification

Every capture is automatically classified into content categories:

| Category | Examples |
|---|---|
| `code` | VS Code, Xcode, Cursor, GitHub in browser |
| `terminal` | iTerm2, Terminal, Warp |
| `chat` | Slack, Discord, Teams, iMessage |
| `browser` | Chrome, Safari, Arc (sub-classified by content) |
| `docs` | Notion, Obsidian, Google Docs, Word |
| `design` | Figma, Sketch, Photoshop |
| `email` | Mail, Gmail, Outlook |
| `media` | Spotify, YouTube, Netflix |

Browser content is further classified by window title (GitHub = code, Gmail = email, YouTube = media).

Each capture also gets a **productivity score** — categories like code, terminal, docs, and design are considered productive. Ask Claude "how productive was my morning?" and it knows.

## Adaptive Capture Rate

With `--adaptive`, the watcher dynamically adjusts its capture interval:

- **Active** (lots of screen changes): captures every 3-5 seconds
- **Moderate**: base interval (default 10s)
- **Idle** (no changes): slows to 30s
- **Screen locked**: pauses entirely

Uses an exponential moving average of change frequency with burst detection and idle detection. Saves CPU and storage when you're not actively working, captures more when you are.

## Triggers

Define rules in `~/.claude-eyes/config.json` that fire when patterns appear on screen:

```json
{
  "triggers": [
    {
      "name": "build-failure",
      "pattern": "BUILD FAILED|error:.*fatal|FAIL.*test",
      "action": "log",
      "cooldown_seconds": 60
    },
    {
      "name": "meeting-starting",
      "pattern": "zoom.*meeting|teams.*meeting",
      "match_on": "window_title",
      "action": "command",
      "command": "osascript -e 'display notification \"Meeting detected\" with title \"Eyes\"'",
      "cooldown_seconds": 300
    }
  ]
}
```

Trigger actions:
- **log** — write to `~/.claude-eyes/triggers.log`
- **command** — run a shell command (notifications, scripts, webhooks)
- **flag** — set a flag the MCP server can report to Claude

## Daily & Weekly Digests

Generate structured reports:

- **Daily digest**: hourly heatmap, category breakdown, productivity score, top apps, session timeline
- **Weekly digest**: 7-day comparison table with active time, productivity, and trends
- **Day comparison**: side-by-side metrics between any two days

Ask Claude "give me my daily digest" or "compare today to yesterday".

## Natural Language Time Queries

The `get_screen_at_time` MCP tool understands time expressions:

- "this morning" / "this afternoon" / "this evening"
- "yesterday" / "yesterday morning"
- "last 2 hours" / "last 30 minutes" / "last 3 days"
- "today" / "this week" / "last week"

So you can ask Claude: *"what was on my screen yesterday morning?"* and it just works.

## Privacy

- Everything stays on your machine. Zero network calls.
- Screenshots are captured, OCR'd, and immediately deleted — only text is stored.
- Database lives at `~/.claude-eyes/eyes.db` — delete it anytime to wipe history.
- The watcher is a LaunchAgent you fully control (start/stop/remove).

**Be aware:** it captures text from whatever is on screen. If you have passwords, sensitive documents, or private messages visible, that text will be in the local database. Stop the watcher during sensitive work if needed:

```bash
launchctl unload ~/Library/LaunchAgents/com.claude-eyes.watcher.plist  # stop
launchctl load ~/Library/LaunchAgents/com.claude-eyes.watcher.plist    # start
```

## vs Screenpipe

[Screenpipe](https://github.com/mediar-ai/screenpipe) is the main alternative for ambient AI screen context. Here's how Eyes compares:

| | Eyes | Screenpipe |
|---|---|---|
| **Size** | ~1,300 lines, 7 files | Full product, thousands of files |
| **Scope** | Screen text only | Screen + audio + UI elements |
| **Storage** | ~2KB/frame text | Images + audio + text |
| **Setup** | `bash install.sh` | Desktop app installer |
| **Dependencies** | Python + macOS Vision | Rust + multiple native libs |
| **Target** | Claude Code users | General AI assistant users |
| **Cloud** | Never | Optional cloud features |

Eyes is the lightweight option. If you just want Claude to know what's on your screen without installing a full platform, this is it.

## Architecture

```
eyes/
  eyes.py           # CLI and watcher loop (15 commands)
  capture.py        # Screenshot + OCR engine (Vision framework + tesseract fallback)
  store.py          # SQLite + FTS5 database, sessions, focus stats, natural time parsing
  mcp_server.py     # MCP server (14 tools)
  classifier.py     # Content classification engine (pattern-based, no ML)
  adaptive.py       # Adaptive capture rate (EMA-based, idle detection)
  triggers.py       # Screen content trigger system (pattern match -> actions)
  digest.py         # Daily/weekly digest generation with category analysis
  install.sh        # Setup script
  requirements.txt  # Python dependencies
  com.claude-eyes.watcher.plist  # macOS LaunchAgent
```

## Requirements

- macOS (uses Vision framework for OCR)
- Python 3.10+
- Screen Recording permission

## Contributing

PRs welcome. The big gaps:
- **Windows support** — need a different OCR backend (Windows.Media.Ocr or Tesseract)
- **Linux support** — Tesseract + xdotool for active window detection
- **Configurable capture regions** — monitor selection, window-only capture
- **Retention policies** — auto-prune by age or size

## License

MIT
