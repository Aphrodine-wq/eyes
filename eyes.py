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

from store import EyesStore
from capture import (
    capture_frame, capture_frame_with_vision,
    capture_screenshot, compute_phash, ocr_image, get_active_window_info,
    AsyncCapture
)


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
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if use_vision and not api_key:
        print("⚠️  --with-vision requires ANTHROPIC_API_KEY env var")
        sys.exit(1)

    prev_phash = ""
    captured = 0
    skipped = 0

    # Use async capture so OCR doesn't block the loop on Intel
    async_cap = AsyncCapture(max_workers=1) if not use_vision else None

    def handle_signal(sig, frame):
        print(f"\n\n👁️  Stopping. Captured {captured} frames, skipped {skipped} duplicates.")
        if async_cap:
            async_cap.shutdown()
        store.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    mode_str = "vision API" if use_vision else ("accurate OCR" if not fast_ocr else "fast OCR")
    print(f"👁️  Claude Eyes watching (every {interval}s, {mode_str}, scale={scale})")
    print(f"   DB: {store.db_path}")
    print(f"   Press Ctrl+C to stop\n")

    while True:
        try:
            ts = datetime.now().strftime("%H:%M:%S")

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
                    preview = result.text[:80].replace("\n", " ")
                    print(f"  [{ts}] ✅ {result.app_name}: {preview}")

                # Submit new capture job
                async_cap.submit(
                    prev_phash=prev_phash,
                    scale=scale,
                    fast_ocr=fast_ocr,
                )

        except Exception as e:
            print(f"  [{ts}] ❌ {e}")

        time.sleep(interval)


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
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
