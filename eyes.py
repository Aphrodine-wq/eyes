#!/usr/bin/env python3
"""
eyes.py — Main CLI for Claude Eyes (Intel Mac optimized).

Usage:
    python eyes.py watch [--interval N] [--with-vision] [--scale F] [--accurate]
    python eyes.py now
    python eyes.py benchmark          # test OCR speed on your machine
    python eyes.py history [MINUTES]
    python eyes.py search QUERY
    python eyes.py app APP_NAME [MINUTES]
    python eyes.py stats
    python eyes.py prune [DAYS]
"""

import sys
import time
import signal
import argparse
import os
from datetime import datetime

from store import EyesStore, load_config, is_app_ignored, CONFIG_PATH
from capture import (
    capture_frame, capture_frame_with_vision,
    capture_screenshot, compute_phash, ocr_image, get_active_window_info,
    AsyncCapture
)
from adaptive import AdaptiveState, IdleDetector
from triggers import TriggerEngine
from classifier import classify_capture
from flow import FlowDetector
from context_chain import ContextTracker
from knowledge import KnowledgeGraph, EntityExtractor


def format_entry(entry, verbose=False):
    ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
    header = f"[{ts}] {entry.app_name}"
    if entry.window_title:
        header += f" — {entry.window_title}"

    if verbose:
        lines = [header, "-" * 60]
        if entry.text:
            text = entry.text[:500] + "..." if len(entry.text) > 500 else entry.text
            lines.append(text)
        if entry.extra_context:
            lines.append(f"\n🧠 Context: {entry.extra_context}")
        lines.append("")
        return "\n".join(lines)
    else:
        preview = entry.text[:120].replace("\n", " ")
        return f"{header}: {preview}"


