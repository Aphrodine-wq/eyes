"""
knowledge.py — Persistent knowledge graph for Claude Eyes.

Extracts entities (people, files, URLs, projects, errors, commands, topics)
from screen captures and builds a graph of co-occurrence relationships.
Answers questions like "who was I talking about in Slack?", "what files
have I been working on?", or "what URLs did I visit related to auth?".

All data lives in the existing eyes.db — pass the EyesStore's connection
to KnowledgeGraph on init.
"""

import re
import json
import time
import sqlite3
from dataclasses import dataclass
from typing import Optional

# --- Data classes ---

@dataclass
class Entity:
    """A single extracted entity."""
    type: str           # person, file, url, project, error, command, topic
    value: str
    timestamp: float
    app_name: str
    confidence: float   # 0.0 - 1.0

@dataclass
class Relation:
    """A relationship between two entities."""
    entity_a: str
    entity_b: str
    relation_type: str  # co-occurred, mentioned-with, opened-from
    strength: int
    last_seen: float

@dataclass
class EntityInfo:
    """Full info about a stored entity."""
    id: int
    entity_type: str
    value: str
    first_seen: float
    last_seen: float
    occurrence_count: int
    apps: list[str]
    contexts: list[str]

@dataclass
class TimelineEntry:
    """When an entity was observed."""
    timestamp: float
    app_name: str
    window_title: str

# --- Regex patterns for entity extraction ---

# Person patterns
_RE_AT_MENTION = re.compile(r"@(\w{2,30})")
_RE_FROM_FIELD = re.compile(r"(?:From|Author|By|Sender)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)")
_RE_TO_FIELD = re.compile(r"(?:To|Recipient)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)")
_RE_GIT_AUTHOR = re.compile(r"(?:Author|Commit(?:ter)?)[:\s]+([A-Za-z][\w.-]+ [A-Za-z][\w.-]+)")
_RE_CHAT_NAME = re.compile(r"^([A-Z][a-z]{1,15} [A-Z][a-z]{1,15})(?:\s+[\d:]+\s*(?:AM|PM)?|:)", re.MULTILINE)

# File patterns — paths and filenames with common extensions
_FILE_EXTENSIONS = (
    r"\.(?:py|js|ts|tsx|jsx|rb|go|rs|java|cpp|hpp|cs|swift|kt|"
    r"sh|bash|zsh|fish|yml|yaml|json|toml|xml|html|css|scss|less|"
    r"md|txt|csv|sql|graphql|proto|tf|Dockerfile|Makefile|"
    r"env|cfg|ini|conf|lock|log)"
)
_RE_FILE_PATH = re.compile(r"(?<!//)(?:[~/.][\w./-]*|\b\w[\w.-]*/\w[\w./-]*)" + _FILE_EXTENSIONS + r"\b")
_RE_FILENAME = re.compile(r"\b[\w][\w.-]*" + _FILE_EXTENSIONS + r"\b")

# URL patterns
_RE_URL = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{4,200}"
)
_RE_DOMAIN = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|org|net|io|dev|app|co|ai|sh|me|edu|gov|cloud|xyz)\b"
)

# Project patterns
_RE_CD_PROJECT = re.compile(r"cd\s+(?:~/)?[\w./-]*?(\b[\w][\w.-]+)\s*$", re.MULTILINE)
_RE_GIT_REPO = re.compile(r"(?:git\s+clone\s+\S+/|origin\s+\S+/)(\w[\w.-]+?)(?:\.git)?(?:\s|$)")
_RE_WORKSPACE = re.compile(r"\[(\w[\w. -]+)\]\s*[-—]")

# Error patterns
_RE_ERROR = re.compile(
    r"^.*(?:Error|Exception|FAIL|FAILED|panic|Traceback|"
    r"FATAL|CRITICAL|TypeError|SyntaxError|RuntimeError|"
    r"ValueError|KeyError|ImportError|ModuleNotFoundError|"
    r"NameError|AttributeError|IndexError|FileNotFoundError|"
    r"ConnectionError|TimeoutError|PermissionError).*$",
    re.MULTILINE | re.IGNORECASE,
)

