"""
intelligence.py -- Unified intelligence layer for Claude Eyes.

One entry point that takes ANY natural language question and routes it to
the right combination of engines to answer it. Reasons across all 9
analysis engines simultaneously, then synthesizes their outputs into a
coherent narrative response.

This is god mode.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from store import EyesStore, ScreenEntry, parse_natural_time
from classifier import classify_capture, classify_batch, Classification
from flow import FlowDetector, FlowState
from context_chain import ContextTracker, ContextChain
from patterns import PatternEngine, Prediction, BehavioralAnomaly, WorkflowFingerprint
from semantic import TFIDFIndex, TopicModeler, SemanticResult
from insights import InsightsEngine, InsightReport
from timeline import build_timeline, Timeline
from digest import generate_daily_digest, compare_days
from adaptive import AdaptiveState


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass
class IntelligenceResponse:
    """The unified answer returned by the intelligence layer."""
    answer: str
    sources: list[str]
    confidence: float
    follow_up_suggestions: list[str]


# ---------------------------------------------------------------------------
# Question categories
# ---------------------------------------------------------------------------

class QuestionCategory:
    TEMPORAL = "temporal"
    ACTIVITY = "activity"
    FLOW = "flow"
    INTERRUPTION = "interruption"
    CONTEXT = "context"
    SEARCH = "search"
    COMPARATIVE = "comparative"
    ANOMALY = "anomaly"
    PREDICTIVE = "predictive"
    PATTERN = "pattern"
    INSIGHT = "insight"
    DEFAULT = "default"


# Keyword patterns for each category (compiled once)
_CATEGORY_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    (QuestionCategory.TEMPORAL, [
        re.compile(r"\b(when|what time|how long|how much time)\b", re.I),
        re.compile(r"\b(morning|afternoon|evening|yesterday|today|last\s+\w+\s+(hour|minute|day|week))\b", re.I),
        re.compile(r"\b(this\s+(morning|afternoon|evening|week))\b", re.I),
        re.compile(r"\b(between\s+\d|from\s+\d|since\s+\d)\b", re.I),
    ]),
    (QuestionCategory.ACTIVITY, [
        re.compile(r"\b(what was i doing|what have i been doing|what did i do)\b", re.I),
        re.compile(r"\b(activity|summary|recap|overview)\b", re.I),
        re.compile(r"\b(show me|tell me about)\s+(my\s+)?(day|work|session)\b", re.I),
    ]),
    (QuestionCategory.FLOW, [
        re.compile(r"\b(am i|was i)\s+(focused|productive|distracted|in flow|in the zone)\b", re.I),
        re.compile(r"\b(focus|flow\s*state|productivity|attention|scattered|deep work)\b", re.I),
        re.compile(r"\b(how\s+(focused|productive|distracted))\b", re.I),
    ]),
    (QuestionCategory.INTERRUPTION, [
        re.compile(r"\b(what\s+interrupted|what\s+broke|what\s+disrupted|flow\s*breaker)\b", re.I),
        re.compile(r"\b(distraction|interrupt|broke\s+(my\s+)?focus)\b", re.I),
        re.compile(r"\b(why did i (stop|switch|leave))\b", re.I),
    ]),
    (QuestionCategory.CONTEXT, [
        re.compile(r"\b(how did i get here|what led to|what brought me)\b", re.I),
        re.compile(r"\b(context\s*chain|path|trail|breadcrumb)\b", re.I),
        re.compile(r"\b(what was (the|my) chain)\b", re.I),
    ]),
    (QuestionCategory.SEARCH, [
        re.compile(r"\b(find|where did i see|search|look for|locate)\b", re.I),
        re.compile(r"\b(when did i (see|visit|open|read|look at))\b", re.I),
        re.compile(r"\b(have i (seen|visited|opened))\b", re.I),
    ]),
    (QuestionCategory.COMPARATIVE, [
        re.compile(r"\b(compare|versus|vs\.?|compared to|better|worse)\b", re.I),
        re.compile(r"\b(more productive|less productive)\s+(today|yesterday|than)\b", re.I),
        re.compile(r"\b(today\s+vs|yesterday\s+vs|monday\s+vs)\b", re.I),
    ]),
    (QuestionCategory.ANOMALY, [
        re.compile(r"\b(what.s\s+different|anything\s+(unusual|weird|different|off))\b", re.I),
        re.compile(r"\b(anomal|abnormal|out\s+of\s+(the\s+)?ordinary)\b", re.I),
        re.compile(r"\b(strange|unexpected)\b", re.I),
    ]),
    (QuestionCategory.PREDICTIVE, [
        re.compile(r"\b(what.s\s+next|what\s+will\s+i|predict|what.s\s+coming)\b", re.I),
        re.compile(r"\b(next\s+app|what\s+should\s+i\s+(do|open))\b", re.I),
        re.compile(r"\b(probably\s+(going|gonna)\s+do)\b", re.I),
    ]),
    (QuestionCategory.PATTERN, [
        re.compile(r"\b(pattern|habit|routine|recurring|workflow)\b", re.I),
        re.compile(r"\b(what\s+are\s+my\s+(patterns|habits|routines))\b", re.I),
        re.compile(r"\b(do\s+i\s+(always|usually|tend\s+to))\b", re.I),
    ]),
    (QuestionCategory.INSIGHT, [
        re.compile(r"\b(give\s+me\s+insights?|how\s+am\s+i\s+doing)\b", re.I),
        re.compile(r"\b(insight|recommendation|suggest|advice|tips)\b", re.I),
        re.compile(r"\b(improve|optimize|better\s+at)\b", re.I),
        re.compile(r"\b(tell\s+me\s+(something|anything)\s+(about|interesting))\b", re.I),
    ]),
]


# ---------------------------------------------------------------------------
# Time expression extraction
# ---------------------------------------------------------------------------

_TIME_EXPRESSIONS = [
    (re.compile(r"\b(this\s+morning)\b", re.I), "this morning"),
    (re.compile(r"\b(this\s+afternoon)\b", re.I), "this afternoon"),
    (re.compile(r"\b(this\s+evening)\b", re.I), "this evening"),
    (re.compile(r"\b(yesterday\s+morning)\b", re.I), "yesterday morning"),
    (re.compile(r"\b(yesterday\s+afternoon)\b", re.I), "yesterday afternoon"),
    (re.compile(r"\b(yesterday)\b", re.I), "yesterday"),
    (re.compile(r"\b(today)\b", re.I), "today"),
    (re.compile(r"\b(this\s+week)\b", re.I), "this week"),
    (re.compile(r"\b(last\s+week)\b", re.I), "last week"),
    (re.compile(r"\blast\s+(\d+)\s+(hours?|minutes?|mins?|days?|weeks?)\b", re.I), None),  # dynamic
]


def _extract_time_expression(question: str) -> Optional[str]:
    """Pull a natural-time expression out of the question text."""
    q = question.lower()
    for pattern, static_value in _TIME_EXPRESSIONS:
        m = pattern.search(q)
        if m:
            if static_value is not None:
                return static_value
            # Dynamic "last N units" pattern
            return m.group(0)
    return None


def _extract_search_query(question: str) -> str:
    """Pull the search target out of a search question."""
    # Remove question scaffolding, keep the meat
    q = question.strip()
    q = re.sub(r"^(find|search\s+for|look\s+for|where\s+did\s+i\s+see|locate)\s+", "", q, flags=re.I)
    q = re.sub(r"^(have\s+i\s+(seen|visited|opened))\s+", "", q, flags=re.I)
    q = re.sub(r"^(when\s+did\s+i\s+(see|visit|open|read|look\s+at))\s+", "", q, flags=re.I)
    q = re.sub(r"\?$", "", q).strip()
    # Remove wrapping quotes
    q = q.strip("\"'")
    return q if q else question


# ---------------------------------------------------------------------------
# IntelligenceEngine
# ---------------------------------------------------------------------------

class IntelligenceEngine:
    """
    The unified intelligence layer.

    Takes any natural language question, classifies it, routes to the
    appropriate combination of analysis engines, and synthesizes the
    results into a coherent narrative answer.
    """

    def __init__(self):
        self._flow_detector = FlowDetector()
        self._context_tracker = ContextTracker()
        self._pattern_engine = PatternEngine()
        self._insights_engine = InsightsEngine()
        self._tfidf_index = TFIDFIndex()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def answer(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """
        Answer any natural language question using all available engines.

        This is the single entry point. It classifies the question,
        determines which engines to invoke, runs them, and synthesizes
        the outputs into one coherent response.
        """
        categories = self._classify_question(question)
        primary = categories[0] if categories else QuestionCategory.DEFAULT

        # Hydrate the flow detector with recent data so it has state
        self._hydrate_flow(store)

        # Route to the appropriate handler(s)
        handler_map = {
            QuestionCategory.TEMPORAL: self._handle_temporal,
            QuestionCategory.ACTIVITY: self._handle_activity,
            QuestionCategory.FLOW: self._handle_flow,
            QuestionCategory.INTERRUPTION: self._handle_interruption,
            QuestionCategory.CONTEXT: self._handle_context,
            QuestionCategory.SEARCH: self._handle_search,
            QuestionCategory.COMPARATIVE: self._handle_comparative,
            QuestionCategory.ANOMALY: self._handle_anomaly,
            QuestionCategory.PREDICTIVE: self._handle_predictive,
            QuestionCategory.PATTERN: self._handle_pattern,
            QuestionCategory.INSIGHT: self._handle_insight,
            QuestionCategory.DEFAULT: self._handle_default,
        }

        handler = handler_map.get(primary, self._handle_default)

        # For complex questions touching multiple categories, fuse results
        if len(categories) >= 2:
            return self._handle_multi(question, categories, store)

        return handler(question, store)

    # ------------------------------------------------------------------
    # Question classification
    # ------------------------------------------------------------------

    def _classify_question(self, question: str) -> list[str]:
        """
        Classify the question into one or more categories using keyword
        pattern matching. Returns categories ranked by match strength.
        """
        scores: dict[str, int] = {}
        for category, patterns in _CATEGORY_PATTERNS:
            hits = sum(1 for p in patterns if p.search(question))
            if hits > 0:
                scores[category] = hits

        if not scores:
            return [QuestionCategory.DEFAULT]

        ranked = sorted(scores.keys(), key=lambda c: -scores[c])
        return ranked

    # ------------------------------------------------------------------
    # Hydration helpers
    # ------------------------------------------------------------------

    def _hydrate_flow(self, store: EyesStore, minutes: int = 30):
        """Load recent captures into the flow detector so it has state."""
        entries = store.get_recent(minutes=minutes, limit=200)
        for entry in reversed(entries):
            cls = classify_capture(entry.app_name, entry.window_title, entry.text)
            self._flow_detector.record(entry.timestamp, entry.app_name, cls.category)
            self._context_tracker.record(
                entry.timestamp, entry.app_name, entry.window_title,
                entry.text, cls.category,
            )

    def _build_semantic_index(self, store: EyesStore, minutes: int = 120) -> list[ScreenEntry]:
        """Build the TF-IDF index from recent captures."""
        entries = store.get_recent(minutes=minutes, limit=500)
        if entries:
            self._tfidf_index.build(entries)
        return entries

    # ------------------------------------------------------------------
    # Individual handlers
    # ------------------------------------------------------------------

    def _handle_temporal(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle time-related questions: 'what did I do this morning?'"""
        time_expr = _extract_time_expression(question)
        if time_expr:
            start, end = parse_natural_time(time_expr)
        else:
            start, end = parse_natural_time("last 1 hour")
            time_expr = "the last hour"

        entries = store.get_by_time_range(start, end, limit=500)
        sources = ["parse_natural_time", "get_by_time_range"]

        if not entries:
            return IntelligenceResponse(
                answer=f"No screen activity recorded for {time_expr}.",
                sources=sources,
                confidence=0.9,
                follow_up_suggestions=[
                    "What about today overall?",
                    "When was I last active?",
                ],
            )

        # Build activity summary for the period
        summary = store.get_activity_summary(
            minutes=max(1, int((end - start) / 60))
        )

        # Also classify the period
        cat_breakdown = classify_batch(entries)
        sources.append("classify_batch")

        # Get flow state for the period
        period_flow = self._flow_detector.get_flow_state()
        sources.append("FlowDetector")

        # Synthesize
        parts = [f"During {time_expr} ({len(entries)} captures):\n"]
        parts.append(summary)

        # Add category insight
        if cat_breakdown:
            productive = sum(
                info["productive_frames"] for info in cat_breakdown.values()
            )
            total = sum(info["count"] for info in cat_breakdown.values())
            prod_pct = round((productive / total) * 100) if total else 0
            parts.append(f"\nProductivity: {prod_pct}% of captures were productive work.")

        answer_text = "\n".join(parts)

        return IntelligenceResponse(
            answer=answer_text,
            sources=sources,
            confidence=0.85,
            follow_up_suggestions=[
                "Am I focused right now?",
                "What interrupted me during that time?",
                f"Compare {time_expr} to yesterday",
            ],
        )

    def _handle_activity(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'what was I doing' questions."""
        time_expr = _extract_time_expression(question)
        if time_expr:
            minutes = max(1, int(
                (parse_natural_time(time_expr)[1] - parse_natural_time(time_expr)[0]) / 60
            ))
        else:
            minutes = 60

        summary = store.get_activity_summary(minutes=minutes)
        sessions = store.get_sessions(hours=max(1, minutes // 60))
        sources = ["get_activity_summary", "get_sessions"]

        parts = [summary]

        if sessions:
            parts.append(f"\nDetected {len(sessions)} work session(s):")
            for i, s in enumerate(sessions[-5:], 1):
                start_str = datetime.fromtimestamp(s.start).strftime("%H:%M")
                parts.append(f"  {i}. {start_str} -- {s.summary}")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.85,
            follow_up_suggestions=[
                "Am I in flow right now?",
                "What are my patterns this week?",
                "Give me insights about my work habits",
            ],
        )

    def _handle_flow(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle focus/productivity questions."""
        flow_state = self._flow_detector.get_flow_state()
        attention = self._flow_detector.get_attention_profile(hours=4)
        sources = ["FlowDetector", "get_attention_profile"]

        # Also classify recent work
        entries = store.get_recent(minutes=30, limit=100)
        cat_breakdown = classify_batch(entries) if entries else {}
        if cat_breakdown:
            sources.append("classify_batch")

        parts = [
            f"Flow state: {flow_state.state.replace('_', ' ').title()} (score: {flow_state.score})",
            flow_state.description(),
            "",
        ]

        # Attention profile summary
        if attention.avg_flow_score > 0:
            parts.append(f"Attention profile (last 4 hours):")
            parts.append(f"  Average flow score: {attention.avg_flow_score}")
            parts.append(f"  Deep focus time: {attention.total_deep_focus_minutes:.0f}min")
            parts.append(f"  Scattered time: {attention.total_scattered_minutes:.0f}min")
            if attention.top_flow_apps:
                parts.append(f"  Best flow apps: {', '.join(attention.top_flow_apps[:3])}")
            parts.append(f"  Avg session before switch: {attention.avg_session_before_switch:.1f}min")

        # Category productivity
        if cat_breakdown:
            productive = sum(info["productive_frames"] for info in cat_breakdown.values())
            total = sum(info["count"] for info in cat_breakdown.values())
            prod_pct = round((productive / total) * 100) if total else 0
            parts.append(f"\nRecent work: {prod_pct}% productive ({total} captures)")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.8,
            follow_up_suggestions=[
                "What interrupted my focus?",
                "What are my peak hours?",
                "How does today compare to yesterday?",
            ],
        )

    def _handle_interruption(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'what interrupted me' questions."""
        breakers = self._flow_detector.detect_flow_breakers(minutes=120)
        sources = ["detect_flow_breakers"]

        if not breakers:
            # Check wider window
            breakers = self._flow_detector.detect_flow_breakers(minutes=480)
            if not breakers:
                return IntelligenceResponse(
                    answer="No significant flow interruptions detected recently. "
                           "Either you haven't had extended focus periods, or "
                           "nothing has broken them.",
                    sources=sources,
                    confidence=0.7,
                    follow_up_suggestions=[
                        "Am I focused right now?",
                        "What was I doing this morning?",
                        "What are my distraction patterns?",
                    ],
                )

        parts = [f"Found {len(breakers)} flow interruption(s):\n"]
        for b in breakers:
            ts = datetime.fromtimestamp(b["timestamp"]).strftime("%H:%M")
            parts.append(
                f"  {ts} -- {b['breaker_app']} broke a {b['broken_duration_min']:.0f}min "
                f"focus session in {b['broken_app']}"
            )

        # Aggregate: which apps break focus most?
        breaker_apps = [b["breaker_app"] for b in breakers]
        from collections import Counter
        breaker_counts = Counter(breaker_apps)
        if breaker_counts:
            worst = breaker_counts.most_common(1)[0]
            parts.append(f"\nTop focus-breaker: {worst[0]} ({worst[1]} interruptions)")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.8,
            follow_up_suggestions=[
                "How can I improve my focus?",
                "What are my patterns this week?",
                "Am I in flow right now?",
            ],
        )

    def _handle_context(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'how did I get here' questions."""
        chain = self._context_tracker.get_current_chain()
        recent_chains = self._context_tracker.get_recent_chains(limit=5)
        sources = ["ContextTracker.get_current_chain"]

        if chain:
            parts = [
                "Here is how you arrived at your current context:\n",
                chain.narrative(),
            ]
        elif recent_chains:
            parts = ["No active context chain right now, but here are recent chains:\n"]
            for c in recent_chains[-3:]:
                parts.append(c.narrative())
                parts.append("")
            sources.append("get_recent_chains")
        else:
            return IntelligenceResponse(
                answer="Not enough context transitions tracked yet to build a chain. "
                       "Keep working and the context tracker will pick up how "
                       "information flows across your apps.",
                sources=sources,
                confidence=0.5,
                follow_up_suggestions=[
                    "What was I doing recently?",
                    "Find something I was looking at",
                    "What apps have I used?",
                ],
            )

        # Enrich with forgotten context
        latest = store.get_latest()
        if latest:
            forgotten = self._context_tracker.find_forgotten_context(latest.text)
            if forgotten:
                parts.append("\nRelated context you might have forgotten:")
                for node in forgotten[:3]:
                    ts = datetime.fromtimestamp(node.timestamp).strftime("%H:%M")
                    parts.append(f"  {ts} in {node.app_name}: \"{node.text_fingerprint[:80]}\"")
                sources.append("find_forgotten_context")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.75,
            follow_up_suggestions=[
                "What was I doing before that?",
                "Find related content",
                "What interrupted my focus?",
            ],
        )

    def _handle_search(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'find [topic]' questions using both FTS and semantic search."""
        query = _extract_search_query(question)
        sources = []

        # FTS5 full-text search
        fts_results = store.search(query, limit=10)
        sources.append("FTS5_search")

        # Semantic TF-IDF search
        entries = self._build_semantic_index(store, minutes=480)
        tfidf_results = self._tfidf_index.search(query, top_k=10)
        sources.append("TFIDFIndex")

        # Merge and deduplicate results by entry ID
        seen_ids: set[int] = set()
        merged: list[tuple[ScreenEntry, float, str]] = []

        for entry in fts_results:
            if entry.id not in seen_ids:
                seen_ids.add(entry.id)
                merged.append((entry, 0.9, "full-text"))

        # Map TF-IDF results to entries
        entry_map = {e.id: e for e in entries} if entries else {}
        for doc_id, sim, shared in tfidf_results:
            if doc_id not in seen_ids and doc_id in entry_map:
                seen_ids.add(doc_id)
                merged.append((entry_map[doc_id], sim, "semantic"))

        if not merged:
            return IntelligenceResponse(
                answer=f"Could not find anything matching \"{query}\" in your screen history.",
                sources=sources,
                confidence=0.7,
                follow_up_suggestions=[
                    "Try a different search term",
                    "What was I doing today?",
                    "What apps have I been using?",
                ],
            )

        # Sort by score descending
        merged.sort(key=lambda x: -x[1])

        parts = [f"Found {len(merged)} result(s) for \"{query}\":\n"]
        for entry, score, method in merged[:8]:
            ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M %b %d")
            preview = (entry.text or "")[:120].replace("\n", " ").strip()
            title = entry.window_title[:60] if entry.window_title else ""
            parts.append(f"  [{ts}] {entry.app_name}")
            if title:
                parts.append(f"    {title}")
            if preview:
                parts.append(f"    \"{preview}...\"")
            parts.append(f"    (match: {method}, score: {score:.2f})")
            parts.append("")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=min(0.95, 0.5 + merged[0][1] * 0.5),
            follow_up_suggestions=[
                f"When did I first see \"{query}\"?",
                "How did I get to that content?",
                "What was I doing around that time?",
            ],
        )

    def _handle_comparative(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'compare today vs yesterday' questions."""
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)

        # Default comparison: today vs yesterday
        day1, day2 = yesterday, today
        label1, label2 = "yesterday", "today"

        # Try to detect specific days from question
        q_lower = question.lower()
        if "monday" in q_lower or "tuesday" in q_lower or "wednesday" in q_lower:
            # More sophisticated day parsing could go here
            pass

        comparison = compare_days(store, day1, day2)
        sources = ["compare_days"]

        # Also compare flow states
        today_entries = store.get_by_time_range(today.timestamp(), time.time(), limit=500)
        yesterday_entries = store.get_by_time_range(yesterday.timestamp(), today.timestamp(), limit=500)

        parts = [comparison]

        # Category comparison
        if today_entries and yesterday_entries:
            today_cats = classify_batch(today_entries)
            yesterday_cats = classify_batch(yesterday_entries)
            sources.append("classify_batch")

            today_prod = sum(c["productive_frames"] for c in today_cats.values())
            yesterday_prod = sum(c["productive_frames"] for c in yesterday_cats.values())
            today_total = sum(c["count"] for c in today_cats.values())
            yesterday_total = sum(c["count"] for c in yesterday_cats.values())

            today_pct = round((today_prod / today_total) * 100) if today_total else 0
            yesterday_pct = round((yesterday_prod / yesterday_total) * 100) if yesterday_total else 0

            parts.append(f"\n  Productivity: {label2} {today_pct}% vs {label1} {yesterday_pct}%")

            diff = today_pct - yesterday_pct
            if abs(diff) >= 5:
                direction = "more" if diff > 0 else "less"
                parts.append(f"  You are {abs(diff)}% {direction} productive {label2}.")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.85,
            follow_up_suggestions=[
                "What are my peak productive hours?",
                "What interrupted me today?",
                "Give me insights about this week",
            ],
        )

    def _handle_anomaly(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'what is different today' questions."""
        anomalies = self._pattern_engine.detect_anomalies(store)
        sources = ["detect_anomalies"]

        if not anomalies:
            return IntelligenceResponse(
                answer="Nothing significantly unusual detected today. "
                       "Your activity patterns are within normal range "
                       "compared to the last 7 days.",
                sources=sources,
                confidence=0.7,
                follow_up_suggestions=[
                    "What are my usual patterns?",
                    "How productive am I today?",
                    "Compare today to yesterday",
                ],
            )

        parts = [f"Detected {len(anomalies)} anomaly/anomalies today:\n"]
        for a in anomalies:
            severity_marker = {
                "info": "[i]",
                "notable": "[!]",
                "unusual": "[!!]",
                "significant": "[!!!]",
            }.get(a.severity, "[ ]")
            parts.append(f"  {severity_marker} {a.description}")
            parts.append(f"      Expected: {a.expected_value}  |  Actual: {a.actual_value}")
            parts.append("")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.8,
            follow_up_suggestions=[
                "Why am I spending so much time in that app?",
                "Am I more productive today?",
                "What are my usual patterns?",
            ],
        )

    def _handle_predictive(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'what will I do next' questions."""
        prediction = self._pattern_engine.predict_next_app(store)
        sources = ["predict_next_app"]

        if not prediction.predicted_app:
            return IntelligenceResponse(
                answer="Not enough historical data to make a prediction. "
                       "Keep using Eyes and it will learn your transition patterns.",
                sources=sources,
                confidence=0.3,
                follow_up_suggestions=[
                    "What are my patterns?",
                    "What was I doing recently?",
                    "What apps do I use most?",
                ],
            )

        parts = [
            f"Prediction: You will likely switch to {prediction.predicted_app} "
            f"(confidence: {prediction.confidence:.0%})",
            f"Reasoning: {prediction.reasoning}",
        ]

        if prediction.alternatives:
            parts.append("\nAlternatives:")
            for app, conf in prediction.alternatives:
                parts.append(f"  {app}: {conf:.0%}")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=prediction.confidence,
            follow_up_suggestions=[
                "What are my app transition patterns?",
                "Am I in flow right now?",
                "What have I been doing?",
            ],
        )

    def _handle_pattern(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'what are my patterns' questions."""
        workflows = self._pattern_engine.detect_workflows(store)
        sources = ["detect_workflows"]

        # Also get attention profile for pattern context
        attention = self._flow_detector.get_attention_profile(hours=8)
        sources.append("get_attention_profile")

        parts = []

        if workflows:
            parts.append(f"Detected {len(workflows)} recurring workflow(s):\n")
            for wf in workflows[:6]:
                seq = " -> ".join(wf.app_sequence)
                parts.append(f"  [{wf.name}] {seq}")
                parts.append(
                    f"    Seen {wf.occurrences}x, typically {wf.time_of_day} "
                    f"(~{wf.typical_duration_min:.0f}min, confidence: {wf.confidence:.0%})"
                )
                parts.append("")
        else:
            parts.append("No strong recurring workflows detected yet. "
                        "More data (3-7 days) will reveal your patterns.\n")

        # Attention patterns
        if attention.peak_focus_hour:
            parts.append("Focus patterns:")
            parts.append(f"  Peak focus hour: {attention.peak_focus_hour}:00")
            parts.append(f"  Worst focus hour: {attention.worst_focus_hour}:00")
            if attention.top_flow_apps:
                parts.append(f"  Flow apps: {', '.join(attention.top_flow_apps[:4])}")
            if attention.top_distraction_apps:
                parts.append(f"  Distraction apps: {', '.join(attention.top_distraction_apps[:4])}")
            parts.append(f"  Avg session length: {attention.avg_session_before_switch:.1f}min")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.75 if workflows else 0.5,
            follow_up_suggestions=[
                "Give me insights and recommendations",
                "Am I in flow right now?",
                "What is different about today?",
            ],
        )

    def _handle_insight(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """Handle 'give me insights' questions."""
        report = self._insights_engine.generate_report(store, days=7)
        sources = ["InsightsEngine"]

        parts = ["Here are your behavioral insights:\n"]

        # Peak / dead hours
        if report.peak_hours:
            peak_str = ", ".join(f"{h}:00" for h in report.peak_hours)
            dead_str = ", ".join(f"{h}:00" for h in report.dead_hours) if report.dead_hours else "N/A"
            parts.append(f"Peak productivity hours: {peak_str}")
            parts.append(f"Low-energy hours: {dead_str}")
            if report.most_productive_day:
                parts.append(f"Most productive day: {report.most_productive_day}")
            parts.append("")

        # Focus metrics
        if report.avg_deep_focus_duration > 0:
            parts.append(f"Average deep focus session: {report.avg_deep_focus_duration:.0f}min")
        if report.context_switch_cost_minutes > 0:
            parts.append(f"Context switch recovery cost: ~{report.context_switch_cost_minutes:.0f}min")
        parts.append("")

        # Habit loops
        if report.habit_loops:
            parts.append("Habit loops detected:")
            for loop in report.habit_loops[:4]:
                parts.append(f"  {loop.description}")
            parts.append("")

        # Correlations
        if report.correlations:
            parts.append("Interesting correlations:")
            for corr in report.correlations[:3]:
                parts.append(f"  {corr.description} (r={corr.correlation})")
            parts.append("")

        # Recommendations
        if report.recommendations:
            parts.append("Recommendations:")
            for rec in report.recommendations:
                impact_tag = f"[{rec.impact}]" if rec.impact else ""
                parts.append(f"  {impact_tag} {rec.title}")
                parts.append(f"    {rec.description}")
            parts.append("")

        if len(parts) <= 2:
            return IntelligenceResponse(
                answer="Not enough data to generate meaningful insights yet. "
                       "Eyes needs 3-5 days of capture history to find patterns.",
                sources=sources,
                confidence=0.4,
                follow_up_suggestions=[
                    "What was I doing today?",
                    "Am I focused right now?",
                    "What apps have I used?",
                ],
            )

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.8,
            follow_up_suggestions=[
                "What are my recurring workflows?",
                "Compare today vs yesterday",
                "What is different about today?",
            ],
        )

    def _handle_default(self, question: str, store: EyesStore) -> IntelligenceResponse:
        """
        Default handler when the question does not clearly match any
        category. Combines current screen state, recent context, and
        flow state for a general answer.
        """
        sources = []

        # Current state
        latest = store.get_latest()
        flow_state = self._flow_detector.get_flow_state()
        sources.extend(["get_latest", "FlowDetector"])

        # Recent activity summary
        summary = store.get_activity_summary(minutes=30)
        sources.append("get_activity_summary")

        parts = []

        if latest:
            ts = datetime.fromtimestamp(latest.timestamp).strftime("%H:%M")
            parts.append(f"Right now ({ts}): {latest.app_name}")
            if latest.window_title:
                parts.append(f"  Window: {latest.window_title}")
            parts.append(f"  Flow state: {flow_state.state.replace('_', ' ')} (score: {flow_state.score})")
            parts.append("")

        parts.append(summary)

        # Check for anomalies as bonus context
        anomalies = self._pattern_engine.detect_anomalies(store)
        if anomalies:
            parts.append(f"\nNote: {len(anomalies)} anomaly/anomalies detected today:")
            for a in anomalies[:2]:
                parts.append(f"  {a.description}")
            sources.append("detect_anomalies")

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=sources,
            confidence=0.6,
            follow_up_suggestions=[
                "Am I focused right now?",
                "What are my patterns?",
                "What was I doing this morning?",
                "Give me insights",
            ],
        )

    # ------------------------------------------------------------------
    # Multi-engine fusion for complex questions
    # ------------------------------------------------------------------

    def _handle_multi(self, question: str, categories: list[str],
                      store: EyesStore) -> IntelligenceResponse:
        """
        Handle complex questions that touch multiple categories.

        Invokes all relevant engines, then weaves their outputs into
        a single coherent narrative. This is the synthesis step that
        makes the intelligence layer more than just a router.
        """
        all_sources: list[str] = []
        fragments: list[tuple[str, str, float]] = []  # (category, text, confidence)

        # Map each category to a handler and collect results
        handler_map = {
            QuestionCategory.TEMPORAL: self._handle_temporal,
            QuestionCategory.ACTIVITY: self._handle_activity,
            QuestionCategory.FLOW: self._handle_flow,
            QuestionCategory.INTERRUPTION: self._handle_interruption,
            QuestionCategory.CONTEXT: self._handle_context,
            QuestionCategory.SEARCH: self._handle_search,
            QuestionCategory.COMPARATIVE: self._handle_comparative,
            QuestionCategory.ANOMALY: self._handle_anomaly,
            QuestionCategory.PREDICTIVE: self._handle_predictive,
            QuestionCategory.PATTERN: self._handle_pattern,
            QuestionCategory.INSIGHT: self._handle_insight,
        }

        for cat in categories[:3]:  # Cap at 3 engines for performance
            handler = handler_map.get(cat)
            if handler:
                result = handler(question, store)
                fragments.append((cat, result.answer, result.confidence))
                all_sources.extend(result.sources)

        if not fragments:
            return self._handle_default(question, store)

        # Deduplicate sources
        seen = set()
        unique_sources = []
        for s in all_sources:
            if s not in seen:
                seen.add(s)
                unique_sources.append(s)

        # Synthesize: combine fragments with section headers
        if len(fragments) == 1:
            return IntelligenceResponse(
                answer=fragments[0][1],
                sources=unique_sources,
                confidence=fragments[0][2],
                follow_up_suggestions=self._suggest_follow_ups(categories),
            )

        parts = [f"Analyzing from {len(fragments)} perspectives:\n"]

        section_titles = {
            QuestionCategory.TEMPORAL: "Time Analysis",
            QuestionCategory.ACTIVITY: "Activity",
            QuestionCategory.FLOW: "Focus & Flow",
            QuestionCategory.INTERRUPTION: "Interruptions",
            QuestionCategory.CONTEXT: "Context Chain",
            QuestionCategory.SEARCH: "Search Results",
            QuestionCategory.COMPARATIVE: "Comparison",
            QuestionCategory.ANOMALY: "Anomalies",
            QuestionCategory.PREDICTIVE: "Prediction",
            QuestionCategory.PATTERN: "Patterns",
            QuestionCategory.INSIGHT: "Insights",
        }

        for cat, text, conf in fragments:
            title = section_titles.get(cat, cat.title())
            parts.append(f"--- {title} ---")
            parts.append(text)
            parts.append("")

        # Overall confidence is weighted average
        total_conf = sum(c for _, _, c in fragments) / len(fragments)

        return IntelligenceResponse(
            answer="\n".join(parts),
            sources=unique_sources,
            confidence=round(total_conf, 2),
            follow_up_suggestions=self._suggest_follow_ups(categories),
        )

    # ------------------------------------------------------------------
    # Follow-up suggestion generation
    # ------------------------------------------------------------------

    def _suggest_follow_ups(self, categories: list[str]) -> list[str]:
        """Generate contextually relevant follow-up suggestions."""
        suggestions_pool = {
            QuestionCategory.TEMPORAL: [
                "What was I doing this morning?",
                "How did yesterday compare?",
            ],
            QuestionCategory.ACTIVITY: [
                "Am I focused right now?",
                "What are my patterns this week?",
            ],
            QuestionCategory.FLOW: [
                "What interrupted my focus?",
                "What are my peak hours?",
            ],
            QuestionCategory.INTERRUPTION: [
                "How can I reduce distractions?",
                "What are my flow patterns?",
            ],
            QuestionCategory.CONTEXT: [
                "What was I working on before?",
                "Find related content",
            ],
            QuestionCategory.SEARCH: [
                "When did I first see that?",
                "What was I doing at that time?",
            ],
            QuestionCategory.COMPARATIVE: [
                "What are my weekly trends?",
                "Give me insights",
            ],
            QuestionCategory.ANOMALY: [
                "What are my usual patterns?",
                "Am I more productive today?",
            ],
            QuestionCategory.PREDICTIVE: [
                "What are my transition patterns?",
                "Am I in flow right now?",
            ],
            QuestionCategory.PATTERN: [
                "Give me recommendations",
                "What is different today?",
            ],
            QuestionCategory.INSIGHT: [
                "What are my workflows?",
                "Compare this week to last week",
            ],
        }

        result: list[str] = []
        # Suggest things from categories NOT already covered
        all_cats = set(suggestions_pool.keys())
        covered = set(categories)
        uncovered = all_cats - covered

        for cat in uncovered:
            result.extend(suggestions_pool.get(cat, []))
            if len(result) >= 4:
                break

        # Always include at least one from covered categories
        if categories:
            covered_suggestions = suggestions_pool.get(categories[0], [])
            if covered_suggestions:
                result.insert(0, covered_suggestions[0])

        return result[:4]
