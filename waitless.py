"""Monitoring engine for the WaitLess camera interface."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from PIL import Image
import shutil
import time
from urllib.parse import urlsplit

from camera import MacCamera, CameraError
from demo import VideoCamera, VideoEnded
from history import PreviewArchive, ObservationJournal, capture_outcome
from intent import ApprovedPlan
from openai_client import (ApiError, load_config, response_usage, text_content, validate_config, model_image)
from planner import PLANNING_MODEL
from monitor import (DECISIONS_ENDPOINT, MONITOR_MODEL, MONITOR_QUESTION_INSTRUCTIONS,
                     make_monitoring_payload, evaluate_preview, decision_probability)


# Provisional product threshold; calibrate with representative previews later.
CAPTURE_PROBABILITY_THRESHOLD = 0.90


@dataclass(frozen=True)
class SessionSettings:
    request: str
    interval_s: int = 60
    start_delay_s: int = 0

    def __post_init__(self):
        if not isinstance(self.request, str) or not self.request.strip() or len(self.request) > 8000:
            raise ValueError("Enter an instruction (up to 8,000 characters).")
        for name, minimum in (("interval_s", 1), ("start_delay_s", 0)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= 86400:
                raise ValueError(f"{name} must be whole seconds from {minimum} to 86400.")


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
        self.approval = report["approval"]
        self.plan = self.approval["plan"]

    def publish(self):
        write_json(self.output / "results.json", self.report)

    def shoot(self, preview, entry, journal, call_id):
        entry.update(capture_status="requested", capture_id=self.report["photos_taken"])
        self.report["phase"] = "capturing"
        journal.event("capture", "started", call_id + ":capture", capture_status="requested")

        def on_shutter():
            entry.update(capture_status="shutter_sent", shutter_sent_at=datetime.now(timezone.utc).isoformat())
            journal.event("capture", "started", call_id + ":capture", capture_status="shutter_sent")

        try:
            check_stop(self.stop_event)
            files = self.camera.capture(entry["capture_id"], on_shutter=on_shutter)
        except SessionStopped:
            entry["capture_status"] = "skipped_user_stop"
            journal.event("capture", "skipped", call_id + ":capture", **capture_outcome(entry))
            raise
        except (RuntimeError, OSError, KeyboardInterrupt):
            entry["capture_status"] = ("failed_after_shutter" if entry["capture_status"] == "shutter_sent"
                                       else "failed_or_unconfirmed")
            journal.event("capture", "failed", call_id + ":capture", **capture_outcome(entry))
            raise
        entry.update(capture_status="downloaded", saved_files=[str(p.relative_to(self.output)) for p in files])
        if isinstance(self.camera, VideoCamera):
            entry["video_capture_position_s"] = self.camera.last_capture_position
        elif isinstance(self.camera, MacCamera):
            entry["capture_image"] = self.camera.last_capture_info
        self.report["photos_taken"] += 1
        self.archive.record_capture(entry["capture_id"], preview, files)
        journal.event("capture", "completed", call_id + ":capture", **capture_outcome(entry))

    def observe(self, preview, entry):
        journal = ObservationJournal(self.output, preview, entry, self.publish)
        outcome = "error"
        active = False
        entry.update(assessment=None, assessment_status="pending")
        journal.event("run", "started", "run", outcome="saved")
        try:
            current_image = model_image(preview.image())
            if self.plan["reference"] is not None:
                with Image.open(self.output / self.plan["reference"]["photo"]) as reference:
                    payload = make_monitoring_payload(self.plan, current_image, reference)
            else:
                payload = make_monitoring_payload(self.plan, current_image)
            journal.message({"role": "user", "content": [
                text_content({"plan": self.plan, "frame_id": preview.id,
                              "intent_revision": self.approval["revision"]}),
                {"type": "image", "file": str(preview.path.relative_to(self.output))}]})
            self.report["phase"] = "thinking"
            self.publish()
            check_stop(self.stop_event)
            active = True
            journal.event("model", "started", "model:1", turn=1)
            self.report["api_requests"] += 1
            entry["model_calls"] += 1
            entry["run_status"] = "evaluating"
            before = time.monotonic()
            response = evaluate_preview(payload, self.config)
            probability = decision_probability(response)
            condition = "met" if probability >= CAPTURE_PROBABILITY_THRESHOLD else "unmet"
            journal.message({"role": "assistant", "content": [text_content(response)]})
            entry["usage"] = response_usage(response)
            journal.event("model", "completed", "model:1", turn=1, latency_s=time.monotonic() - before)
            active = False
            check_stop(self.stop_event)
            entry.update(assessment={"frame_id": preview.id, "intent_revision": self.approval["revision"],
                                     "condition": condition, "probability": probability,
                                     "threshold": CAPTURE_PROBABILITY_THRESHOLD}, assessment_status="available")
            if condition == "unmet":
                outcome = "no_photo"
            else:
                self.shoot(preview, entry, journal, f"frame:{preview.id}")
                outcome = entry["capture_status"]
        except SessionStopped:
            outcome = "user_stop"
            raise
        except BaseException:
            outcome = "error"
            entry["run_status"] = "failed"
            if active:
                journal.event("model", "failed", "model:1")
            raise
        finally:
            if entry["assessment_status"] == "pending":
                entry["assessment_status"] = "unavailable"
            if entry["run_status"] != "failed":
                entry["run_status"] = "finished" if outcome in ("no_photo", "downloaded") else "skipped"
            entry["outcome"] = outcome
            journal.event("run", entry["run_status"], "run", outcome=outcome,
                          model_calls=entry["model_calls"], capture_outcome=capture_outcome(entry))

    def run(self, camera_id, video=None):
        source = (VideoCamera(video, self.output / "moments", enable_capture=True) if video else
                  MacCamera(self.output / "moments", device_id=camera_id, enable_capture=True, stop_event=self.stop_event))
        with source as self.camera:
            self.report.update(camera_model=self.camera.model,
                               source=f"video:{video.name}" if video else f"continuity:{self.camera.model}",
                               camera_capabilities=self.camera.capabilities,
                               monitoring_started_at=datetime.now(timezone.utc).isoformat())
            self.publish()
            start = time.monotonic()
            next_sample = start
            self.archive = PreviewArchive(self.output, start)
            self.report.update(preview_index="previews/index.jsonl")
            while True:
                check_stop(self.stop_event)
                now = time.monotonic()
                if now < next_sample:
                    delay = next_sample - now
                    if self.stop_event is None:
                        time.sleep(delay)
                    else:
                        self.stop_event.wait(delay)
                check_stop(self.stop_event)
                if video and self.camera.remaining_seconds <= 0:
                    self.report.update(status="completed", stop_reason="video_end")
                    break
                self.report.update(phase="observing", next_observation_at=None)
                self.publish()
                try:
                    preview = self.archive.append(self.camera.latest_frame())
                except VideoEnded:
                    self.report.update(status="completed", stop_reason="video_end")
                    break
                entry = {**preview.metadata(), "preview_file": str(preview.path.relative_to(self.output)),
                         "model_calls": 0, "run_status": "saved", "capture_status": "not_requested",
                         "next_preview_interval_s": self.settings.interval_s}
                if video:
                    entry["video_position_s"] = self.camera.last_frame_position
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
                if video:
                    self.camera.position += self.settings.interval_s
                    continue
                next_sample = max(time.monotonic(), preview.received_at + self.settings.interval_s)
                self.report.update(phase="waiting", next_observation_at=time.time() + max(0, next_sample - time.monotonic()))
                self.publish()
        self.publish()
        return self.output


def run_session(settings: SessionSettings, output: Path, *, approved_plan: ApprovedPlan,
                camera_id=None, stop_event=None, video=None) -> Path:
    start_deadline = time.monotonic() + settings.start_delay_s
    scheduled_start_at = time.time() + settings.start_delay_s
    if not isinstance(approved_plan, ApprovedPlan) or approved_plan.data["request"] != settings.request:
        raise ValueError("A matching approved plan is required before monitoring.")
    source = approved_plan.data.get("source", {})
    if (source.get("mode") != ("video" if video else "camera")
            or source.get("id") != (video.metadata()["id"] if video else camera_id)
            or (not video and (not camera_id or source.get("kind") != "iphone"))):
        raise ValueError("Review the selected camera or video before monitoring.")
    config = load_config()
    validate_config(config)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "moments").mkdir()
    reference = approved_plan.data["plan"]["reference"]
    if reference:
        shutil.copyfile(approved_plan.reference_path, output / reference["photo"])
    write_json(output / "approval.json", approved_plan.data)
    report = {
        "status": "running", "phase": "scheduled" if settings.start_delay_s else "connecting",
        "scheduled_start_at": scheduled_start_at, "mode": "demo_video" if video else "continuity_camera",
        "provider": "openai", "model": MONITOR_MODEL,
        "approval": approved_plan.data,
        "model_settings": {"api": "decisions", "question_type": "predicate",
                           "capture_probability_threshold": CAPTURE_PROBABILITY_THRESHOLD,
                           "planning_model": PLANNING_MODEL},
        "api_host": urlsplit(DECISIONS_ENDPOINT).hostname,
        "instructions": MONITOR_QUESTION_INSTRUCTIONS, "request": settings.request,
        "source": f"video:{video.name}" if video else "continuity:iphone", "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {key: getattr(settings, key) for key in
                     ("interval_s", "start_delay_s")},
        "current_interval_s": settings.interval_s,
        "observations": [], "previews_saved": 0, "photos_taken": 0,
        "api_requests": 0,
    }
    if video:
        report["video"] = video.metadata()
    write_json(output / "results.json", report)
    monitor = None
    try:
        check_stop(stop_event)
        while time.monotonic() < start_deadline:
            delay = start_deadline - time.monotonic()
            if stop_event is None:
                time.sleep(max(0, delay))
            else:
                stop_event.wait(max(0, delay))
            check_stop(stop_event)
        report["phase"] = "connecting"
        write_json(output / "results.json", report)
        check_stop(stop_event)
        monitor = Monitor(settings, config, output, report, stop_event)
        return monitor.run(camera_id, video)
    except SessionStopped:
        failure = getattr(getattr(monitor, "camera", None), "failure", None)
        report.update(status="stopped", stop_reason="camera_disconnected" if failure else "user_stop")
        if failure:
            report["camera_error"] = failure
        return output
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        write_json(output / "results.json", report)
        raise
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, CameraError):
            report.update(camera_model=exc.model, source=f"continuity:{exc.model}",
                          camera_capabilities=exc.capabilities, camera_error=str(exc))
        if isinstance(exc, ApiError):
            report["api_error"] = exc.details
        write_json(output / "results.json", report)
        raise
    finally:
        report.update(phase="finished", next_observation_at=None,
                      ended_at=datetime.now(timezone.utc).isoformat())
        write_json(output / "results.json", report)
