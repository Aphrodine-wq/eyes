"""
classifier.py — Content classification engine.

Classifies screen captures into content types based on app name,
window title, and OCR text patterns. No ML required — pattern-based
with configurable rules.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Classification:
    """Classification result for a screen capture."""
    category: str          # code, chat, browser, docs, media, terminal, design, email, unknown
    confidence: float      # 0.0 - 1.0
    subcategory: str       # e.g., "python", "slack", "github"
    keywords: list[str]    # extracted keywords from content
    is_productive: bool    # heuristic: is this likely productive work?


# App -> category mapping (lowercase)
APP_CATEGORIES = {
    # Code editors
    "xcode": ("code", "xcode"),
    "visual studio code": ("code", "vscode"),
    "code": ("code", "vscode"),
    "sublime text": ("code", "sublime"),
    "neovim": ("code", "neovim"),
    "vim": ("code", "vim"),
    "intellij idea": ("code", "intellij"),
    "pycharm": ("code", "pycharm"),
    "webstorm": ("code", "webstorm"),
    "cursor": ("code", "cursor"),
    "zed": ("code", "zed"),

    # Terminals
    "terminal": ("terminal", "terminal"),
    "iterm2": ("terminal", "iterm"),
    "iterm": ("terminal", "iterm"),
    "warp": ("terminal", "warp"),
    "alacritty": ("terminal", "alacritty"),
    "kitty": ("terminal", "kitty"),
    "hyper": ("terminal", "hyper"),

    # Chat / communication
    "slack": ("chat", "slack"),
    "discord": ("chat", "discord"),
    "microsoft teams": ("chat", "teams"),
    "zoom": ("chat", "zoom"),
    "messages": ("chat", "imessage"),
    "telegram": ("chat", "telegram"),
    "whatsapp": ("chat", "whatsapp"),
    "facetime": ("chat", "facetime"),

    # Browsers
    "google chrome": ("browser", "chrome"),
    "safari": ("browser", "safari"),
    "firefox": ("browser", "firefox"),
    "arc": ("browser", "arc"),
    "brave browser": ("browser", "brave"),
    "microsoft edge": ("browser", "edge"),

    # Documents
    "pages": ("docs", "pages"),
    "microsoft word": ("docs", "word"),
    "google docs": ("docs", "gdocs"),
    "notion": ("docs", "notion"),
    "obsidian": ("docs", "obsidian"),
    "bear": ("docs", "bear"),
    "typora": ("docs", "typora"),

    # Spreadsheets / data
    "numbers": ("docs", "numbers"),
    "microsoft excel": ("docs", "excel"),

    # Design
    "figma": ("design", "figma"),
    "sketch": ("design", "sketch"),
    "adobe photoshop": ("design", "photoshop"),
    "adobe illustrator": ("design", "illustrator"),
    "canva": ("design", "canva"),
    "preview": ("design", "preview"),

    # Email
    "mail": ("email", "apple-mail"),
    "gmail": ("email", "gmail"),
    "outlook": ("email", "outlook"),
    "spark": ("email", "spark"),

    # Media
    "spotify": ("media", "spotify"),
    "apple music": ("media", "apple-music"),
    "music": ("media", "apple-music"),
    "vlc": ("media", "vlc"),
    "quicktime player": ("media", "quicktime"),
    "youtube": ("media", "youtube"),
    "tv": ("media", "apple-tv"),

    # System
    "finder": ("system", "finder"),
    "system preferences": ("system", "preferences"),
    "system settings": ("system", "settings"),
    "activity monitor": ("system", "activity-monitor"),
}

# Productive categories
PRODUCTIVE_CATEGORIES = {"code", "terminal", "docs", "design", "email"}

# Text patterns for classification refinement
CODE_PATTERNS = [
    r'\bdef\s+\w+\s*\(', r'\bclass\s+\w+', r'\bimport\s+\w+',
    r'\bfunction\s+\w+', r'\bconst\s+\w+', r'\blet\s+\w+',
    r'\breturn\s+', r'\bif\s*\(', r'\bfor\s*\(',
    r'[{}\[\]];', r'=>', r'\.map\(', r'\.filter\(',
    r'git\s+(commit|push|pull|merge|checkout|branch|log|diff|status)',
    r'npm\s+(install|run|test)', r'pip\s+install',
    r'docker\s+(build|run|compose|push)',
    r'<\w+[^>]*>', r'</\w+>',  # HTML/XML tags
]

CHAT_PATTERNS = [
    r'\d{1,2}:\d{2}\s*(AM|PM|am|pm)?',  # timestamps in chat
    r'(sent|delivered|typing|online|offline)',
    r'@\w+',  # mentions
    r'#\w+',  # channels
]

BROWSER_URL_PATTERNS = [
    r'https?://[^\s]+',
    r'www\.\w+\.\w+',
    r'\.com\b|\.org\b|\.io\b|\.dev\b',
]


def classify_capture(app_name: str, window_title: str, text: str) -> Classification:
    """
    Classify a screen capture based on app, window title, and text content.
    Returns a Classification with category, confidence, and extracted keywords.
    """
    app_lower = app_name.lower().strip()
    title_lower = window_title.lower() if window_title else ""
    text_lower = text.lower() if text else ""

    # Step 1: App-based classification (highest confidence)
    category = "unknown"
    subcategory = ""
    confidence = 0.0

    for app_key, (cat, subcat) in APP_CATEGORIES.items():
        if app_key in app_lower:
            category = cat
            subcategory = subcat
            confidence = 0.85
            break

    # Step 2: Window title refinement
    if category == "browser":
        # Try to classify browser content by title/URL
        if any(w in title_lower for w in ["github", "gitlab", "bitbucket", "stackoverflow"]):
            category = "code"
            subcategory = "browser-dev"
            confidence = 0.8
        elif any(w in title_lower for w in ["gmail", "outlook", "mail"]):
            category = "email"
            subcategory = "browser-email"
            confidence = 0.75
        elif any(w in title_lower for w in ["slack", "discord", "teams"]):
            category = "chat"
            subcategory = "browser-chat"
            confidence = 0.75
        elif any(w in title_lower for w in ["docs.google", "notion", "confluence"]):
            category = "docs"
            subcategory = "browser-docs"
            confidence = 0.75
        elif any(w in title_lower for w in ["figma", "canva"]):
            category = "design"
            subcategory = "browser-design"
            confidence = 0.75
        elif any(w in title_lower for w in ["youtube", "netflix", "twitch", "spotify"]):
            category = "media"
            subcategory = "browser-media"
            confidence = 0.75

    # Step 3: Terminal content refinement
    if category == "terminal":
        # Check if terminal is running code-related commands
        code_signals = sum(1 for p in CODE_PATTERNS if re.search(p, text_lower))
        if code_signals >= 2:
            subcategory = "terminal-dev"
            confidence = 0.9

    # Step 4: Text pattern analysis for unknown or low-confidence
    if confidence < 0.7:
        code_score = sum(1 for p in CODE_PATTERNS if re.search(p, text_lower))
        chat_score = sum(1 for p in CHAT_PATTERNS if re.search(p, text_lower))
        url_score = sum(1 for p in BROWSER_URL_PATTERNS if re.search(p, text_lower))

        scores = {
            "code": code_score * 2,
            "chat": chat_score * 3,
            "browser": url_score * 2,
        }
        best = max(scores, key=scores.get)
        if scores[best] >= 3:
            category = best
            confidence = min(0.7, 0.3 + scores[best] * 0.1)

    # Step 5: Extract keywords
    keywords = extract_keywords(text, category)

    # Step 6: Productivity heuristic
    is_productive = category in PRODUCTIVE_CATEGORIES

    return Classification(
        category=category,
        confidence=round(confidence, 2),
        subcategory=subcategory,
        keywords=keywords[:10],
        is_productive=is_productive,
    )


def extract_keywords(text: str, category: str) -> list[str]:
    """Extract relevant keywords from screen text based on content category."""
    if not text:
        return []

    words = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{2,30}\b', text)
    word_freq = {}
    for w in words:
        wl = w.lower()
        # Skip common stop words
        if wl in _STOP_WORDS:
            continue
        word_freq[wl] = word_freq.get(wl, 0) + 1

    # Sort by frequency, return top words
    sorted_words = sorted(word_freq.items(), key=lambda x: -x[1])
    return [w for w, c in sorted_words if c >= 2][:10]


_STOP_WORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "her", "was", "one", "our", "out", "has", "have", "had", "this", "that",
    "with", "from", "they", "been", "said", "each", "which", "their",
    "will", "way", "about", "many", "then", "them", "would", "like",
    "more", "some", "time", "very", "when", "what", "your", "how",
    "new", "now", "old", "see", "just", "also", "back", "after",
    "use", "two", "first", "well", "than", "only", "come", "its",
    "over", "such", "take", "into", "most", "make", "could",
    "class", "def", "return", "import", "from", "true", "false", "none",
    "self", "print", "function", "const", "let", "var",
}


def classify_batch(entries: list) -> dict:
    """
    Classify a batch of entries and return aggregate stats.
    Returns category breakdown with time estimates.
    """
    categories = {}
    for entry in entries:
        c = classify_capture(entry.app_name, entry.window_title, entry.text)
        key = c.category
        if key not in categories:
            categories[key] = {
                "count": 0,
                "subcategories": {},
                "keywords": {},
                "productive_frames": 0,
            }
        categories[key]["count"] += 1
        if c.is_productive:
            categories[key]["productive_frames"] += 1

        sub = c.subcategory
        categories[key]["subcategories"][sub] = categories[key]["subcategories"].get(sub, 0) + 1

        for kw in c.keywords:
            categories[key]["keywords"][kw] = categories[key]["keywords"].get(kw, 0) + 1

    # Sort keywords within each category
    for cat in categories.values():
        sorted_kw = sorted(cat["keywords"].items(), key=lambda x: -x[1])
        cat["top_keywords"] = [w for w, c in sorted_kw[:8]]
        del cat["keywords"]

    return categories
