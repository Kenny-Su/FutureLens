"""Monitoring engine for the WaitLess camera interface."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from PIL import Image
import shutil
import time

from camera import MacCamera
from demo import VideoCamera, VideoEnded
from intent import ApprovedPlan
from openai_client import ApiError, load_config, validate_config
from monitor import (MONITOR_MODEL,
                     make_monitoring_payload, evaluate_preview, decision_probability)


# Provisional product threshold; calibrate with representative previews later.
CAPTURE_PROBABILITY_THRESHOLD = 0.5


@dataclass(frozen=True)
class SessionSettings:
    request: str
    interval_s: int = 60
    start_delay_s: int = 0
    demo_fps: float = 1

    def __post_init__(self):
        if type(self.demo_fps) not in (int, float) or not math.isfinite(self.demo_fps) or self.demo_fps <= 0:
            raise ValueError("Demo sampling rate must be a positive number.")
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
        self.plan = report["approval"]["plan"]

    def publish(self):
        write_json(self.output / "results.json", self.report)

    def observe(self, image, entry):
        if self.plan["reference"] is not None:
            with Image.open(self.output / self.plan["reference"]["photo"]) as reference:
                payload = make_monitoring_payload(self.plan, image, reference)
        else:
            payload = make_monitoring_payload(self.plan, image)
        check_stop(self.stop_event)
        self.report["phase"] = "thinking"
        self.publish()
        response = evaluate_preview(payload, self.config)
        probability = decision_probability(response)
        check_stop(self.stop_event)
        if probability >= CAPTURE_PROBABILITY_THRESHOLD:
            self.report["phase"] = "capturing"
            self.publish()
            files = self.camera.capture(self.report["photos_taken"])
            entry["saved_files"] = [str(path.relative_to(self.output)) for path in files]
            self.report["photos_taken"] += 1
            self.publish()

    def run(self, camera_id, video=None):
        source = (VideoCamera(video, self.output / "moments", enable_capture=True) if video else
                  MacCamera(self.output / "moments", device_id=camera_id, enable_capture=True, stop_event=self.stop_event))
        with source as self.camera:
            self.report.update(camera_model=self.camera.model,
                               source=f"video:{video.name}" if video else f"continuity:{self.camera.model}",
                               monitoring_started_at=datetime.now(timezone.utc).isoformat())
            self.publish()
            start = time.monotonic()
            next_sample = start
            (self.output / "previews").mkdir()
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
                self.report.update(phase="observing", next_observation_at=None)
                self.publish()
                try:
                    frame = self.camera.latest_frame()
                except VideoEnded:
                    self.report.update(status="completed", stop_reason="video_end")
                    break
                frame_id = len(self.report["observations"])
                preview_file = f"previews/{frame_id:06d}.jpg"
                frame.image.save(self.output / preview_file, format="JPEG", quality=95, subsampling=0)
                entry = {"frame_id": frame_id, "timestamp_s": frame.received_at - start, "preview_file": preview_file}
                if video:
                    entry["video_position_s"] = self.camera.last_frame_position
                self.report["observations"].append(entry)
                self.publish()
                self.observe(frame.image, entry)
                check_stop(self.stop_event)
                if video:
                    self.camera.position = len(self.report["observations"]) / self.settings.demo_fps
                    continue
                next_sample = max(time.monotonic(), frame.received_at + self.settings.interval_s)
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
    report = {
        "status": "running", "phase": "scheduled" if settings.start_delay_s else "connecting",
        "scheduled_start_at": scheduled_start_at, "mode": "demo_video" if video else "continuity_camera",
        "provider": "openai", "model": MONITOR_MODEL,
        "approval": approved_plan.data,
        "capture_probability_threshold": CAPTURE_PROBABILITY_THRESHOLD,
        "request": settings.request,
        "source": f"video:{video.name}" if video else "continuity:iphone", "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {key: getattr(settings, key) for key in
                     (("demo_fps",) if video else ("interval_s", "start_delay_s"))},
        "observations": [], "photos_taken": 0,
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
        report.update(status="failed", error=str(exc))
        if isinstance(exc, ApiError):
            report["api_error"] = exc.details
        write_json(output / "results.json", report)
        raise
    finally:
        report.update(phase="finished", next_observation_at=None,
                      ended_at=datetime.now(timezone.utc).isoformat())
        write_json(output / "results.json", report)