def cmd_watch(args):
    """Run the screen watcher loop with threaded OCR for Intel."""
    store = EyesStore()
    interval = args.interval
    use_vision = args.with_vision
    scale = args.scale
    fast_ocr = not args.accurate
    use_adaptive = args.adaptive
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if use_vision and not api_key:
        print("⚠️  --with-vision requires ANTHROPIC_API_KEY env var")
        sys.exit(1)

    prev_phash = ""
    captured = 0
    skipped = 0
    ignored = 0
    idle_paused = 0
    triggered = 0
    config = load_config()

    # Adaptive rate engine
    adaptive = AdaptiveState(
        min_interval=3.0,
        max_interval=30.0,
        base_interval=float(interval),
    ) if use_adaptive else None

    # Idle detector
    idle_detector = IdleDetector()

    # Trigger engine
    trigger_engine = TriggerEngine.from_config(config)
    trigger_count = len(trigger_engine.rules)

    # Flow detector
    flow_detector = FlowDetector(window_minutes=15)

    # Context tracker
    context_tracker = ContextTracker(window_size=100)

    # Knowledge graph
    knowledge_graph = KnowledgeGraph(store.conn)
    entity_extractor = EntityExtractor()

    # Use async capture so OCR doesn't block the loop on Intel
    async_cap = AsyncCapture(max_workers=1) if not use_vision else None

    def handle_signal(sig, frame):
        stats = f"Captured {captured}, skipped {skipped} dupes, ignored {ignored} (blocked apps)"
        if idle_paused:
            stats += f", idle-paused {idle_paused}"
        if triggered:
            stats += f", {triggered} triggers fired"
        if adaptive:
            astatus = adaptive.get_status()
            stats += f", {astatus['total_adjustments']} rate adjustments"
            stats += f", ~{astatus['estimated_time_saved']:.0f}s saved"
        print(f"\n\n👁️  Stopping. {stats}.")
        if async_cap:
            async_cap.shutdown()
        store.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    mode_str = "vision API" if use_vision else ("accurate OCR" if not fast_ocr else "fast OCR")
    adaptive_str = " + adaptive rate" if use_adaptive else ""
    trigger_str = f" + {trigger_count} triggers" if trigger_count else ""
    print(f"👁️  Eyes watching (every {interval}s, {mode_str}{adaptive_str}{trigger_str}, scale={scale})")
    print(f"   DB: {store.db_path}")
    print(f"   Press Ctrl+C to stop\n")

    while True:
        try:
            ts = datetime.now().strftime("%H:%M:%S")

            # Check if active app is on the ignore list
            active_app, active_window = get_active_window_info()
            if is_app_ignored(active_app, config):
                ignored += 1
                if adaptive:
                    adaptive.record_change(False)
                time.sleep(adaptive.current_interval if adaptive else interval)
                continue

            if use_vision:
                # Vision API mode: synchronous (API call is the bottleneck anyway)
                frame = capture_frame_with_vision(api_key, prev_phash, scale=scale)
                if frame is None:
                    skipped += 1
                    print(f"  [{ts}] ⏭️  skip (no change)")
                else:
                    store.insert(
                        timestamp=frame.timestamp,
                        app_name=frame.app_name,
                        window_title=frame.window_title,
                        text=frame.text,
                        extra_context=frame.extra_context,
                        phash=frame.phash,
                    )
                    prev_phash = frame.phash
                    captured += 1
                    preview = frame.text[:80].replace("\n", " ")
                    print(f"  [{ts}] ✅ {frame.app_name}: {preview}")

            else:
                # Local OCR mode: threaded so we don't block
                # Check for results from previous tick
                result = async_cap.get_result()
                if result is not None:
                    # Idle detection
                    if idle_detector.check(result.app_name, result.text, result.phash):
                        idle_paused += 1
                        if adaptive:
                            adaptive.record_change(False)
                        print(f"  [{ts}] 💤 idle (screen locked or inactive)")
                    else:
                        store.insert(
                            timestamp=result.timestamp,
                            app_name=result.app_name,
                            window_title=result.window_title,
                            text=result.text,
                            extra_context=result.extra_context,
                            phash=result.phash,
                        )
                        prev_phash = result.phash
                        captured += 1

                        # Classify the capture
                        cls = classify_capture(result.app_name, result.window_title, result.text)

                        # Feed flow detector
                        flow_detector.record(result.timestamp, result.app_name, cls.category)

                        # Feed context tracker
                        context_tracker.record(
                            result.timestamp, result.app_name,
                            result.window_title, result.text, cls.category
                        )

                        # Extract entities for knowledge graph
                        try:
                            entities = entity_extractor.extract(
                                result.text, result.app_name, result.window_title
                            )
                            if entities:
                                knowledge_graph.record_entities(
                                    entities, result.timestamp,
                                    result.app_name, result.window_title
                                )
                        except Exception:
                            pass

                        # Run triggers
                        events = trigger_engine.evaluate(
                            result.app_name, result.window_title, result.text
                        )
                        if events:
                            triggered += len(events)
                            for ev in events:
                                print(f"  [{ts}] ⚡ TRIGGER: {ev.rule_name} → {ev.matched_text[:60]}")

                        # Adaptive rate
                        if adaptive:
                            adaptive.record_change(True)

                        preview = result.text[:60].replace("\n", " ")
                        cat_str = f" [{cls.category}]" if cls.category != "unknown" else ""
                        rate_str = f" ({adaptive.current_interval:.0f}s)" if adaptive else ""
                        print(f"  [{ts}] ✅ {result.app_name}{cat_str}: {preview}{rate_str}")

                elif adaptive:
                    # No result (duplicate) — record as no-change
                    adaptive.record_change(False)
                    skipped += 1

                # Submit new capture job
                async_cap.submit(
                    prev_phash=prev_phash,
                    scale=scale,
                    fast_ocr=fast_ocr,
                )

        except Exception as e:
            print(f"  [{ts}] ❌ {e}")

        current_interval = adaptive.current_interval if adaptive else interval
        time.sleep(current_interval)


def cmd_now(args):
    """Capture and display what's on screen right now."""
    print("📸 Capturing...")
    frame = capture_frame(scale=0.5, fast_ocr=True)
    if frame:
        entry_like = type("E", (), {
            "timestamp": frame.timestamp,
            "app_name": frame.app_name,
            "window_title": frame.window_title,
            "text": frame.text,
            "extra_context": frame.extra_context,
        })()
        print(format_entry(entry_like, verbose=True))
    else:
        print("Nothing captured (screen may be locked or permission denied)")


