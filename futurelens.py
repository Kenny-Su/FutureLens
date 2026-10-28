"""Monitoring engine for the FutureLens camera interface."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

from camera import SonyCamera, CameraError, DEFAULT_HELPER
from history import PreviewArchive
from model import (API_ENDPOINT, INSTRUCTIONS, MODEL, MAX_IMAGES, MAX_REQUEST_BYTES, MAX_INPUT_TOKENS,
                   ApiError, CapacityReached, call_model, check_capacity, count_tokens, image_content,
                   load_config, make_payload, parse_action, text_content, validate_config)


@dataclass(frozen=True)
class SessionSettings:
    request: str
    duration: float = 3600
    interval: float = 60
    max_requests: int = 60
    max_shots: int = 1
    max_frame_age: float = 30
    cooldown: float = 5

    def __post_init__(self):
        if not isinstance(self.request, str) or not self.request.strip() or len(self.request) > 8000:
            raise ValueError("Enter an instruction (up to 8,000 characters).")
        for name in ("duration", "interval", "max_frame_age", "cooldown"):
            value = getattr(self, name)
            try:
                valid = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid or (value < 0 if name == "cooldown" else value <= 0):
                qualifier = "non-negative" if name == "cooldown" else "positive"
                raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a finite, {qualifier} number.")
        for name in ("max_shots", "max_requests"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Photo and observation limits must be positive whole numbers.")


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def capture_outcome(entry):
    return {key: entry[key] for key in ("frame_id", "action", "capture_status", "capture_id",
                                      "shutter_sent_at", "saved_files") if key in entry}


class SessionStopped(Exception):
    """A UI stop request, handled between camera/API operations."""


def check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise SessionStopped()


def _run_camera(settings, config, output, report, *, camera_helper, stop_event):
    captures = calls = token_checks = 0
    last_capture = -math.inf
    messages = []
    pending_outcome = None
    with SonyCamera(output / "moments", enable_capture=True, helper=camera_helper) as camera:
        report.update(camera_model=camera.model, source=f"sony:usb:{camera.model}",
                      camera_capabilities=camera.capabilities,
                      monitoring_started_at=datetime.now(timezone.utc).isoformat())
        write_json(output / "results.json", report)
        start = time.monotonic()
        deadline = start + settings.duration
        next_sample = start
        archive = PreviewArchive(output, start)
        report["stop_reason"] = "duration"
        archive.record_message({"role": "system", "content": [{"type": "text", "text": INSTRUCTIONS}]})
        try:
            while time.monotonic() < deadline:
                check_stop(stop_event)
                if calls >= settings.max_requests:
                    report["stop_reason"] = "max_requests"
                    break
                now = time.monotonic()
                if now < next_sample:
                    delay = min(next_sample - now, deadline - now)
                    if stop_event is None:
                        time.sleep(delay)
                    else:
                        stop_event.wait(delay)
                check_stop(stop_event)
                if time.monotonic() >= deadline:
                    break
                report.update(phase="observing", next_observation_at=None)
                write_json(output / "results.json", report)
                preview = archive.append(camera.latest_frame())
                entry = {**preview.metadata(), "preview_file": str(preview.path.relative_to(output))}
                report["observations"].append(entry)
                report["previews_saved"] = archive.count
                write_json(output / "results.json", report)
                content = []
                if not messages:
                    content.append(text_content({"user_request": settings.request}))
                if pending_outcome is not None:
                    content.append(text_content({"capture_outcome": capture_outcome(pending_outcome)}))
                content.extend([text_content({"preview": preview.metadata()}), image_content(preview.image())])
                message = {"role": "user", "content": content}
                messages.append(message)
                archive.record_message(message, preview)
                pending_outcome = None
                check_stop(stop_event)
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
                    report["phase"] = "checking"
                    write_json(output / "results.json", report)
                    check_stop(stop_event)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        entry["status"] = "skipped_duration_expired"
                        break
                    token_checks += 1
                    report["token_count_requests"] = token_checks
                    entry["input_tokens"] = count_tokens(payload, config, timeout=min(90, remaining))
                except CapacityReached as exc:
                    report.update(status="capacity_reached", stop_reason="capacity_reached", capacity=exc.details)
                    entry["status"] = "capacity_reached"
                    break
                check_stop(stop_event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    entry["status"] = "skipped_duration_expired"
                    break
                report["phase"] = "thinking"
                write_json(output / "results.json", report)
                check_stop(stop_event)
                before = time.monotonic()
                remaining = deadline - before
                if remaining <= 0:
                    entry["status"] = "skipped_duration_expired"
                    break
                calls += 1
                report["api_requests"] = calls
                response = call_model(payload, config, timeout=min(90, remaining))
                action = parse_action(response)
                now = time.monotonic()
                entry.update(action=action, inference_latency_s=now - before,
                             frame_age_s=now - preview.received_at, capture_status="not_requested")
                message = {"role": "assistant", "content": response["content"]}
                messages.append(message)
                archive.record_message(message)
                pending_outcome = entry
                if stop_event is not None and stop_event.is_set():
                    if action == "shoot":
                        entry["capture_status"] = "skipped_user_stop"
                    check_stop(stop_event)
                if action == "shoot":
                    if now >= deadline:
                        entry["capture_status"] = "skipped_duration_expired"
                    elif now - preview.received_at >= settings.max_frame_age:
                        entry["capture_status"] = "skipped_stale_frame"
                    elif now - last_capture < settings.cooldown:
                        entry["capture_status"] = "skipped_cooldown"
                    else:
                        entry.update(capture_status="requested", capture_id=captures)
                        report["phase"] = "capturing"
                        write_json(output / "results.json", report)

                        def on_shutter():
                            entry.update(capture_status="shutter_sent",
                                         shutter_sent_at=datetime.now(timezone.utc).isoformat())
                            write_json(output / "results.json", report)

                        remaining = min(deadline, preview.received_at + settings.max_frame_age) - time.monotonic()
                        if stop_event is not None and stop_event.is_set():
                            entry["capture_status"] = "skipped_user_stop"
                            check_stop(stop_event)
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
                            report["photos_taken"] = captures
                            last_capture = time.monotonic()
                            archive.record_capture(entry["capture_id"], preview, files)
                check_stop(stop_event)
                if captures >= settings.max_shots:
                    report["stop_reason"] = "max_shots"
                    break
                # Stay on the session's schedule; never burst through missed slots.
                next_sample += settings.interval
                if next_sample < time.monotonic():
                    next_sample = start + math.ceil((time.monotonic() - start) / settings.interval) * settings.interval
                report.update(phase="waiting", next_observation_at=time.time() + max(0, next_sample - time.monotonic()))
                write_json(output / "results.json", report)
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
        report["status"] = "completed"
    write_json(output / "results.json", report)
    return output


def run_session(settings: SessionSettings, output: Path, *, camera_helper=DEFAULT_HELPER, stop_event=None) -> Path:
    config = load_config()
    validate_config(config)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "moments").mkdir()
    report = {
        "status": "running", "phase": "connecting", "mode": "sony_usb",
        "prompt_version": "claude-conversation-v1", "provider": "anthropic", "model": MODEL,
        "model_settings": {"effort": "low", "thinking": "disabled", "automatic_prompt_caching": True},
        "api_host": urlsplit(API_ENDPOINT).hostname,
        "instructions": INSTRUCTIONS, "request": settings.request,
        "source": "sony:usb", "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {key: getattr(settings, key) for key in
                     ("duration", "interval", "max_requests", "cooldown", "max_frame_age", "max_shots")},
        "capacity_limits": {"images": MAX_IMAGES, "request_bytes": MAX_REQUEST_BYTES, "input_tokens": MAX_INPUT_TOKENS},
        "conversation_file": "conversation.jsonl",
        "observations": [], "previews_saved": 0, "photos_taken": 0,
        "api_requests": 0, "token_count_requests": 0,
    }
    write_json(output / "results.json", report)
    try:
        check_stop(stop_event)
        return _run_camera(settings, config, output, report, camera_helper=camera_helper, stop_event=stop_event)
    except SessionStopped:
        report.update(status="stopped", stop_reason="user_stop")
        return output
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
    finally:
        report.update(phase="finished", next_observation_at=None,
                      ended_at=datetime.now(timezone.utc).isoformat())
        write_json(output / "results.json", report)
