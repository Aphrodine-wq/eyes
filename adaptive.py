"""
adaptive.py — Adaptive capture rate engine.

Dynamically adjusts capture interval based on screen activity:
  - High change rate → faster captures (min 3s)
  - Low change rate / idle → slower captures (max 30s)
  - Screen locked / screensaver → pause entirely

Uses an exponential moving average of change frequency
to smooth out the rate adjustments.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AdaptiveState:
    """Tracks activity patterns to determine optimal capture rate."""
    # Config
    min_interval: float = 3.0
    max_interval: float = 30.0
    base_interval: float = 10.0

    # State
    change_history: list[bool] = field(default_factory=list)
    window_size: int = 20           # rolling window of change/no-change
    ema_alpha: float = 0.15         # smoothing factor for EMA
    ema_change_rate: float = 0.5    # current smoothed change rate (0-1)
    current_interval: float = 10.0
    last_capture_time: float = 0.0
    idle_count: int = 0
    burst_count: int = 0

    # Metrics
    total_adjustments: int = 0
    time_saved_seconds: float = 0.0  # estimated time saved vs fixed interval

    def record_change(self, changed: bool) -> float:
        """
        Record whether the screen changed and return the next interval.
        This is the core adaptive logic.
        """
        self.change_history.append(changed)
        if len(self.change_history) > self.window_size:
            self.change_history.pop(0)

        # Update EMA
        signal = 1.0 if changed else 0.0
        self.ema_change_rate = (
            self.ema_alpha * signal +
            (1 - self.ema_alpha) * self.ema_change_rate
        )

        # Track consecutive states for burst/idle detection
        if changed:
            self.idle_count = 0
            self.burst_count += 1
        else:
            self.burst_count = 0
            self.idle_count += 1

        # Calculate new interval
        old_interval = self.current_interval
        self.current_interval = self._compute_interval()
        self.last_capture_time = time.time()

        if abs(old_interval - self.current_interval) > 0.5:
            self.total_adjustments += 1

        # Track efficiency
        if self.current_interval > self.base_interval:
            self.time_saved_seconds += (self.current_interval - self.base_interval)

        return self.current_interval

    def _compute_interval(self) -> float:
        """
        Compute optimal interval from current state.

        Strategy:
        - High EMA (>0.7) = lots of changes = capture fast
        - Mid EMA (0.3-0.7) = moderate = base interval
        - Low EMA (<0.3) = idle = slow down
        - Extended idle (10+ no-change) = max interval
        - Burst (5+ consecutive changes) = min interval
        """
        rate = self.ema_change_rate

        # Burst mode: lots of rapid changes
        if self.burst_count >= 5:
            return self.min_interval

        # Extended idle: nothing happening
        if self.idle_count >= 10:
            return self.max_interval

        # Gradual scaling based on EMA
        if rate > 0.7:
            # Very active
            interval = self.min_interval + (self.base_interval - self.min_interval) * (1 - rate)
        elif rate > 0.3:
            # Moderate
            interval = self.base_interval
        else:
            # Slowing down
            scale = (0.3 - rate) / 0.3  # 0 at rate=0.3, 1 at rate=0
            interval = self.base_interval + (self.max_interval - self.base_interval) * scale

        return round(max(self.min_interval, min(self.max_interval, interval)), 1)

    def get_status(self) -> dict:
        """Get current adaptive state for display."""
        recent = self.change_history[-10:] if self.change_history else []
        recent_rate = sum(recent) / len(recent) if recent else 0

        if self.ema_change_rate > 0.7:
            mode = "active"
        elif self.ema_change_rate > 0.3:
            mode = "moderate"
        elif self.idle_count >= 10:
            mode = "idle"
        else:
            mode = "slowing"

        return {
            "mode": mode,
            "current_interval": self.current_interval,
            "ema_change_rate": round(self.ema_change_rate, 3),
            "recent_change_rate": round(recent_rate, 3),
            "idle_count": self.idle_count,
            "burst_count": self.burst_count,
            "total_adjustments": self.total_adjustments,
            "estimated_time_saved": round(self.time_saved_seconds, 1),
        }


class IdleDetector:
    """
    Detects if the screen is likely locked or the screensaver is on.
    Uses a combination of signals:
      - Same phash for extended period
      - Known screensaver app names
      - Very low text content (locked screen)
    """

    LOCK_APPS = {"loginwindow", "screensaverengine", "lockscreen"}
    MIN_TEXT_FOR_ACTIVE = 20  # characters

    def __init__(self):
        self.same_hash_count = 0
        self.last_phash = ""

    def check(self, app_name: str, text: str, phash: str) -> bool:
        """Returns True if the screen appears to be locked/idle."""
        # Check for known lock screen apps
        if app_name.lower() in self.LOCK_APPS:
            return True

        # Check for very low text content (locked screen)
        if len(text.strip()) < self.MIN_TEXT_FOR_ACTIVE:
            return True

        # Check for extended same-hash (screen frozen / screensaver)
        if phash and phash == self.last_phash:
            self.same_hash_count += 1
        else:
            self.same_hash_count = 0
        self.last_phash = phash

        # 30+ consecutive same frames = likely idle/locked
        return self.same_hash_count >= 30

    def reset(self):
        self.same_hash_count = 0
        self.last_phash = ""
