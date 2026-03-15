"""
triggers.py — Screen content trigger system.

Define rules that fire when specific content appears on screen.
Triggers can run shell commands, write to a log, or set flags
for the MCP server to report.

Config format in ~/.claude-eyes/config.json:
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
            "pattern": "zoom.*meeting|teams.*meeting|google meet",
            "match_on": "window_title",
            "action": "command",
            "command": "osascript -e 'display notification \"Meeting detected\" with title \"Eyes\"'",
            "cooldown_seconds": 300
        }
    ]
}
"""

import re
import time
import subprocess
import json
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


TRIGGERS_LOG = Path.home() / ".claude-eyes" / "triggers.log"


@dataclass
class TriggerRule:
    """A single trigger rule definition."""
    name: str
    pattern: str              # regex pattern
    match_on: str = "text"    # "text", "window_title", "app_name", "any"
    action: str = "log"       # "log", "command", "flag"
    command: str = ""         # shell command to run (if action=command)
    cooldown_seconds: int = 60
    enabled: bool = True


@dataclass
class TriggerEvent:
    """A triggered event record."""
    rule_name: str
    timestamp: float
    matched_text: str
    app_name: str
    action_taken: str


class TriggerEngine:
    """
    Evaluates screen captures against trigger rules.
    Manages cooldowns and fires actions.
    """

    def __init__(self, rules: list[TriggerRule] = None):
        self.rules = rules or []
        self.last_fired: dict[str, float] = {}  # rule_name -> last fire time
        self.events: list[TriggerEvent] = []
        self.flags: dict[str, str] = {}  # active flags for MCP to read

    @classmethod
    def from_config(cls, config: dict) -> "TriggerEngine":
        """Build trigger engine from config dict."""
        raw_triggers = config.get("triggers", [])
        rules = []
        for t in raw_triggers:
            if "name" not in t or "pattern" not in t:
                continue
            rules.append(TriggerRule(
                name=t["name"],
                pattern=t["pattern"],
                match_on=t.get("match_on", "text"),
                action=t.get("action", "log"),
                command=t.get("command", ""),
                cooldown_seconds=t.get("cooldown_seconds", 60),
                enabled=t.get("enabled", True),
            ))
        return cls(rules=rules)

    def evaluate(self, app_name: str, window_title: str, text: str) -> list[TriggerEvent]:
        """
        Evaluate all rules against a capture. Returns list of fired triggers.
        Respects cooldowns.
        """
        now = time.time()
        fired = []

        for rule in self.rules:
            if not rule.enabled:
                continue

            # Check cooldown
            last = self.last_fired.get(rule.name, 0)
            if now - last < rule.cooldown_seconds:
                continue

            # Determine what to match against
            if rule.match_on == "text":
                target = text
            elif rule.match_on == "window_title":
                target = window_title
            elif rule.match_on == "app_name":
                target = app_name
            elif rule.match_on == "any":
                target = f"{app_name} {window_title} {text}"
            else:
                target = text

            # Check pattern
            match = re.search(rule.pattern, target, re.IGNORECASE)
            if not match:
                continue

            # Fire the trigger
            matched_text = match.group(0)[:200]
            event = TriggerEvent(
                rule_name=rule.name,
                timestamp=now,
                matched_text=matched_text,
                app_name=app_name,
                action_taken=rule.action,
            )

            self._execute_action(rule, event)
            self.last_fired[rule.name] = now
            self.events.append(event)
            fired.append(event)

            # Keep event history bounded
            if len(self.events) > 500:
                self.events = self.events[-250:]

        return fired

    def _execute_action(self, rule: TriggerRule, event: TriggerEvent):
        """Execute the trigger action."""
        if rule.action == "log":
            self._log_event(event)

        elif rule.action == "command":
            self._log_event(event)
            try:
                subprocess.Popen(
                    rule.command,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        elif rule.action == "flag":
            self.flags[rule.name] = event.matched_text
            self._log_event(event)

    def _log_event(self, event: TriggerEvent):
        """Append trigger event to the log file."""
        try:
            TRIGGERS_LOG.parent.mkdir(parents=True, exist_ok=True)
            from datetime import datetime
            ts = datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            line = (
                f"[{ts}] TRIGGER: {event.rule_name} | "
                f"App: {event.app_name} | "
                f"Match: {event.matched_text} | "
                f"Action: {event.action_taken}\n"
            )
            with open(TRIGGERS_LOG, "a") as f:
                f.write(line)
        except Exception:
            pass

    def get_recent_events(self, limit: int = 20) -> list[TriggerEvent]:
        """Get recent trigger events."""
        return self.events[-limit:]

    def get_active_flags(self) -> dict[str, str]:
        """Get currently active flags (set by flag triggers)."""
        return dict(self.flags)

    def clear_flag(self, name: str):
        """Clear a specific flag."""
        self.flags.pop(name, None)

    def clear_all_flags(self):
        """Clear all flags."""
        self.flags.clear()
