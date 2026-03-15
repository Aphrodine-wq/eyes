"""
context_chain.py — Cross-app context flow tracking.

Tracks how information flows across apps. When you see something in Slack,
search for it in Chrome, then open a file in VS Code — Eyes knows the full
chain of how you got there.

This is the module that makes people question reality.
"""

import re
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Optional
from difflib import SequenceMatcher

from _stop_words import STOP_WORDS as _CONTEXT_STOP_WORDS


@dataclass
class ContextNode:
    """A single node in the context chain."""
    timestamp: float
    app_name: str
    window_title: str
    text_fingerprint: str   # short representative text
    keywords: list[str]
    category: str = ""


@dataclass
class ContextTransition:
    """A tracked transition between contexts."""
    from_node: ContextNode
    to_node: ContextNode
    shared_terms: list[str]     # terms that carried across the transition
    transition_type: str        # "search", "copy-paste", "reference", "navigate", "switch"
    confidence: float


@dataclass
class ContextChain:
    """A full chain of related context transitions."""
    nodes: list[ContextNode]
    transitions: list[ContextTransition]
    topic: str                  # inferred topic of the chain
    duration_seconds: float
    apps_involved: list[str]

    def narrative(self) -> str:
        """Generate a human-readable narrative of the context chain."""
        if not self.nodes:
            return "No context chain detected."

        parts = []
        for i, node in enumerate(self.nodes):
            from datetime import datetime
            ts = datetime.fromtimestamp(node.timestamp).strftime("%H:%M")

            if i == 0:
                parts.append(f"Started in {node.app_name} at {ts}")
                if node.text_fingerprint:
                    parts.append(f"  saw: \"{node.text_fingerprint}\"")
            else:
                transition = self.transitions[i - 1] if i - 1 < len(self.transitions) else None
                verb = _transition_verb(transition.transition_type if transition else "switch")
                parts.append(f"  -> {verb} {node.app_name} at {ts}")
                if transition and transition.shared_terms:
                    parts.append(f"     carrying: {', '.join(transition.shared_terms[:5])}")
                if node.text_fingerprint:
                    parts.append(f"     context: \"{node.text_fingerprint}\"")

        dur = self.duration_seconds
        if dur > 3600:
            dur_str = f"{dur/3600:.1f}h"
        elif dur > 60:
            dur_str = f"{dur/60:.0f}min"
        else:
            dur_str = f"{dur:.0f}s"

        parts.append(f"\nChain: {' -> '.join(self.apps_involved)} ({dur_str})")
        if self.topic:
            parts.append(f"Topic: {self.topic}")

        return "\n".join(parts)


def _transition_verb(t_type: str) -> str:
    return {
        "search": "searched in",
        "copy-paste": "pasted into",
        "reference": "referenced in",
        "navigate": "navigated to",
        "switch": "switched to",
    }.get(t_type, "moved to")


