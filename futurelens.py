"""Watch a Sony USB camera through one persistent Claude conversation."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from camera import SonyCamera, CameraError, DEFAULT_HELPER
from history import PreviewArchive
from model import (API_ENDPOINT, INSTRUCTIONS, MODEL, MAX_IMAGES, MAX_REQUEST_BYTES, MAX_INPUT_TOKENS,
                   ApiError, CapacityReached, call_model, check_capacity, count_tokens, image_content,
                   load_config, make_payload, parse_action, text_content, validate_config)


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def capture_outcome(entry):
    return {key: entry[key] for key in ("frame_id", "action", "capture_status", "capture_id",
                                      "shutter_sent_at", "saved_files") if key in entry}


def run_camera(args, config, output, report):
    captures = calls = token_checks = 0
    last_capture = -math.inf
    messages = []
    pending_outcome = None
    with SonyCamera(output / "moments", enable_capture=not args.dry_run,
                    helper=args.camera_helper) as camera:
        report.update(camera_model=camera.model, source=f"sony:usb:{camera.model}",
                      camera_capabilities=camera.capabilities)
        write_json(output / "results.json", report)
        start = time.monotonic()
        deadline = start + args.duration
        next_sample = start
        archive = PreviewArchive(output, start)
        report["stop_reason"] = "duration"
        if not args.dry_run:
            archive.record_message({"role": "system", "content": [{"type": "text", "text": INSTRUCTIONS}]})
        try:
            while time.monotonic() < deadline:
                if not args.dry_run and calls >= args.max_requests:
                    report["stop_reason"] = "max_requests"
                    break
                now = time.monotonic()
                if now < next_sample:
                    time.sleep(min(next_sample - now, deadline - now))
                if time.monotonic() >= deadline:
                    break
                preview = archive.append(camera.latest_frame())
                entry = {**preview.metadata(), "preview_file": str(preview.path.relative_to(output))}
                report["observations"].append(entry)
                write_json(output / "results.json", report)
                if not args.dry_run:
                    content = []
                    if not messages:
                        content.append(text_content({"user_request": args.request}))
                    if pending_outcome is not None:
                        content.append(text_content({"capture_outcome": capture_outcome(pending_outcome)}))
                    content.extend([text_content({"preview": preview.metadata()}), image_content(preview.image())])
                    message = {"role": "user", "content": content}
                    messages.append(message)
                    archive.record_message(message, preview)
                    pending_outcome = None
                    if time.monotonic() >= deadline:
                        entry["status"] = "skipped_duration_expired"
                        break
                    payload = make_payload(messages)
                    try:
                        entry.update(check_capacity(payload))
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            entry["status"] = "skipped_duration_expired"
                            break
                        token_checks += 1
                        entry["input_tokens"] = count_tokens(payload, config, timeout=min(90, remaining))
                    except CapacityReached as exc:
                        report.update(status="capacity_reached", stop_reason="capacity_reached", capacity=exc.details)
                        entry["status"] = "capacity_reached"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        entry["status"] = "skipped_duration_expired"
                        break
                    before = time.monotonic()
                    calls += 1
                    response = call_model(payload, config, timeout=min(90, remaining))
                    action = parse_action(response)
                    now = time.monotonic()
                    entry.update(action=action, inference_latency_s=now - before,
                                 frame_age_s=now - preview.received_at, capture_status="not_requested")
                    message = {"role": "assistant", "content": response["content"]}
                    messages.append(message)
                    archive.record_message(message)
                    pending_outcome = entry
                    if action == "shoot":
                        if now >= deadline:
                            entry["capture_status"] = "skipped_duration_expired"
                        elif now - preview.received_at >= args.max_frame_age:
                            entry["capture_status"] = "skipped_stale_frame"
                        elif now - last_capture < args.cooldown:
                            entry["capture_status"] = "skipped_cooldown"
                        else:
                            entry.update(capture_status="requested", capture_id=captures)
                            write_json(output / "results.json", report)

                            def on_shutter():
                                entry.update(capture_status="shutter_sent",
                                             shutter_sent_at=datetime.now(timezone.utc).isoformat())
                                write_json(output / "results.json", report)

                            remaining = min(deadline, preview.received_at + args.max_frame_age) - time.monotonic()
                            if remaining <= 0:
                                entry["capture_status"] = "skipped_expired_before_shutter"
                            else:
                                try:
                                    files = camera.capture(captures, on_shutter=on_shutter, max_delay=remaining)
                                except (RuntimeError, OSError, KeyboardInterrupt):
                                    entry["capture_status"] = ("failed_after_shutter" if entry["capture_status"] == "shutter_sent"
                                                               else "failed_or_unconfirmed")
                                    raise
                                entry.update(capture_status="downloaded",
                                             saved_files=[str(p.relative_to(output)) for p in files])
                                captures += 1
                                last_capture = time.monotonic()
                                archive.record_capture(entry["capture_id"], preview, files)
                    print(json.dumps(capture_outcome(entry)), flush=True)
                    if captures >= args.max_shots:
                        report["stop_reason"] = "max_shots"
                        break
                write_json(output / "results.json", report)
                # Stay on the session's schedule; never burst through missed slots.
                next_sample += args.interval
                if next_sample < time.monotonic():
                    next_sample = start + math.ceil((time.monotonic() - start) / args.interval) * args.interval
        finally:
            report.update(previews_saved=archive.count, preview_index="previews/index.jsonl",
                          photos_taken=captures, api_requests=calls, token_count_requests=token_checks)
            write_json(output / "results.json", report)
            if pending_outcome is not None:
                # Preserve the final outcome even when there will be no next observation.
                message = {"role": "user", "content": [text_content({"capture_outcome": capture_outcome(pending_outcome)})]}
                messages.append(message)
                archive.record_message(message)
    if not report["previews_saved"]:
        raise RuntimeError("No live-view frames were received within the monitoring duration")
    if report["status"] != "capacity_reached":
        report["status"] = "dry_run" if args.dry_run else "completed"
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
        "prompt_version": "claude-conversation-v1", "provider": "anthropic", "model": MODEL,
        "model_settings": {"effort": "low", "thinking": "disabled", "automatic_prompt_caching": True},
        "api_host": urlsplit(API_ENDPOINT).hostname,
        "instructions": INSTRUCTIONS, "request": args.request,
        "source": "sony:usb", "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {key: getattr(args, key) for key in
                     ("duration", "interval", "max_requests", "cooldown", "max_frame_age", "max_shots")},
        "capacity_limits": {"images": MAX_IMAGES, "request_bytes": MAX_REQUEST_BYTES, "input_tokens": MAX_INPUT_TOKENS},
        "observations": [],
    }
    if not args.dry_run:
        report["conversation_file"] = "conversation.jsonl"
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
        if isinstance(exc, CameraError):
            report.update(camera_model=exc.model, source=f"sony:usb:{exc.model}",
                          camera_capabilities=exc.capabilities, camera_error=str(exc))
        if isinstance(exc, ApiError):
            report["api_error"] = exc.details
        write_json(output / "results.json", report)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--duration", type=float, default=3600, help="Monitoring duration in seconds (default: 3600)")
    parser.add_argument("--interval", type=float, default=60, help="Seconds between observations (default: 60)")
    parser.add_argument("--max-requests", type=int, default=60, help="Maximum inference calls; excludes token-count checks")
    parser.add_argument("--camera-helper", type=Path, default=DEFAULT_HELPER)
    parser.add_argument("--max-frame-age", type=float, default=30, help="Maximum preview age before capture, in seconds (default: 30)")
    parser.add_argument("--cooldown", type=float, default=5, help="Minimum seconds between completed camera captures")
    parser.add_argument("--max-shots", type=int, default=1, help="Stop camera monitoring after this many photos")
    parser.add_argument("--dry-run", action="store_true", help="Save camera previews at the interval without API calls or shutter commands")
    args = parser.parse_args(argv)
    if not args.request.strip():
        parser.error("--request must not be empty")
    for key in ("duration", "interval", "max_frame_age"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} must be a finite, positive number")
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
