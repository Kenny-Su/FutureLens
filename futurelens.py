"""Control a Sony A7 IV over USB and photograph a requested condition."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlsplit

from camera import SonyCamera, DEFAULT_HELPER
from model import API_ENDPOINT, INSTRUCTIONS, MODEL, ApiError, decide, load_config, validate_config


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def run_camera(args, config, output, report):
    latencies = []
    armed = True
    last_capture = -math.inf
    captures = 0
    with SonyCamera(output / "moments", enable_capture=not args.dry_run,
                    helper=args.camera_helper) as camera:
        report["camera_model"] = camera.model
        write_json(output / "results.json", report)
        start = time.monotonic()
        deadline = start + args.duration
        next_sample = start
        for index in range(args.max_requests):
            now = time.monotonic()
            if now >= deadline:
                break
            if now < next_sample:
                time.sleep(min(next_sample - now, deadline - now))
            if time.monotonic() >= deadline:
                break
            frame = camera.latest_frame()
            if time.monotonic() >= deadline:
                break
            next_sample = time.monotonic() + 1 / args.fps
            entry = {"timestamp_s": frame.received_at - start, "frame_no": frame.frame_no}
            report["decisions"].append(entry)
            if args.dry_run:
                filename = f"sample_{index:05d}.jpg"
                frame.image.save(output / filename, quality=95, subsampling=0)
                entry["sample_file"] = filename
            else:
                before = time.monotonic()
                shot = decide(frame.image, args.request, config)
                now = time.monotonic()
                elapsed = now - before
                latencies.append(elapsed)
                entry.update(shot=shot, decision_latency_s=elapsed,
                             frame_age_s=now - frame.received_at)
                if not shot:
                    armed = True
                elif now >= deadline:
                    entry["capture_status"] = "skipped_duration_expired"
                elif now - frame.received_at > args.max_frame_age:
                    entry["capture_status"] = "skipped_stale_frame"
                elif not armed:
                    entry["capture_status"] = "skipped_already_captured_condition"
                elif now - last_capture < args.cooldown:
                    entry["capture_status"] = "skipped_cooldown"
                else:
                    entry["capture_status"] = "requested"
                    write_json(output / "results.json", report)

                    def on_shutter():
                        entry.update(capture_status="shutter_sent",
                                     shutter_sent_at=datetime.now(timezone.utc).isoformat())
                        write_json(output / "results.json", report)

                    try:
                        remaining = min(args.max_frame_age - (time.monotonic() - frame.received_at),
                                        deadline - time.monotonic())
                        if remaining <= 0:
                            entry["capture_status"] = "skipped_stale_frame"
                            write_json(output / "results.json", report)
                            continue
                        files = camera.capture(index, on_shutter=on_shutter, max_delay=remaining)
                    except (RuntimeError, OSError):
                        entry["capture_status"] = "failed_after_shutter" if entry["capture_status"] == "shutter_sent" else "failed_or_unconfirmed"
                        raise
                    entry.update(capture_status="downloaded", saved_files=[str(p.relative_to(output)) for p in files])
                    captures += 1
                    last_capture = time.monotonic()
                    armed = False
                print(json.dumps({"shot": shot, "capture_status": entry.get("capture_status")}), flush=True)
            write_json(output / "results.json", report)
            if captures >= args.max_shots:
                break
    if not report["decisions"]:
        raise RuntimeError("No live-view frames were received within the monitoring duration")
    report.update(status="dry_run" if args.dry_run else "completed", photos_taken=captures)
    if latencies:
        report["decision_latency_p50_s"] = statistics.median(latencies)
    write_json(output / "results.json", report)
    return output


def run(args) -> Path:
    config = None if args.dry_run else load_config()
    if not args.dry_run:
        validate_config(config)
    output = (args.output or Path("outputs") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "moments").mkdir()
    report = {
        "status": "running", "mode": "sony_usb",
        "prompt_version": "claude-shot-v1", "provider": "anthropic", "model": MODEL,
        "model_settings": {"effort": "low", "thinking": "disabled"},
        "api_host": urlsplit(API_ENDPOINT).hostname,
        "instructions": INSTRUCTIONS, "request": args.request,
        "source": "sony:usb:ILCE-7M4",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {key: getattr(args, key) for key in
                     ("duration", "fps", "max_requests", "cooldown", "max_frame_age", "max_shots")},
        "decisions": [],
    }
    write_json(output / "results.json", report)
    print(f"Output: {output}", file=sys.stderr)
    try:
        return run_camera(args, config, output, report)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        write_json(output / "results.json", report)
        raise
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, ApiError):
            report["api_error"] = exc.details
        write_json(output / "results.json", report)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--fps", type=float, default=1, help="Maximum live-preview checks per second (default: 1)")
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--camera-helper", type=Path, default=DEFAULT_HELPER)
    parser.add_argument("--max-frame-age", type=float, default=3, help="Discard camera decisions older than this many seconds")
    parser.add_argument("--cooldown", type=float, default=5, help="Minimum seconds between completed camera captures")
    parser.add_argument("--max-shots", type=int, default=1, help="Stop camera monitoring after this many photos")
    parser.add_argument("--dry-run", action="store_true", help="Save live camera previews without API calls or shutter commands")
    args = parser.parse_args(argv)
    if not args.request.strip():
        parser.error("--request must not be empty")
    for key in ("duration", "fps", "max_frame_age"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(f"--{key} must be a finite, positive number")
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    if not math.isfinite(args.cooldown) or args.cooldown < 0:
        parser.error("--cooldown must be finite and non-negative")
    if args.max_shots < 1:
        parser.error("--max-shots must be positive")
    return args


def main(argv=None) -> int:
    try:
        run(parse_args(argv))
    except KeyboardInterrupt:
        print("FutureLens: stopped", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"FutureLens: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