class ContextTracker:
    """
    Tracks context flow across app transitions.
    Maintains a sliding window of recent captures and detects
    when content from one app carries into another.
    """

    def __init__(self, window_size: int = 50):
        self.window: deque[ContextNode] = deque(maxlen=window_size)
        self.chains: list[ContextChain] = []
        self.active_chain_nodes: list[ContextNode] = []
        self.active_chain_transitions: list[ContextTransition] = []
        self._last_app = ""
        self._last_keywords: set[str] = set()

    def record(self, timestamp: float, app_name: str, window_title: str,
               text: str, category: str = "") -> Optional[ContextTransition]:
        """
        Record a new capture and detect context transitions.
        Returns a transition if one was detected.
        """
        # Extract keywords and fingerprint
        keywords = _extract_terms(text)
        fingerprint = _text_fingerprint(text, window_title)

        node = ContextNode(
            timestamp=timestamp,
            app_name=app_name,
            window_title=window_title,
            text_fingerprint=fingerprint,
            keywords=keywords,
            category=category,
        )
        self.window.append(node)

        # Detect app transition
        if app_name != self._last_app and self._last_app:
            transition = self._detect_transition(node)

            if transition:
                if not self.active_chain_nodes:
                    # Start new chain with previous node
                    prev = self._find_previous_node()
                    if prev:
                        self.active_chain_nodes.append(prev)

                self.active_chain_nodes.append(node)
                self.active_chain_transitions.append(transition)
            else:
                # No shared context — close current chain if exists
                self._close_chain()
                self.active_chain_nodes = [node]
                self.active_chain_transitions = []

        self._last_app = app_name
        self._last_keywords = set(keywords)

        return None

    def _detect_transition(self, current: ContextNode) -> Optional[ContextTransition]:
        """Detect if a meaningful context transition occurred."""
        if not self.window or len(self.window) < 2:
            return None

        # Find the most recent node from the previous app
        prev_node = self._find_previous_node()
        if not prev_node:
            return None

        # Find shared terms between previous and current context
        prev_terms = set(prev_node.keywords)
        curr_terms = set(current.keywords)
        shared = prev_terms & curr_terms

        if not shared:
            # Check window title similarity
            title_sim = _similarity(prev_node.window_title, current.window_title)
            if title_sim > 0.3:
                shared = _extract_shared_title_terms(prev_node.window_title, current.window_title)

        if not shared:
            return None

        # Determine transition type
        t_type = _infer_transition_type(prev_node, current, shared)

        # Calculate confidence based on shared term count and text similarity
        text_sim = _similarity(prev_node.text_fingerprint, current.text_fingerprint)
        confidence = min(1.0, (len(shared) * 0.15) + (text_sim * 0.5))

        if confidence < 0.2:
            return None

        return ContextTransition(
            from_node=prev_node,
            to_node=current,
            shared_terms=sorted(shared)[:10],
            transition_type=t_type,
            confidence=round(confidence, 2),
        )

    def _find_previous_node(self) -> Optional[ContextNode]:
        """Find the most recent node from a different app."""
        for node in reversed(self.window):
            if node.app_name != self._last_app:
                return node
        return None

    def _close_chain(self):
        """Close the active chain and store it."""
        if len(self.active_chain_nodes) >= 2:
            apps = []
            seen = set()
            for n in self.active_chain_nodes:
                if n.app_name not in seen:
                    apps.append(n.app_name)
                    seen.add(n.app_name)

            # Infer topic from most common shared terms
            all_shared = []
            for t in self.active_chain_transitions:
                all_shared.extend(t.shared_terms)

            from collections import Counter
            term_counts = Counter(all_shared)
            topic = ", ".join(w for w, _ in term_counts.most_common(3)) if term_counts else ""

            chain = ContextChain(
                nodes=list(self.active_chain_nodes),
                transitions=list(self.active_chain_transitions),
                topic=topic,
                duration_seconds=self.active_chain_nodes[-1].timestamp - self.active_chain_nodes[0].timestamp,
                apps_involved=apps,
            )
            self.chains.append(chain)

            # Keep bounded
            if len(self.chains) > 100:
                self.chains = self.chains[-50:]

    def get_current_chain(self) -> Optional[ContextChain]:
        """Get the currently active context chain."""
        if len(self.active_chain_nodes) < 2:
            return None

        apps = list(dict.fromkeys(n.app_name for n in self.active_chain_nodes))
        all_shared = []
        for t in self.active_chain_transitions:
            all_shared.extend(t.shared_terms)

        from collections import Counter
        term_counts = Counter(all_shared)
        topic = ", ".join(w for w, _ in term_counts.most_common(3)) if term_counts else ""

        return ContextChain(
            nodes=list(self.active_chain_nodes),
            transitions=list(self.active_chain_transitions),
            topic=topic,
            duration_seconds=time.time() - self.active_chain_nodes[0].timestamp,
            apps_involved=apps,
        )

    def get_recent_chains(self, limit: int = 10) -> list[ContextChain]:
        """Get recent completed context chains."""
        current = self.get_current_chain()
        chains = list(self.chains[-limit:])
        if current:
            chains.append(current)
        return chains

    def find_forgotten_context(self, current_text: str, hours: int = 4) -> list[ContextNode]:
        """
        Find past context nodes that are relevant to the current screen
        but happened long enough ago that the user might have forgotten.

        This is the 'phantom memory' feature — Claude surfaces things
        you saw hours ago that are relevant right now.
        """
        current_terms = set(_extract_terms(current_text))
        if not current_terms:
            return []

        cutoff = time.time() - (hours * 3600)
        recent_cutoff = time.time() - 600  # ignore last 10 min (not "forgotten" yet)

        forgotten = []
        for node in self.window:
            if node.timestamp < cutoff or node.timestamp > recent_cutoff:
                continue
            node_terms = set(node.keywords)
            overlap = current_terms & node_terms
            if len(overlap) >= 2:
                forgotten.append(node)

        # Deduplicate by app + fingerprint
        seen = set()
        unique = []
        for n in forgotten:
            key = (n.app_name, n.text_fingerprint)
            if key not in seen:
                seen.add(key)
                unique.append(n)

        return unique[-10:]


