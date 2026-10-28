"""Monitoring engine for the FutureLens camera interface."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

from camera import SonyCamera, CameraError, DEFAULT_HELPER
from history import PreviewArchive, AgentJournal, capture_outcome
from model import (API_ENDPOINT, INSTRUCTIONS, MODEL, MAX_IMAGES, MAX_REQUEST_BYTES, MAX_INPUT_TOKENS,
                   ApiError, CapacityReached, call_model, check_capacity, count_tokens, image_content,
                   load_config, make_payload, parse_turn, text_content, validate_config, MAX_CALLS_PER_OBSERVATION)


@dataclass(frozen=True)
class SessionSettings:
    request: str
    duration: float = 3600
    interval: float = 60
    max_requests: int = 360
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
                raise ValueError("Photo and AI call limits must be positive whole numbers.")


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


class SessionStopped(Exception):
    """A UI stop request, handled between camera/API operations."""


def check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise SessionStopped()


class Monitor:
    def __init__(self, settings, config, output, report, stop_event):
        self.settings, self.config, self.output = settings, config, output
        self.report, self.stop_event = report, stop_event
        self.last_capture = -math.inf
        self.latest_capture_outcome = self.last_downloaded_capture = None

    def publish(self):
        write_json(self.output / "results.json", self.report)

    def expired(self, preview):
        if self.stop_event is not None and self.stop_event.is_set():
            return "user_stop"
        if time.monotonic() >= self.deadline:
            return "duration"
        if time.monotonic() - preview.received_at >= self.settings.max_frame_age:
            return "stale_preview"
        return None

    def shoot(self, preview, entry, journal, call_id):
        reason = self.expired(preview)
        statuses = {"user_stop": "skipped_user_stop", "duration": "skipped_duration_expired",
                    "stale_preview": "skipped_stale_frame"}
        if reason:
            entry["capture_status"] = statuses[reason]
        elif time.monotonic() - self.last_capture < self.settings.cooldown:
            entry["capture_status"] = "skipped_cooldown"
        elif self.report["photos_taken"] >= self.settings.max_shots:
            entry["capture_status"] = "skipped_photo_limit"
        else:
            entry.update(capture_status="requested", capture_id=self.report["photos_taken"])
            self.report["phase"] = "capturing"
            journal.event("capture", "started", call_id + ":capture", capture_status="requested")

            def on_shutter():
                entry.update(capture_status="shutter_sent", shutter_sent_at=datetime.now(timezone.utc).isoformat())
                journal.event("capture", "started", call_id + ":capture", capture_status="shutter_sent")

            # Writing the request and camera preparation can consume the remaining allowance.
            reason = self.expired(preview)
            remaining = min(self.deadline, preview.received_at + self.settings.max_frame_age) - time.monotonic()
            if reason == "user_stop":
                entry["capture_status"] = "skipped_user_stop"
            elif reason or remaining <= 0:
                entry["capture_status"] = "skipped_expired_before_shutter"
            else:
                try:
                    files = self.camera.capture(entry["capture_id"], on_shutter=on_shutter, max_delay=remaining)
                except (RuntimeError, OSError, KeyboardInterrupt):
                    entry["capture_status"] = ("failed_after_shutter" if entry["capture_status"] == "shutter_sent"
                                               else "failed_or_unconfirmed")
                    journal.event("capture", "failed", call_id + ":capture", **capture_outcome(entry))
                    raise
                entry.update(capture_status="downloaded", saved_files=[str(p.relative_to(self.output)) for p in files])
                self.report["photos_taken"] += 1
                self.last_capture = time.monotonic()
                self.archive.record_capture(entry["capture_id"], preview, files)
        journal.event("capture", "completed" if entry["capture_status"] == "downloaded" else "skipped",
                      call_id + ":capture", **capture_outcome(entry))
        return capture_outcome(entry)

    def observe(self, preview, entry):
        journal = AgentJournal(self.output, preview, entry, self.publish)
        messages, seen_ids = [], set()
        active = None
        outcome = "error"
        journal.message({"role": "system", "content": [{"type": "text", "text": INSTRUCTIONS}]})
        initial = {"user_request": self.settings.request, "preview": preview.metadata(),
                   "session": {"remaining_seconds": max(0, self.deadline - time.monotonic()),
                               "photos_taken": self.report["photos_taken"], "photo_limit": self.settings.max_shots,
                               "model_calls_remaining": self.settings.max_requests - self.report["api_requests"],
                               "calls_per_observation": MAX_CALLS_PER_OBSERVATION,
                               "preview_freshness_s": self.settings.max_frame_age,
                               "capture_cooldown_s": self.settings.cooldown,
                               "cooldown_remaining_s": max(0, self.settings.cooldown - (time.monotonic() - self.last_capture))},
                   "history": {"count": preview.id, "first_frame_id": 0 if preview.id else None,
                               "latest_frame_id": preview.id - 1 if preview.id else None},
                   "latest_capture_outcome": self.latest_capture_outcome,
                   "last_downloaded_capture": self.last_downloaded_capture}
        message = {"role": "user", "content": [text_content(initial), image_content(preview.image())]}
        messages.append(message)
        journal.message(message, [preview])
        journal.event("run", "started", "run", outcome="saved")
        try:
            for turn in range(1, MAX_CALLS_PER_OBSERVATION + 1):
                outcome = self.expired(preview)
                if outcome:
                    break
                if self.report["api_requests"] >= self.settings.max_requests:
                    outcome = "session_budget"
                    break
                action_id = f"model:{turn}"
                active = ("model", action_id)
                self.report["phase"] = "checking"
                journal.event("model", "checking", action_id, turn=turn)
                payload = make_payload(messages)
                metrics = check_capacity(payload)
                outcome = self.expired(preview)
                if outcome:
                    break
                self.report["token_count_requests"] += 1
                metrics["input_tokens"] = count_tokens(payload, self.config,
                                                       timeout=min(90, self.deadline - time.monotonic()))
                entry.update(metrics)
                outcome = self.expired(preview)
                if outcome:
                    break
                self.report["phase"] = "thinking"
                journal.event("model", "started", action_id, turn=turn, **metrics)
                outcome = self.expired(preview)
                if outcome:
                    break
                self.report["api_requests"] += 1
                entry["model_calls"] += 1
                entry["run_status"] = "evaluating"
                self.publish()
                outcome = self.expired(preview)
                if outcome:
                    self.report["api_requests"] -= 1
                    entry["model_calls"] -= 1
                    break
                before = time.monotonic()
                response = call_model(payload, self.config, timeout=min(90, self.deadline - before))
                call = parse_turn(response, seen_ids)
                message = {"role": "assistant", "content": response["content"]}
                messages.append(message)
                journal.message(message)
                journal.event("model", "completed", action_id, turn=turn, latency_s=time.monotonic() - before)
                active = None
                if call is None:
                    outcome = self.expired(preview) or "no_photo"
                    break
                name, arguments, call_id = call["name"], call["input"], call["id"]
                active = ("tool", "tool:" + call_id)
                journal.event("tool", "started", "tool:" + call_id, name=name, arguments=arguments)
                self.report["phase"] = "retrieving" if name != "shoot" else "capturing"
                self.publish()
                images, is_error, summary = [], False, {}
                if name == "shoot":
                    try:
                        result = self.shoot(preview, entry, journal, call_id)
                    except (RuntimeError, OSError, KeyboardInterrupt):
                        # Even an uncertain capture has a factual terminal tool result.
                        journal.message({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                            "is_error": True, "content": [text_content(capture_outcome(entry))]}]})
                        raise
                    content = [text_content(result)]
                    summary = result
                elif self.expired(preview):
                    outcome = self.expired(preview)
                    is_error = True
                    content = [text_content({"error": f"Observation ended: {outcome}. Tool was not executed."})]
                    summary = {"error": outcome}
                else:
                    try:
                        if name == "list_history":
                            result = self.archive.list_history(preview.id, arguments)
                            content = [text_content(result)]
                            summary = {"count": len(result["previews"]), "next_cursor": result["next_cursor"]}
                        else:
                            images = self.archive.get_history(preview.id, arguments)
                            content = []
                            for image in images:
                                content.extend([text_content(self.archive.describe(image)), image_content(image.image())])
                            summary = {"frame_ids": [image.id for image in images]}
                    except (ValueError, OverflowError) as exc:
                        is_error = True
                        content = [text_content({"error": str(exc)})]
                        summary = {"error": str(exc)}
                message = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                          "content": content, "is_error": is_error}]}
                messages.append(message)
                journal.message(message, images)
                journal.event("tool", "failed" if is_error else "completed", "tool:" + call_id, name=name, **summary)
                active = None
                if name == "shoot":
                    outcome = entry["capture_status"]
                    break
                outcome = self.expired(preview)
                if outcome:
                    break
            else:
                outcome = "session_budget" if self.report["api_requests"] >= self.settings.max_requests else "run_limit"
        except CapacityReached as exc:
            outcome = "capacity_reached"
            self.report.update(status="capacity_reached", stop_reason="capacity_reached", capacity=exc.details)
        except BaseException:
            outcome = "user_stop" if self.stop_event is not None and self.stop_event.is_set() else "error"
            # API and capture failures remain terminal, including after a Stop request.
            entry["run_status"] = "failed"
            if active:
                journal.event(active[0], "failed", active[1])
                active = None
            raise
        finally:
            outcome = outcome or "error"
            if active:
                journal.event(active[0], "skipped", active[1], reason=outcome)
            if entry["run_status"] != "failed":
                entry["run_status"] = "finished" if outcome in ("no_photo", "downloaded") else "skipped"
            entry.update(outcome=outcome, frame_age_s=time.monotonic() - preview.received_at)
            self.archive.record_outcome(entry)
            if entry["capture_status"] != "not_requested":
                self.latest_capture_outcome = capture_outcome(entry)
                if entry["capture_status"] == "downloaded":
                    self.last_downloaded_capture = self.latest_capture_outcome
            journal.event("run", entry["run_status"], "run", outcome=outcome,
                          model_calls=entry["model_calls"], capture_outcome=capture_outcome(entry))

    def run(self, camera_helper):
        with SonyCamera(self.output / "moments", enable_capture=True, helper=camera_helper) as self.camera:
            self.report.update(camera_model=self.camera.model, source=f"sony:usb:{self.camera.model}",
                               camera_capabilities=self.camera.capabilities,
                               monitoring_started_at=datetime.now(timezone.utc).isoformat())
            self.publish()
            start = time.monotonic()
            self.deadline = start + self.settings.duration
            next_sample = start
            self.archive = PreviewArchive(self.output, start)
            self.report.update(stop_reason="duration", preview_index="previews/index.jsonl")
            while time.monotonic() < self.deadline:
                check_stop(self.stop_event)
                if self.report["api_requests"] >= self.settings.max_requests:
                    self.report["stop_reason"] = "max_requests"
                    break
                now = time.monotonic()
                if now < next_sample:
                    delay = min(next_sample - now, self.deadline - now)
                    if self.stop_event is None:
                        time.sleep(delay)
                    else:
                        self.stop_event.wait(delay)
                check_stop(self.stop_event)
                if time.monotonic() >= self.deadline:
                    break
                self.report.update(phase="observing", next_observation_at=None)
                self.publish()
                preview = self.archive.append(self.camera.latest_frame())
                entry = {**preview.metadata(), "preview_file": str(preview.path.relative_to(self.output)),
                         "model_calls": 0, "run_status": "saved", "capture_status": "not_requested"}
                self.report["observations"].append(entry)
                self.report["previews_saved"] = self.archive.count
                self.publish()
                try:
                    self.observe(preview, entry)
                except BaseException:
                    # Journal initialization can fail before the run has its first event.
                    if entry["run_status"] == "saved":
                        entry.update(run_status="failed", outcome="error")
                    raise
                check_stop(self.stop_event)
                if self.report["status"] == "capacity_reached":
                    break
                if self.report["photos_taken"] >= self.settings.max_shots:
                    self.report["stop_reason"] = "max_shots"
                    break
                next_sample += self.settings.interval
                if next_sample < time.monotonic():
                    next_sample = start + math.ceil((time.monotonic() - start) / self.settings.interval) * self.settings.interval
                self.report.update(phase="waiting", next_observation_at=time.time() + max(0, next_sample - time.monotonic()))
                self.publish()
        if not self.report["previews_saved"]:
            raise RuntimeError("No live-view frames were received within the monitoring duration")
        if self.report["status"] != "capacity_reached":
            self.report["status"] = "completed"
        self.publish()
        return self.output


def run_session(settings: SessionSettings, output: Path, *, camera_helper=DEFAULT_HELPER, stop_event=None) -> Path:
    config = load_config()
    validate_config(config)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "moments").mkdir()
    report = {
        "status": "running", "phase": "connecting", "mode": "sony_usb",
        "prompt_version": "claude-agent-v1", "journal_version": 2, "provider": "anthropic", "model": MODEL,
        "model_settings": {"effort": "low", "thinking": "disabled", "automatic_prompt_caching": True},
        "api_host": urlsplit(API_ENDPOINT).hostname,
        "instructions": INSTRUCTIONS, "request": settings.request,
        "source": "sony:usb", "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {key: getattr(settings, key) for key in
                     ("duration", "interval", "max_requests", "cooldown", "max_frame_age", "max_shots")},
        "capacity_limits": {"images": MAX_IMAGES, "request_bytes": MAX_REQUEST_BYTES, "input_tokens": MAX_INPUT_TOKENS},
        "calls_per_observation": MAX_CALLS_PER_OBSERVATION,
        "observations": [], "previews_saved": 0, "photos_taken": 0,
        "api_requests": 0, "token_count_requests": 0,
    }
    write_json(output / "results.json", report)
    try:
        check_stop(stop_event)
        return Monitor(settings, config, output, report, stop_event).run(camera_helper)
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
