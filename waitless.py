"""Monitoring engine for the WaitLess camera interface."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import time
from urllib.parse import urlsplit

from camera import MacCamera, CameraError
from demo import VideoCamera, VideoEnded
from agent import (MonitoringAgent, COMPACT_INPUT_TOKENS, COMPACT_REQUEST_BYTES, COMPACT_IMAGES,
                   KEEP_OBSERVATIONS, COMPACTION_RETRY_SECONDS)
from history import Preview, PreviewArchive, AgentJournal, capture_outcome, append_json
from intent import ApprovedIntent, json_response, validate_snapshot, validate_wait_reason
from model import (API_ENDPOINT, MONITORING_SYSTEM_PROMPT, MODEL, MAX_IMAGES, MAX_REQUEST_BYTES, MAX_INPUT_TOKENS,
                   ApiError, CapacityReached, call_model, check_capacity, count_tokens, image_content,
                   load_config, parse_compaction, response_usage, text_content, validate_config, MAX_CALLS_PER_OBSERVATION,
                   EFFORT, THINKING, MAX_OUTPUT_TOKENS, model_image)


@dataclass(frozen=True)
class SessionSettings:
    request: str
    max_frame_age: float = 30
    cooldown: float = 5
    interval_s: int = 60
    start_delay_s: int = 0

    def __post_init__(self):
        if not isinstance(self.request, str) or not self.request.strip() or len(self.request) > 8000:
            raise ValueError("Enter an instruction (up to 8,000 characters).")
        for name, minimum in (("interval_s", 1), ("start_delay_s", 0)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= 86400:
                raise ValueError(f"{name} must be whole seconds from {minimum} to 86400.")
        for name in ("max_frame_age", "cooldown"):
            value = getattr(self, name)
            try:
                valid = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid or (value < 0 if name == "cooldown" else value <= 0):
                qualifier = "non-negative" if name == "cooldown" else "positive"
                raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a finite, {qualifier} number.")


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
        self.agent = MonitoringAgent(output)
        self.intent = report["intent"]
        self.reference = Preview(-1, 0, 0, -1, output / "intent-reference.jpg",
                                 self.intent["width"], self.intent["height"])

    def publish(self):
        write_json(self.output / "results.json", self.report)

    def expired(self, preview):
        if self.stop_event is not None and self.stop_event.is_set():
            return "user_stop"
        if time.monotonic() - preview.received_at >= self.settings.max_frame_age:
            return "stale_preview"
        return None

    def shoot(self, preview, entry, journal, call_id):
        reason = self.expired(preview)
        statuses = {"user_stop": "skipped_user_stop", "stale_preview": "skipped_stale_frame"}
        if reason:
            entry["capture_status"] = statuses[reason]
        elif entry["assessment"]["condition"] != "met":
            entry["capture_status"] = ("skipped_intent_uncertain" if entry["assessment"]["condition"] == "uncertain"
                                       else "skipped_condition_unmet")
        elif time.monotonic() - self.last_capture < self.settings.cooldown:
            entry["capture_status"] = "skipped_cooldown"
        else:
            entry.update(capture_status="requested", capture_id=self.report["photos_taken"])
            self.report["phase"] = "capturing"
            journal.event("capture", "started", call_id + ":capture", capture_status="requested")

            def on_shutter():
                entry.update(capture_status="shutter_sent", shutter_sent_at=datetime.now(timezone.utc).isoformat())
                journal.event("capture", "started", call_id + ":capture", capture_status="shutter_sent")

            # Writing the request and camera preparation can consume the remaining allowance.
            reason = self.expired(preview)
            remaining = preview.received_at + self.settings.max_frame_age - time.monotonic()
            if reason == "user_stop":
                entry["capture_status"] = "skipped_user_stop"
            elif reason or remaining <= 0:
                entry["capture_status"] = "skipped_expired_before_shutter"
            else:
                try:
                    files = self.camera.capture(entry["capture_id"], on_shutter=on_shutter, max_delay=remaining)
                except VideoEnded:
                    entry["capture_status"] = "skipped_video_end"
                except (RuntimeError, OSError, KeyboardInterrupt):
                    entry["capture_status"] = ("failed_after_shutter" if entry["capture_status"] == "shutter_sent"
                                               else "failed_or_unconfirmed")
                    journal.event("capture", "failed", call_id + ":capture", **capture_outcome(entry))
                    raise
                else:
                    entry.update(capture_status="downloaded", saved_files=[str(p.relative_to(self.output)) for p in files])
                    if isinstance(self.camera, VideoCamera):
                        entry["video_capture_position_s"] = self.camera.last_capture_position
                    self.report["photos_taken"] += 1
                    self.last_capture = time.monotonic()
                    self.archive.record_capture(entry["capture_id"], preview, files)
        journal.event("capture", "completed" if entry["capture_status"] == "downloaded" else "skipped",
                      call_id + ":capture", **capture_outcome(entry))
        return capture_outcome(entry)

    def current_state(self, preview, model_calls):
        now = time.monotonic()
        return {"photos_taken": self.report["photos_taken"],
                "calls_per_observation": MAX_CALLS_PER_OBSERVATION,
                "observation_calls_remaining": MAX_CALLS_PER_OBSERVATION - model_calls,
                "preview_freshness_s": self.settings.max_frame_age,
                "preview_remaining_seconds": max(0, preview.received_at + self.settings.max_frame_age - now),
                "capture_cooldown_s": self.settings.cooldown,
                "cooldown_remaining_s": max(0, self.settings.cooldown - (now - self.last_capture))}

    def append_message(self, message, journal, images=()):
        self.agent.append(message, journal.preview.id, images)
        journal.message(message, images)

    def observe(self, preview, entry):
        journal = AgentJournal(self.output, preview, entry, self.publish)
        entry["context_revision"] = self.agent.revision
        active = None
        outcome = "error"
        decision_corrections = 0
        journal.message({"role": "system", "content": [{"type": "text", "text": MONITORING_SYSTEM_PROMPT}]})
        initial = {"user_request": self.settings.request, "preview": preview.metadata(),
                   "session": self.current_state(preview, 0),
                   "history": {"count": preview.id, "first_frame_id": 0 if preview.id else None,
                               "latest_frame_id": preview.id - 1 if preview.id else None},
                   "sampling": {"interval_s": self.settings.interval_s},
                   "latest_capture_outcome": self.latest_capture_outcome,
                   "last_downloaded_capture": self.last_downloaded_capture}
        current_image = model_image(preview.image())
        initial.update(approved_intent=self.intent, current_image_width=current_image.width,
                       current_image_height=current_image.height, intent_revision=self.intent["revision"])
        message = {"role": "user", "content": [text_content(initial),
            text_content({"image": "REFERENCE", "captured_at": self.intent["reference_at"]}),
            image_content(self.reference.image()), text_content({"image": "CURRENT", "frame_id": preview.id}),
            image_content(current_image)]}
        entry.update(assessment=None, assessment_status="pending")
        self.append_message(message, journal, [self.reference, preview])
        journal.event("run", "started", "run", outcome="saved")
        try:
            for turn in range(1, MAX_CALLS_PER_OBSERVATION + 1):
                outcome = self.expired(preview)
                if outcome:
                    break
                action_id = f"model:{turn}"
                active = ("model", action_id)
                self.report["phase"] = "checking"
                journal.event("model", "checking", action_id, turn=turn)
                payload = self.agent.payload()
                metrics = check_capacity(payload)
                outcome = self.expired(preview)
                if outcome:
                    break
                self.report["token_count_requests"] += 1
                metrics["input_tokens"] = count_tokens(payload, self.config, timeout=90)
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
                response = call_model(payload, self.config, timeout=90)
                call = self.agent.accept_response(response, preview.id, metrics["input_tokens"])
                message = {"role": "assistant", "content": response["content"]}
                journal.message(message)
                journal.event("model", "completed", action_id, turn=turn, latency_s=time.monotonic() - before)
                active = None
                if call is None or call["name"] == "shoot":
                    try:
                        if call is None:
                            end = json_response(response)
                            if (not isinstance(end, dict) or end.get("action") != "wait"
                                    or "snapshot" not in end or set(end) - {"action", "snapshot", "wait_reason"}):
                                raise ValueError("Missing wait observation snapshot.")
                            snapshot = end["snapshot"]
                        else:
                            snapshot = call["input"].get("snapshot")
                        assessment = validate_snapshot(snapshot, self.intent, preview.id)
                    except ValueError as exc:
                        entry.update(assessment=None, assessment_status="unavailable", assessment_error=str(exc))
                        if call:
                            self.append_message({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                "is_error": True, "content": [text_content({"error": str(exc), "executed": False})]}]}, journal)
                        outcome = "invalid_assessment"
                        break
                    if call is None:
                        try:
                            state = self.current_state(preview, entry["model_calls"])
                            wait_reason = validate_wait_reason(
                                end.get("wait_reason"), assessment["condition"],
                                photos_taken=state["photos_taken"], cooldown_remaining_s=state["cooldown_remaining_s"])
                        except ValueError as exc:
                            entry.update(assessment=None, assessment_status="pending", decision_error=str(exc))
                            entry.pop("wait_reason", None)
                            # Correct a contradictory decision once on this same frame. Never
                            # translate a "met" assessment into a shutter request.
                            feedback = [text_content({"error": str(exc), "executed": False,
                                "current_preview_id": preview.id, "session": state,
                                "instruction": "Reconcile the decision for this CURRENT frame. If the condition is met "
                                    "and a requested photo is still due, call shoot. Otherwise return a consistent "
                                    "snapshot and wait_reason. Respect the approved quantity, repetition and timing; "
                                    "do not repeat an already fulfilled one-photo request."})]
                            journal.event("model", "completed", action_id, turn=turn, decision_error=str(exc))
                            self.append_message({"role": "user", "content": feedback}, journal)
                            outcome = self.expired(preview) or "invalid_decision"
                            if decision_corrections or outcome != "invalid_decision" or turn == MAX_CALLS_PER_OBSERVATION:
                                break
                            decision_corrections += 1
                            continue
                        entry["wait_reason"] = wait_reason
                    else:
                        entry.pop("wait_reason", None)
                    entry.pop("decision_error", None)
                    entry.update(assessment=assessment, assessment_status="available")
                    self.publish()
                if call is None:
                    outcome = self.expired(preview) or "no_photo"
                    break
                name, arguments, call_id = call["name"], call["input"], call["id"]
                active = ("tool", "tool:" + call_id)
                journal.event("tool", "started", "tool:" + call_id, name=name, arguments=arguments)
                self.report["phase"] = "capturing" if name == "shoot" else "retrieving"
                self.publish()
                images, is_error, summary = [], False, {}
                if name == "shoot":
                    try:
                        result = self.shoot(preview, entry, journal, call_id)
                    except (RuntimeError, OSError, KeyboardInterrupt):
                        # Even an uncertain capture has a factual terminal tool result.
                        self.append_message({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                            "is_error": True, "content": [text_content(capture_outcome(entry))]}]}, journal)
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
                if name != "shoot":
                    content.append(text_content({"current_preview_id": preview.id,
                        "session": self.current_state(preview, entry["model_calls"])}))
                message = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                          "content": content, "is_error": is_error}]}
                self.append_message(message, journal, images)
                journal.event("tool", "failed" if is_error else "completed",
                              "tool:" + call_id, name=name, **summary)
                active = None
                if name == "shoot":
                    outcome = entry["capture_status"]
                    break
                outcome = self.expired(preview)
                if outcome:
                    break
            else:
                outcome = "run_limit"
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
            if entry.get("assessment_status") == "pending" or outcome in ("run_limit", "error", "invalid_assessment", "invalid_decision"):
                entry.update(assessment=None, assessment_status="unavailable")
                entry.pop("wait_reason", None)
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

    def compact_between_observations(self, preview, entry):
        check_stop(self.stop_event)
        candidate = self.agent.compaction_candidate(preview.id, time.monotonic())
        if candidate is None:
            return
        cut, payload = candidate
        check_capacity(payload)
        self.agent.last_attempt_observation = preview.id
        self.report.update(phase="compacting", next_observation_at=None)
        info = {"status": "checking", "observation_id": preview.id,
                "context_revision": self.agent.revision}
        self.report["compaction"] = info
        self.publish()
        started = time.monotonic()
        # Record this in the last observation's trace, but not in its inference count.
        def event(status, **details):
            info.update(status=status, latency_s=time.monotonic() - started, **details)
            self.agent.record("compaction_attempt", **info)
            event_record = {"id": f"{preview.id}:{len(entry['events'])}", "sequence": len(entry["events"]),
                "observation_id": preview.id, "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": max(0, time.monotonic() - preview.received_at), "kind": "compaction",
                "action_id": f"compaction:{preview.id}", **info}
            append_json(self.output / entry["events_file"], event_record)
            entry["events"].append(event_record)
            self.publish()
        event("checking")
        reserved = dispatched = False
        try:
            check_stop(self.stop_event)
            self.report["token_count_requests"] += 1
            count_tokens(payload, self.config, timeout=90)
            check_stop(self.stop_event)
            self.report["api_requests"] += 1
            self.report["compaction_requests"] += 1
            reserved = True
            event("started")
            check_stop(self.stop_event)
            dispatched = True
            response = call_model(payload, self.config, timeout=90)
            info["usage"] = response_usage(response) if isinstance(response, dict) else {}
            parse_compaction(response)
        except (SessionStopped, CapacityReached):
            if reserved and not dispatched:
                self.report["api_requests"] -= 1
                self.report["compaction_requests"] -= 1
            event("skipped")
            raise
        except (ApiError, RuntimeError, ValueError) as exc:
            self.agent.compaction_failed(time.monotonic())
            details = {"error_type": type(exc).__name__, "retry_after_s": COMPACTION_RETRY_SECONDS}
            if isinstance(exc, ApiError):
                details["api_error"] = exc.details
            event("failed", **details)
            return
        # Persistence errors must stop the session, never masquerade as recoverable API failures.
        self.agent.apply_compaction(response, cut)
        self.report["compactions_completed"] += 1
        self.report["context_revision"] = self.agent.revision
        event("completed", context_revision=self.agent.revision, usage=response_usage(response))

    def run(self, camera_id, video=None):
        source = (VideoCamera(video, self.output / "moments", enable_capture=True) if video else
                  MacCamera(self.output / "moments", enable_capture=True, device_id=camera_id))
        with source as self.camera:
            self.report.update(camera_model=self.camera.model,
                               source=f"video:{video.name}" if video else f"macos:{self.camera.model}",
                               camera_id=camera_id if not video else None,
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
                    if video:
                        delay = min(delay, self.camera.remaining_seconds)
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
                if self.report["status"] == "capacity_reached":
                    break
                if video and self.camera.remaining_seconds <= 0:
                    self.report.update(status="completed", stop_reason="video_end")
                    break
                try:
                    self.compact_between_observations(preview, entry)
                except CapacityReached as exc:
                    self.report.update(status="capacity_reached", stop_reason="capacity_reached", capacity=exc.details)
                    break
                check_stop(self.stop_event)
                next_sample = max(time.monotonic(), preview.received_at + self.settings.interval_s)
                if video:
                    next_sample = min(next_sample, self.camera.started + video.duration)
                self.report.update(phase="waiting", next_observation_at=time.time() + max(0, next_sample - time.monotonic()))
                self.publish()
        self.publish()
        return self.output


def run_session(settings: SessionSettings, output: Path, *, approved_intent: ApprovedIntent,
                camera_id=None, stop_event=None, video=None) -> Path:
    start_deadline = time.monotonic() + settings.start_delay_s
    scheduled_start_at = time.time() + settings.start_delay_s
    if not isinstance(approved_intent, ApprovedIntent) or approved_intent.data["request"] != settings.request:
        raise ValueError("A matching approved intent is required before monitoring.")
    source = approved_intent.data.get("source", {})
    if (source.get("mode") != ("video" if video else "camera")
            or source.get("id") != (video.metadata()["id"] if video else camera_id)
            or (not video and not camera_id)):
        raise ValueError("Review the selected camera or video before monitoring.")
    config = load_config()
    validate_config(config)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "moments").mkdir()
    shutil.copyfile(approved_intent.reference_path, output / "intent-reference.jpg")
    write_json(output / "intent.json", approved_intent.data)
    report = {
        "status": "running", "phase": "scheduled" if settings.start_delay_s else "connecting",
        "scheduled_start_at": scheduled_start_at, "mode": "demo_video" if video else "macos_camera",
        "provider": "anthropic", "model": MODEL,
        "intent": approved_intent.data,
        "model_settings": {"effort": EFFORT, "thinking": THINKING["type"],
                           "thinking_display": THINKING["display"], "max_output_tokens": MAX_OUTPUT_TOKENS,
                           "automatic_prompt_caching": True},
        "api_host": urlsplit(API_ENDPOINT).hostname,
        "instructions": MONITORING_SYSTEM_PROMPT, "request": settings.request,
        "source": f"video:{video.name}" if video else "macos:camera", "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {key: getattr(settings, key) for key in
                     ("cooldown", "max_frame_age", "interval_s", "start_delay_s")},
        "current_interval_s": settings.interval_s,
        "capacity_limits": {"images": MAX_IMAGES, "request_bytes": MAX_REQUEST_BYTES, "input_tokens": MAX_INPUT_TOKENS},
        "calls_per_observation": MAX_CALLS_PER_OBSERVATION,
        "observations": [], "previews_saved": 0, "photos_taken": 0,
        "api_requests": 0, "token_count_requests": 0,
        "conversation_file": "conversation.jsonl", "context_revision": 0,
        "compaction_requests": 0, "compactions_completed": 0, "compaction": None,
        "compaction_settings": {"input_tokens": COMPACT_INPUT_TOKENS, "request_bytes": COMPACT_REQUEST_BYTES,
                                "images": COMPACT_IMAGES, "keep_observations": KEEP_OBSERVATIONS,
                                "retry_after_s": COMPACTION_RETRY_SECONDS},
    }
    if video:
        report["video"] = video.metadata()
    write_json(output / "results.json", report)
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
        return Monitor(settings, config, output, report, stop_event).run(camera_id, video)
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
            report.update(camera_model=exc.model, source=f"macos:{exc.model}",
                          camera_capabilities=exc.capabilities, camera_error=str(exc))
        if isinstance(exc, ApiError):
            report["api_error"] = exc.details
        write_json(output / "results.json", report)
        raise
    finally:
        report.update(phase="finished", next_observation_at=None,
                      ended_at=datetime.now(timezone.utc).isoformat())
        write_json(output / "results.json", report)
