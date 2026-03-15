# Claude Eyes 👁️ (Intel Mac Edition)

A lightweight screen-awareness system for macOS that gives Claude persistent visual context of what you're looking at. Optimized for Intel Macs.

## How It Works

```
Screen → JPEG Screenshot → Downscale (sips) → Diff Check (phash)
                                                    │
                                            changed? │ no → skip
                                                    ↓ yes
                                        Fast OCR (Vision framework)
                                                    │
                                            SQLite + FTS5 ← text only (~2KB)
                                                    │
                                            Delete screenshot
                                                    │
                                            MCP Server → Claude
```

## Intel-Specific Optimizations

| Optimization | What it does | Speedup |
|---|---|---|
| **Fast OCR mode** | `VNRequestTextRecognitionLevelFast` instead of Accurate | ~3x |
| **Downscale via sips** | Half-resolution before OCR (less pixels for CPU) | ~2x |
| **JPEG capture** | Faster write/read than PNG | ~1.5x |
| **Tiny phash** | 128px thumbnail, hash_size=8 | instant |
| **Threaded OCR** | Background thread so loop isn't blocked | no missed ticks |
| **Skip language correction** | Disabled in fast mode | ~20% faster |

Net result: **~0.5-1.5s per frame** on Intel instead of 3-5s.

## Setup

```bash
cd claude-eyes
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Grant Screen Recording permission:
# System Preferences → Privacy & Security → Screen Recording → add Terminal
```

Or just run the installer:
```bash
bash install.sh
```

## Usage

### First: benchmark your machine
```bash
python eyes.py benchmark
```
This tests OCR speed at different settings and recommends an interval.

### Start the watcher
```bash
python eyes.py watch                    # default: 10s interval, fast OCR, 50% scale
python eyes.py watch --interval 5       # faster polling (if your CPU can handle it)
python eyes.py watch --scale 0.75       # higher quality OCR (slower)
python eyes.py watch --accurate         # accurate OCR mode (2-4x slower)
python eyes.py watch --with-vision      # also use Claude Vision API ($ costs)
```

### Query from CLI
```bash
python eyes.py now                      # what's on screen right now
python eyes.py history 30               # last 30 minutes
python eyes.py history 30 -v            # verbose (full text)
python eyes.py search "obsidian"        # full-text search
python eyes.py app Safari 60            # Safari activity, last hour
python eyes.py stats                    # storage stats
python eyes.py prune 7                  # delete entries older than 7 days
```

### Connect to Claude via MCP
Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
    "mcpServers": {
        "claude-eyes": {
            "command": "/path/to/claude-eyes/venv/bin/python3",
            "args": ["/path/to/claude-eyes/mcp_server.py"]
        }
    }
}
```

Then Claude can:
- "What was I looking at 20 minutes ago?"
- "Find that thing I saw about embeddings"
- "What have I been doing in Obsidian today?"
- "What's on my screen right now?"

## Tesseract Fallback

If the Vision framework gives you trouble on your Intel Mac, install tesseract as a fallback:
```bash
brew install tesseract
```
The system will auto-detect and use it if Vision framework fails.

## Storage

- **~2KB per capture** (just text + metadata, never images)
- **1 hour at 10s intervals** ≈ 360 captures ≈ 720KB
- **1 day** ≈ ~17MB (assumes ~50% skipped for no-change)
- **1 week** ≈ ~120MB
- Auto-prune with `python eyes.py prune 7`

## Files

```
claude-eyes/
├── eyes.py           # CLI — watcher, queries, benchmark
├── capture.py        # Screenshot + OCR engine (Intel optimized)
├── store.py          # SQLite + FTS5 storage
├── mcp_server.py     # MCP server for Claude integration
├── install.sh        # One-line setup
├── requirements.txt  # Python deps
└── com.claude-eyes.watcher.plist  # LaunchAgent (auto-start)
```
