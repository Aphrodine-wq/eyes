#!/bin/bash
set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.claude-eyes.watcher.plist"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo "👁️  Claude Eyes — Setup"
echo "========================"
echo ""

# 1. Create venv and install deps
echo "📦 Installing dependencies..."
cd "$INSTALL_DIR"
python3 -m venv venv 2>/dev/null || true
source venv/bin/activate
pip install -q -r requirements.txt
echo "   ✅ Dependencies installed"

# 2. Create database directory
mkdir -p "$HOME/.claude-eyes"
echo "   ✅ Database directory ready"

# 3. Test screen capture permission
echo ""
echo "📸 Testing screen capture..."
TMPFILE=$(mktemp /tmp/ceyes_test_XXXXXX.png)
if screencapture -x "$TMPFILE" 2>/dev/null; then
    rm -f "$TMPFILE"
    echo "   ✅ Screen Recording permission granted"
else
    rm -f "$TMPFILE"
    echo "   ⚠️  Screen Recording permission needed!"
    echo "   → System Preferences → Privacy & Security → Screen Recording"
    echo "   → Add Terminal (or your terminal app)"
fi

# 4. Install LaunchAgent (optional)
echo ""
read -p "🚀 Auto-start on login? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    mkdir -p "$LAUNCH_AGENTS"
    # Update paths in plist
    sed "s|/Users/YOU/claude-eyes|$INSTALL_DIR|g" "$INSTALL_DIR/$PLIST_NAME" > "$LAUNCH_AGENTS/$PLIST_NAME"
    # Also update the python path to use venv
    sed -i '' "s|/usr/bin/env</string>|$INSTALL_DIR/venv/bin/python3</string>|" "$LAUNCH_AGENTS/$PLIST_NAME"
    sed -i '' "/<string>python3<\/string>/d" "$LAUNCH_AGENTS/$PLIST_NAME"
    launchctl load "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true
    echo "   ✅ LaunchAgent installed — watcher will start on login"
fi

# 5. Configure Claude Desktop MCP (optional)
echo ""
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
read -p "🔌 Configure Claude Desktop MCP? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    PYTHON_PATH="$INSTALL_DIR/venv/bin/python3"
    MCP_PATH="$INSTALL_DIR/mcp_server.py"

    if [ -f "$CLAUDE_CONFIG" ]; then
        # Check if already configured
        if grep -q "claude-eyes" "$CLAUDE_CONFIG" 2>/dev/null; then
            echo "   ⏭️  Already configured in Claude Desktop"
        else
            echo "   📝 Add this to your Claude Desktop config ($CLAUDE_CONFIG):"
            echo ""
            echo "   \"claude-eyes\": {"
            echo "     \"command\": \"$PYTHON_PATH\","
            echo "     \"args\": [\"$MCP_PATH\"]"
            echo "   }"
        fi
    else
        mkdir -p "$(dirname "$CLAUDE_CONFIG")"
        cat > "$CLAUDE_CONFIG" << MCPEOF
{
    "mcpServers": {
        "claude-eyes": {
            "command": "$PYTHON_PATH",
            "args": ["$MCP_PATH"]
        }
    }
}
MCPEOF
        echo "   ✅ Claude Desktop config created"
    fi
fi

echo ""
echo "✨ Setup complete! Quick start:"
echo ""
echo "   source venv/bin/activate"
echo "   python eyes.py watch          # start watching"
echo "   python eyes.py search 'topic' # search history"
echo "   python eyes.py stats          # check storage"
echo ""