def _extract_terms(text: str) -> list[str]:
    """Extract meaningful terms from text for context tracking."""
    if not text:
        return []

    # Extract identifiers, URLs, paths, meaningful words
    terms = set()

    # CamelCase and snake_case identifiers
    terms.update(re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', text))
    terms.update(re.findall(r'\b[a-z]+(?:_[a-z]+){1,5}\b', text))

    # File paths and extensions
    terms.update(re.findall(r'[\w/]+\.(?:py|js|ts|jsx|tsx|swift|rs|go|rb|java|css|html|md|json|yaml|yml)\b', text))

    # URLs (domain only)
    for url in re.findall(r'(?:https?://)?(?:www\.)?([a-zA-Z0-9-]+\.[a-z]{2,})', text):
        terms.add(url.lower())

    # Error-like patterns
    terms.update(re.findall(r'\b(?:Error|Exception|Failed|FAIL|error|fatal|panic|crash)\b', text))

    # Git-related
    terms.update(re.findall(r'\b(?:commit|branch|merge|pull|push|rebase|cherry-pick)\b', text, re.I))

    # Regular significant words (3+ chars, not stop words)
    words = re.findall(r'\b[a-zA-Z]{3,20}\b', text)
    word_freq = {}
    for w in words:
        wl = w.lower()
        if wl not in _CONTEXT_STOP_WORDS:
            word_freq[wl] = word_freq.get(wl, 0) + 1
    # Only include words that appear 2+ times (meaningful)
    for w, c in word_freq.items():
        if c >= 2:
            terms.add(w)

    return list(terms)[:30]


def _text_fingerprint(text: str, window_title: str = "") -> str:
    """Create a short representative fingerprint of screen content."""
    # Prefer window title if meaningful
    if window_title and len(window_title) > 5:
        return window_title[:100]

    # Otherwise take first meaningful line
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if len(stripped) > 15:
            return stripped[:100]

    return (text or "")[:100]


def _similarity(a: str, b: str) -> float:
    """Quick text similarity check."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower()[:200], b.lower()[:200]).ratio()


def _extract_shared_title_terms(title1: str, title2: str) -> set[str]:
    """Extract shared meaningful terms from two window titles."""
    t1 = set(re.findall(r'\b\w{3,}\b', title1.lower()))
    t2 = set(re.findall(r'\b\w{3,}\b', title2.lower()))
    shared = (t1 & t2) - _CONTEXT_STOP_WORDS
    return shared


def _infer_transition_type(prev: ContextNode, curr: ContextNode, shared: set) -> str:
    """Infer what type of transition occurred."""
    curr_lower = curr.app_name.lower()
    prev_lower = prev.app_name.lower()

    # Browser search patterns
    search_apps = {"google chrome", "safari", "firefox", "arc", "brave browser"}
    if curr_lower in search_apps and any(w in curr.window_title.lower() for w in ["search", "google", "bing"]):
        return "search"

    # Terminal after code editor = likely running/testing
    terminal_apps = {"terminal", "iterm2", "iterm", "warp", "alacritty"}
    code_apps = {"xcode", "visual studio code", "code", "cursor", "sublime text"}
    if curr_lower in terminal_apps and prev_lower in code_apps:
        return "reference"

    # Code editor after browser = likely implementing something found
    if curr_lower in code_apps and prev_lower in search_apps:
        return "reference"

    # Same content appearing in different app = likely copy-paste
    if len(shared) >= 5:
        return "copy-paste"

    return "navigate"