# Command patterns — lines starting with $ or common prompts
_RE_COMMAND = re.compile(
    r"^[\s]*(?:\$|>>>|>|%|#)\s+(.{3,120})$",
    re.MULTILINE,
)
_RE_SHELL_CMD = re.compile(
    r"\b((?:git|npm|pip|python|node|make|cargo|go|docker|kubectl|"
    r"brew|curl|wget|ssh|scp|rsync|tar|grep|find|sed|awk|"
    r"swift|xcodebuild|pod|yarn|pnpm|bun|deno)"
    r"(?:[^\S\n]+[^\s]+){1,5})"
)

# Stop words for topic extraction (common English + code keywords)
_STOP_WORDS = frozenset(
    "the a an is are was were be been being have has had do does did will would "
    "could should may might shall can need must to of in for on with at by from "
    "as into through during before after above below between out off over under "
    "again further then once here there when where why how all each every both "
    "few more most other some such no not only own same so than too very just "
    "because but and or if while this that these those it its i me my we our "
    "you your he him she her they them what which who whom new also like get "
    "set use see one two first last file true false null none return class def "
    "import".split()
)

_RE_TOPIC_WORD = re.compile(r"\b([a-zA-Z]{4,25})\b")

# --- Entity extractor ---

class EntityExtractor:
    """Regex-based entity extraction from screen capture text. No ML."""

    def extract(self, text: str, app_name: str, window_title: str) -> list[Entity]:
        """Extract all entities from a single screen capture."""
        now = time.time()
        if not text and not window_title:
            return []
        combined = f"{window_title}\n{text}" if window_title else text
        entities: list[Entity] = []
        entities.extend(self._extract_persons(combined, now, app_name))
        entities.extend(self._extract_files(combined, window_title, now, app_name))
        entities.extend(self._extract_urls(combined, now, app_name))
        entities.extend(self._extract_projects(combined, window_title, now, app_name))
        entities.extend(self._extract_errors(combined, now, app_name))
        entities.extend(self._extract_commands(combined, now, app_name))
        entities.extend(self._extract_topics(combined, now, app_name))
        return entities

    def _extract_persons(self, text: str, ts: float, app: str) -> list[Entity]:
        persons, seen = [], set()
        for pattern, conf in [(_RE_AT_MENTION, 0.7), (_RE_FROM_FIELD, 0.9),
                              (_RE_TO_FIELD, 0.9), (_RE_GIT_AUTHOR, 0.85),
                              (_RE_CHAT_NAME, 0.6)]:
            for m in pattern.finditer(text):
                name = m.group(1).strip()
                if len(name) < 2 or name.lower() in _STOP_WORDS:
                    continue
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    persons.append(Entity("person", name, ts, app, conf))
        return persons

    def _extract_files(self, text: str, window_title: str, ts: float, app: str) -> list[Entity]:
        files, seen = [], set()
        # Strip URLs before file matching to avoid false positives
        cleaned = _RE_URL.sub("", text)
        for pattern, conf in [(_RE_FILE_PATH, 0.9), (_RE_FILENAME, 0.7)]:
            for m in pattern.finditer(cleaned):
                val = m.group(0).strip()
                key = val.lower()
                if key not in seen:
                    seen.add(key)
                    files.append(Entity("file", val, ts, app, conf))
        if window_title:
            for m in _RE_FILENAME.finditer(window_title):
                val = m.group(0).strip()
                key = val.lower()
                if key not in seen:
                    seen.add(key)
                    files.append(Entity("file", val, ts, app, 0.85))
        return files

    def _extract_urls(self, text: str, ts: float, app: str) -> list[Entity]:
        urls, seen = [], set()
        for m in _RE_URL.finditer(text):
            val = m.group(0).rstrip(".,;:)\"'")
            key = val.lower()
            if key not in seen:
                seen.add(key)
                urls.append(Entity("url", val, ts, app, 0.95))
        for m in _RE_DOMAIN.finditer(text):
            val = m.group(0)
            key = val.lower()
            if key not in seen:
                seen.add(key)
                urls.append(Entity("url", val, ts, app, 0.6))
        return urls

    def _extract_projects(self, text: str, window_title: str, ts: float, app: str) -> list[Entity]:
        projects, seen = [], set()
        for pattern, conf in [(_RE_CD_PROJECT, 0.8), (_RE_GIT_REPO, 0.9),
                              (_RE_WORKSPACE, 0.7)]:
            for m in pattern.finditer(text):
                val = m.group(1).strip()
                if len(val) < 2:
                    continue
                key = val.lower()
                if key not in seen:
                    seen.add(key)
                    projects.append(Entity("project", val, ts, app, conf))
        return projects

    def _extract_errors(self, text: str, ts: float, app: str) -> list[Entity]:
        errors = []
        for m in _RE_ERROR.finditer(text):
            line = m.group(0).strip()
            if len(line) < 10 or len(line) > 300:
                continue
            errors.append(Entity("error", line[:200], ts, app, 0.85))
            if len(errors) >= 5:
                break
        return errors

    def _extract_commands(self, text: str, ts: float, app: str) -> list[Entity]:
        commands, seen = [], set()
        for pattern, conf in [(_RE_COMMAND, 0.8), (_RE_SHELL_CMD, 0.7)]:
            for m in pattern.finditer(text):
                val = m.group(1).strip() if pattern.groups else m.group(0).strip()
                if len(val) < 3:
                    continue
                key = val.lower()
                if key not in seen:
                    seen.add(key)
                    commands.append(Entity("command", val, ts, app, conf))
                    if len(commands) >= 10:
                        return commands
        return commands

    def _extract_topics(self, text: str, ts: float, app: str) -> list[Entity]:
        """Extract topic keywords -- high-signal words appearing 3+ times."""
        word_counts: dict[str, int] = {}
        for m in _RE_TOPIC_WORD.finditer(text.lower()):
            word = m.group(1)
            if word not in _STOP_WORDS and not word.isdigit():
                word_counts[word] = word_counts.get(word, 0) + 1
        topics = []
        for word, count in sorted(word_counts.items(), key=lambda x: -x[1]):
            if count >= 3:
                topics.append(Entity("topic", word, ts, app, min(0.9, 0.4 + count * 0.05)))
            if len(topics) >= 8:
                break
        return topics