def cmd_benchmark(args):
    """
    Benchmark OCR speed on this machine.
    Helps you pick the right interval for your Intel Mac.
    """
    print("⏱️  Benchmarking OCR speed on your machine...\n")

    path = None
    try:
        # Capture a test screenshot
        path = capture_screenshot(scale=1.0)
        file_size = os.path.getsize(path) / 1024

        print(f"   Screenshot: {file_size:.0f} KB (full resolution)\n")

        # Test different configurations
        configs = [
            ("Fast OCR, 50% scale", 0.5, True),
            ("Fast OCR, 75% scale", 0.75, True),
            ("Fast OCR, full scale", 1.0, True),
            ("Accurate OCR, 50% scale", 0.5, False),
            ("Accurate OCR, full scale", 1.0, False),
        ]

        results = []
        for label, scale, fast in configs:
            # Rescale screenshot
            import shutil
            test_path = path + ".test.jpg"
            shutil.copy(path, test_path)

            if scale < 1.0:
                try:
                    from capture import _downscale_sips
                    _downscale_sips(test_path, scale)
                except Exception:
                    pass

            start = time.time()
            text = ocr_image(test_path, fast=fast)
            elapsed = time.time() - start
            word_count = len(text.split()) if text else 0

            results.append((label, elapsed, word_count))
            print(f"   {label:35s} → {elapsed:.2f}s ({word_count} words)")

            if os.path.exists(test_path):
                os.unlink(test_path)

        # Recommendation
        fastest = min(results, key=lambda x: x[1])
        print(f"\n   🏆 Fastest: {fastest[0]} at {fastest[1]:.2f}s")
        recommended_interval = max(5, int(fastest[1] * 3))
        print(f"   📋 Recommended interval: {recommended_interval}s")
        print(f"      (run: python eyes.py watch --interval {recommended_interval})")

    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def cmd_history(args):
    """Show recent screen history."""
    store = EyesStore()
    entries = store.get_recent(minutes=args.minutes)
    if not entries:
        print(f"No captures in the last {args.minutes} minutes.")
        return
    print(f"📜 Last {args.minutes} minutes ({len(entries)} captures):\n")
    for entry in reversed(entries):
        print(format_entry(entry, verbose=args.verbose))
    store.close()


def cmd_search(args):
    """Full-text search across screen history."""
    store = EyesStore()
    entries = store.search(args.query)
    if not entries:
        print(f"No results for '{args.query}'")
        return
    print(f"🔍 {len(entries)} results for '{args.query}':\n")
    for entry in entries:
        print(format_entry(entry, verbose=True))
    store.close()


def cmd_app(args):
    """Show history for a specific app."""
    store = EyesStore()
    entries = store.search_by_app(args.app_name, minutes=args.minutes)
    if not entries:
        print(f"No captures for '{args.app_name}' in the last {args.minutes} minutes.")
        return
    print(f"📱 {len(entries)} captures from {args.app_name}:\n")
    for entry in reversed(entries):
        print(format_entry(entry, verbose=True))
    store.close()


def cmd_stats(args):
    """Show storage stats."""
    store = EyesStore()
    s = store.stats()
    print("📊 Claude Eyes Stats")
    print(f"   Total captures: {s['total_frames']}")
    if s['oldest_timestamp']:
        oldest = datetime.fromtimestamp(s['oldest_timestamp']).strftime("%Y-%m-%d %H:%M")
        newest = datetime.fromtimestamp(s['newest_timestamp']).strftime("%Y-%m-%d %H:%M")
        print(f"   Date range: {oldest} → {newest}")
    print(f"   Database size: {s['db_size_mb']} MB")
    store.close()


def cmd_prune(args):
    """Delete old entries."""
    store = EyesStore()
    count = store.prune(days=args.days)
    print(f"🗑️  Pruned {count} entries older than {args.days} days.")
    store.close()


def cmd_summary(args):
    """Show an activity summary."""
    store = EyesStore()
    print(store.get_activity_summary(minutes=args.minutes))
    store.close()