# --- Knowledge graph (SQLite-backed) ---

class KnowledgeGraph:
    """Persistent knowledge graph stored in the existing eyes.db."""

    def __init__(self, conn: sqlite3.Connection):
        """Init with an existing SQLite connection (from EyesStore.conn)."""
        self.conn = conn
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_entities (
                id INTEGER PRIMARY KEY,
                entity_type TEXT NOT NULL,
                value TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                apps TEXT DEFAULT '',
                contexts TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_ke_type
                ON knowledge_entities(entity_type);
            CREATE INDEX IF NOT EXISTS idx_ke_value
                ON knowledge_entities(value);
            CREATE INDEX IF NOT EXISTS idx_ke_last_seen
                ON knowledge_entities(last_seen);

            CREATE TABLE IF NOT EXISTS knowledge_relations (
                id INTEGER PRIMARY KEY,
                entity_a_id INTEGER,
                entity_b_id INTEGER,
                relation_type TEXT,
                strength INTEGER DEFAULT 1,
                last_seen REAL,
                FOREIGN KEY (entity_a_id) REFERENCES knowledge_entities(id),
                FOREIGN KEY (entity_b_id) REFERENCES knowledge_entities(id)
            );

            CREATE INDEX IF NOT EXISTS idx_kr_a
                ON knowledge_relations(entity_a_id);
            CREATE INDEX IF NOT EXISTS idx_kr_b
                ON knowledge_relations(entity_b_id);
        """)
        self.conn.commit()

    def record_entities(self, entities: list[Entity], timestamp: float,
                        app_name: str, window_title: str):
        """Upsert entities and create co-occurrence relations among them."""
        if not entities:
            return
        entity_ids = [self._upsert_entity(e, timestamp, app_name, window_title)
                      for e in entities]
        # Create co-occurrence relations between every pair
        for i in range(len(entity_ids)):
            for j in range(i + 1, len(entity_ids)):
                if entity_ids[i] != entity_ids[j]:
                    self._upsert_relation(
                        entity_ids[i], entity_ids[j], "co-occurred", timestamp)
        self.conn.commit()

    def _upsert_entity(self, entity: Entity, timestamp: float,
                        app_name: str, window_title: str) -> int:
        """Insert or update an entity. Returns the entity id."""
        row = self.conn.execute(
            "SELECT id, apps, contexts FROM knowledge_entities "
            "WHERE entity_type = ? AND value = ?",
            (entity.type, entity.value)).fetchone()
        if row:
            eid, apps_json, contexts_json = row
            apps = _json_list(apps_json)
            contexts = _json_list(contexts_json)
            if app_name and app_name not in apps:
                apps.append(app_name)
            if window_title and window_title not in contexts:
                contexts = contexts[-19:] + [window_title]  # cap at 20
            self.conn.execute(
                "UPDATE knowledge_entities SET "
                "last_seen = ?, occurrence_count = occurrence_count + 1, "
                "apps = ?, contexts = ? WHERE id = ?",
                (timestamp, json.dumps(apps), json.dumps(contexts), eid))
            return eid
        else:
            apps = [app_name] if app_name else []
            contexts = [window_title] if window_title else []
            cur = self.conn.execute(
                "INSERT INTO knowledge_entities "
                "(entity_type, value, first_seen, last_seen, occurrence_count, apps, contexts) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (entity.type, entity.value, timestamp, timestamp,
                 json.dumps(apps), json.dumps(contexts)))
            return cur.lastrowid

    def _upsert_relation(self, a_id: int, b_id: int,
                          relation_type: str, timestamp: float):
        """Insert or strengthen a relation between two entities."""
        lo, hi = min(a_id, b_id), max(a_id, b_id)
        row = self.conn.execute(
            "SELECT id FROM knowledge_relations "
            "WHERE entity_a_id = ? AND entity_b_id = ? AND relation_type = ?",
            (lo, hi, relation_type)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE knowledge_relations SET strength = strength + 1, "
                "last_seen = ? WHERE id = ?", (timestamp, row[0]))
        else:
            self.conn.execute(
                "INSERT INTO knowledge_relations "
                "(entity_a_id, entity_b_id, relation_type, strength, last_seen) "
                "VALUES (?, ?, ?, 1, ?)", (lo, hi, relation_type, timestamp))

    def query_entity(self, entity_type: str, value: str) -> Optional[EntityInfo]:
        """Get full info about a specific entity by type and value."""
        row = self.conn.execute(
            "SELECT id, entity_type, value, first_seen, last_seen, "
            "occurrence_count, apps, contexts "
            "FROM knowledge_entities WHERE entity_type = ? AND value = ?",
            (entity_type, value)).fetchone()
        return _row_to_entity_info(row) if row else None

    def get_related(self, entity_value: str, limit: int = 20) -> list[Relation]:
        """Get entities related to the given value, ranked by strength."""
        ids = self.conn.execute(
            "SELECT id FROM knowledge_entities WHERE value = ?",
            (entity_value,)).fetchall()
        if not ids:
            return []
        relations: list[Relation] = []
        for (eid,) in ids:
            rows = self.conn.execute(
                "SELECT r.entity_a_id, r.entity_b_id, r.relation_type, "
                "r.strength, r.last_seen FROM knowledge_relations r "
                "WHERE r.entity_a_id = ? OR r.entity_b_id = ? "
                "ORDER BY r.strength DESC LIMIT ?",
                (eid, eid, limit)).fetchall()
            for a_id, b_id, rtype, strength, last_seen in rows:
                other_id = b_id if a_id == eid else a_id
                other = self.conn.execute(
                    "SELECT value FROM knowledge_entities WHERE id = ?",
                    (other_id,)).fetchone()
                if other:
                    relations.append(Relation(entity_value, other[0],
                                              rtype, strength, last_seen))
        # Deduplicate by entity_b and sort by strength
        seen, unique = set(), []
        for r in sorted(relations, key=lambda x: -x.strength):
            if r.entity_b not in seen:
                seen.add(r.entity_b)
                unique.append(r)
        return unique[:limit]

    def get_recent_entities(self, entity_type: str, hours: int = 4,
                            limit: int = 30) -> list[EntityInfo]:
        """Get recently seen entities of a given type."""
        cutoff = time.time() - (hours * 3600)
        rows = self.conn.execute(
            "SELECT id, entity_type, value, first_seen, last_seen, "
            "occurrence_count, apps, contexts FROM knowledge_entities "
            "WHERE entity_type = ? AND last_seen > ? "
            "ORDER BY last_seen DESC LIMIT ?",
            (entity_type, cutoff, limit)).fetchall()
        return [_row_to_entity_info(r) for r in rows]

    def search_entities(self, query: str, limit: int = 20) -> list[EntityInfo]:
        """Fuzzy search across all entity values using LIKE."""
        rows = self.conn.execute(
            "SELECT id, entity_type, value, first_seen, last_seen, "
            "occurrence_count, apps, contexts FROM knowledge_entities "
            "WHERE value LIKE ? ORDER BY occurrence_count DESC LIMIT ?",
            (f"%{query}%", limit)).fetchall()
        return [_row_to_entity_info(r) for r in rows]

    def get_entity_timeline(self, value: str, limit: int = 50) -> list[TimelineEntry]:
        """Get a timeline of when an entity was seen."""
        entity = self.conn.execute(
            "SELECT id, apps, contexts, first_seen, last_seen "
            "FROM knowledge_entities WHERE value = ?",
            (value,)).fetchone()
        if not entity:
            return []
        eid, apps_json, contexts_json, first_seen, last_seen = entity
        apps = _json_list(apps_json)
        contexts = _json_list(contexts_json)
        # Gather relation timestamps for additional timeline points
        rel_rows = self.conn.execute(
            "SELECT last_seen FROM knowledge_relations "
            "WHERE entity_a_id = ? OR entity_b_id = ? "
            "ORDER BY last_seen DESC LIMIT ?",
            (eid, eid, limit)).fetchall()
        timestamps = {first_seen, last_seen}
        for (ts,) in rel_rows:
            timestamps.add(ts)
        default_app = apps[0] if apps else ""
        default_ctx = contexts[0] if contexts else ""
        return [TimelineEntry(ts, default_app, default_ctx)
                for ts in sorted(timestamps, reverse=True)][:limit]

    def get_graph_stats(self) -> dict:
        """Get overall knowledge graph statistics."""
        total_entities = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_entities").fetchone()[0]
        total_relations = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_relations").fetchone()[0]
        type_rows = self.conn.execute(
            "SELECT entity_type, COUNT(*) FROM knowledge_entities "
            "GROUP BY entity_type ORDER BY COUNT(*) DESC").fetchall()
        top_entities = self.conn.execute(
            "SELECT entity_type, value, occurrence_count "
            "FROM knowledge_entities ORDER BY occurrence_count DESC LIMIT 10"
        ).fetchall()
        strongest = self.conn.execute(
            "SELECT ea.value, eb.value, r.strength, r.relation_type "
            "FROM knowledge_relations r "
            "JOIN knowledge_entities ea ON ea.id = r.entity_a_id "
            "JOIN knowledge_entities eb ON eb.id = r.entity_b_id "
            "ORDER BY r.strength DESC LIMIT 10").fetchall()
        return {
            "total_entities": total_entities,
            "total_relations": total_relations,
            "types": {r[0]: r[1] for r in type_rows},
            "top_entities": [{"type": r[0], "value": r[1], "count": r[2]}
                             for r in top_entities],
            "top_relations": [{"a": r[0], "b": r[1], "strength": r[2], "type": r[3]}
                              for r in strongest],
        }

# --- Helpers ---

def _row_to_entity_info(row: tuple) -> EntityInfo:
    """Convert a raw DB row to an EntityInfo dataclass."""
    return EntityInfo(
        id=row[0], entity_type=row[1], value=row[2],
        first_seen=row[3], last_seen=row[4], occurrence_count=row[5],
        apps=_json_list(row[6]), contexts=_json_list(row[7]))

def _json_list(raw: str) -> list[str]:
    """Safely parse a JSON string as a list, returning [] on failure."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