def cmd_focus(args):
    """Show focus time breakdown."""
    store = EyesStore()
    focus = store.get_focus_stats(minutes=args.minutes)

    print(f"🎯 Focus stats (last {args.minutes} min)\n")
    print(f"   Total captures: {focus['total_frames']}")
    print(f"   Context switches: {focus['switches']}\n")

    for app, info in focus["apps"].items():
        bar_len = int(info["percent"] / 3)
        bar = "█" * bar_len
        print(f"   {app:25s} {bar} {info['estimated_minutes']}min ({info['percent']}%)")
    store.close()


def cmd_sessions(args):
    """Detect and show work sessions."""
    store = EyesStore()
    sessions = store.get_sessions(hours=args.hours)

    if not sessions:
        print(f"No sessions detected in the last {args.hours} hours.")
        return

    print(f"📋 Work sessions (last {args.hours} hours)\n")
    for i, s in enumerate(sessions, 1):
        start_str = datetime.fromtimestamp(s.start).strftime("%H:%M")
        end_str = datetime.fromtimestamp(s.end).strftime("%H:%M")
        print(f"   Session {i}: {start_str} - {end_str} ({s.duration_minutes}min)")
        print(f"   {s.summary}")
        print(f"   Apps: {', '.join(s.apps)}")
        print()
    store.close()


def cmd_digest(args):
    """Generate a daily or weekly digest."""
    from digest import generate_daily_digest, generate_weekly_digest
    store = EyesStore()
    if args.weekly:
        print(generate_weekly_digest(store))
    else:
        if args.date:
            date = datetime.strptime(args.date, "%Y-%m-%d")
        else:
            date = datetime.now()
        print(generate_daily_digest(store, date))
    store.close()


def cmd_classify(args):
    """Classify recent screen activity."""
    from classifier import classify_batch
    store = EyesStore()
    entries = store.get_recent(minutes=args.minutes)
    if not entries:
        print(f"No captures in the last {args.minutes} minutes.")
        return

    categories = classify_batch(entries)
    config = load_config()
    interval = config.get("capture_interval", 10)

    print(f"🏷️  Content classification (last {args.minutes} min, {len(entries)} captures)\n")

    total = sum(c["count"] for c in categories.values())
    productive = sum(c["productive_frames"] for c in categories.values())

    for cat, info in sorted(categories.items(), key=lambda x: -x[1]["count"]):
        pct = round((info["count"] / total) * 100, 1)
        est_min = round((info["count"] * interval) / 60, 1)
        prod_mark = " *" if info["productive_frames"] > info["count"] * 0.5 else ""
        print(f"  {cat:12s} {pct:5.1f}% (~{est_min}min, {info['count']} frames){prod_mark}")

        subs = info.get("subcategories", {})
        for sub, count in sorted(subs.items(), key=lambda x: -x[1])[:3]:
            if sub:
                print(f"    └ {sub}: {count}")

        kws = info.get("top_keywords", [])
        if kws:
            print(f"    keywords: {', '.join(kws[:6])}")
        print()

    prod_pct = round((productive / total) * 100, 1) if total else 0
    print(f"  Productivity: {prod_pct}% (* = productive category)")
    store.close()


def cmd_triggers(args):
    """Show recent trigger events."""
    from pathlib import Path
    log_path = Path.home() / ".claude-eyes" / "triggers.log"
    if not log_path.exists():
        print("No trigger events yet.")
        return
    lines = log_path.read_text().strip().split("\n")
    recent = lines[-20:] if len(lines) > 20 else lines
    print(f"⚡ Recent triggers ({len(recent)} events):\n")
    for line in recent:
        print(f"  {line}")


def cmd_config(args):
    """Show or edit config."""
    config = load_config()
    if args.show:
        import json
        print(f"📝 Config ({CONFIG_PATH}):\n")
        print(json.dumps(config, indent=2))
    elif args.ignore_add:
        apps = config.get("ignore_apps", [])
        if args.ignore_add not in apps:
            apps.append(args.ignore_add)
            config["ignore_apps"] = apps
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
            print(f"   Added '{args.ignore_add}' to ignore list.")
        else:
            print(f"   '{args.ignore_add}' already in ignore list.")
    elif args.ignore_remove:
        apps = config.get("ignore_apps", [])
        if args.ignore_remove in apps:
            apps.remove(args.ignore_remove)
            config["ignore_apps"] = apps
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
            print(f"   Removed '{args.ignore_remove}' from ignore list.")
        else:
            print(f"   '{args.ignore_remove}' not in ignore list.")


def main():
    parser = argparse.ArgumentParser(description="Claude Eyes — screen awareness for Claude (Intel Mac optimized)")
    sub = parser.add_subparsers(dest="command")

    # watch
    p_watch = sub.add_parser("watch", help="Start screen watcher")
    p_watch.add_argument("--interval", type=int, default=10,
                         help="Seconds between captures (default: 10, safe for Intel)")
    p_watch.add_argument("--with-vision", action="store_true",
                         help="Also use Claude Vision API for richer context")
    p_watch.add_argument("--scale", type=float, default=0.5,
                         help="Screenshot scale factor (default: 0.5 = half res)")
    p_watch.add_argument("--accurate", action="store_true",
                         help="Use accurate OCR (slower but more precise)")
    p_watch.add_argument("--adaptive", action="store_true",
                         help="Adaptive capture rate (fast when active, slow when idle)")

    # now
    sub.add_parser("now", help="Capture current screen")

    # benchmark
    sub.add_parser("benchmark", help="Test OCR speed on your machine")

    # history
    p_hist = sub.add_parser("history", help="Show recent captures")
    p_hist.add_argument("minutes", type=int, nargs="?", default=30)
    p_hist.add_argument("-v", "--verbose", action="store_true")

    # search
    p_search = sub.add_parser("search", help="Search screen history")
    p_search.add_argument("query", type=str)

    # app
    p_app = sub.add_parser("app", help="Filter by app name")
    p_app.add_argument("app_name", type=str)
    p_app.add_argument("minutes", type=int, nargs="?", default=60)

    # stats
    sub.add_parser("stats", help="Show storage stats")

    # prune
    p_prune = sub.add_parser("prune", help="Delete old entries")
    p_prune.add_argument("days", type=int, nargs="?", default=7)

    # summary
    p_summary = sub.add_parser("summary", help="Activity summary")
    p_summary.add_argument("minutes", type=int, nargs="?", default=60)

    # focus
    p_focus = sub.add_parser("focus", help="Focus time breakdown")
    p_focus.add_argument("minutes", type=int, nargs="?", default=60)

    # sessions
    p_sessions = sub.add_parser("sessions", help="Detect work sessions")
    p_sessions.add_argument("hours", type=int, nargs="?", default=8)

    # digest
    p_digest = sub.add_parser("digest", help="Generate daily/weekly digest")
    p_digest.add_argument("--weekly", action="store_true", help="Generate weekly digest")
    p_digest.add_argument("--date", type=str, help="Specific date (YYYY-MM-DD)")

    # classify
    p_classify = sub.add_parser("classify", help="Classify recent activity")
    p_classify.add_argument("minutes", type=int, nargs="?", default=60)

    # triggers
    sub.add_parser("triggers", help="Show recent trigger events")

    # config
    p_config = sub.add_parser("config", help="Show or edit config")
    p_config.add_argument("--show", action="store_true", default=True, help="Show current config")
    p_config.add_argument("--ignore-add", type=str, help="Add app to ignore list")
    p_config.add_argument("--ignore-remove", type=str, help="Remove app from ignore list")

    args = parser.parse_args()

    commands = {
        "watch": cmd_watch,
        "now": cmd_now,
        "benchmark": cmd_benchmark,
        "history": cmd_history,
        "search": cmd_search,
        "app": cmd_app,
        "stats": cmd_stats,
        "prune": cmd_prune,
        "summary": cmd_summary,
        "focus": cmd_focus,
        "sessions": cmd_sessions,
        "config": cmd_config,
        "digest": cmd_digest,
        "classify": cmd_classify,
        "triggers": cmd_triggers,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
