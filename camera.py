"""Auto-detect a Sony USB camera and check its live-view/photo capabilities."""

from dataclasses import dataclass
import io
import json
import math
from pathlib import Path
import queue
import subprocess
import threading
import time

from PIL import Image

DEFAULT_HELPER = Path(__file__).with_name("build") / "sony/sony_camera"


@dataclass
class CameraFrame:
    image: Image.Image
    received_at: float
    frame_no: int


class CameraError(RuntimeError):
    def __init__(self, message, model, capabilities):
        super().__init__(message)
        self.model = model
        self.capabilities = capabilities


class SonyCamera:
    """One SDK process, one request at a time, no queued preview frames."""

    def __init__(self, output: Path, *, enable_capture=False, helper=DEFAULT_HELPER):
        self.output = Path(output).resolve()
        self.helper = Path(helper).resolve()
        self.enable_capture = enable_capture
        self.process = None
        self.log = None
        self.reader = None
        self.events = queue.Queue()
        self.model = None
        self.capabilities = {}

    def _accept_camera(self, event):
        if event.get("protocol") != 2:
            raise RuntimeError("Rebuild the camera helper: .venv/bin/python scripts/build_sony.py (capability protocol 2 required)")
        if event.get("connection") != "USB" or not isinstance(event.get("model"), str) or not event["model"]:
            raise RuntimeError("Camera helper returned invalid USB camera information")
        self.model = event["model"]
        caps = event.get("capabilities")
        if (not isinstance(caps, dict) or any(type(caps.get(key)) is not bool for key in ("live_view", "capture_ready"))
                or any(not isinstance(caps.get(key), list) or any(not isinstance(value, str) for value in caps[key])
                       for key in ("preview_issues", "capture_issues"))):
            raise RuntimeError("Camera helper returned invalid capability information")
        self.capabilities = caps
        issues = list(caps["preview_issues"])
        if not caps["live_view"] and not issues:
            issues.append("Live view is unavailable.")
        if self.enable_capture:
            issues.extend(caps["capture_issues"])
            if not caps["capture_ready"] and not issues:
                issues.append("Still-photo capture is unavailable.")
        if issues:
            raise CameraError(f"{self.model}: {' '.join(issues)}", self.model, caps)

    def _read_output(self):
        try:
            while line := self.process.stdout.readline():
                event = json.loads(line)
                if event.get("event") == "frame":
                    size = event.get("bytes")
                    if type(size) is not int or not 0 < size <= 32 * 1024 * 1024:
                        raise RuntimeError("Invalid camera image length")
                    event["data"] = self.process.stdout.read(size)
                    if len(event["data"]) != size:
                        raise RuntimeError("Incomplete camera image")
                self.events.put(event)
        except (ValueError, OSError, RuntimeError) as exc:
            self.events.put({"event": "error", "message": str(exc)})
        finally:
            self.events.put({"event": "error", "message": "Camera helper exited; see camera-sdk.log"})

    def _receive(self, expected, timeout=20):
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("Camera helper timed out; no automatic capture retry") from None
        if event.get("event") == "error":
            raise RuntimeError(event.get("message", "Camera helper failed"))
        if event.get("event") != expected:
            raise RuntimeError(f"Expected camera event {expected}, got {event.get('event')}")
        return event

    def _send(self, command):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Camera helper is not running")
        self.process.stdin.write((command + "\n").encode())
        self.process.stdin.flush()

    def __enter__(self):
        if not self.helper.is_file():
            raise RuntimeError("Build the camera helper first: .venv/bin/python scripts/build_sony.py")
        self.output.mkdir(parents=True, exist_ok=True)
        self.log = (self.output.parent / "camera-sdk.log").open("wb")
        command = [str(self.helper), str(self.output)]
        if self.enable_capture:
            command.append("--enable-capture")
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=self.log, start_new_session=True)
            self.reader = threading.Thread(target=self._read_output, daemon=True)
            self.reader.start()
            self._accept_camera(self._receive("ready", timeout=30))
            if self.enable_capture:
                self._send("PREPARE")
                self._receive("prepared")
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, RuntimeError) and self.model and not isinstance(exc, CameraError):
                raise CameraError(str(exc), self.model, self.capabilities) from None
            raise

    def latest_frame(self):
        # Timestamp the request, conservatively including SDK retrieval and decode time.
        requested_at = time.monotonic()
        self._send("FRAME")
        event = self._receive("frame")
        with Image.open(io.BytesIO(event["data"])) as image:
            frame = image.convert("RGB")
        return CameraFrame(frame, requested_at, event["frame_no"])

    def capture(self, capture_id: int, on_shutter=None, max_delay=10):
        if not self.enable_capture:
            raise RuntimeError("Shutter control is disabled in preview mode")
        if type(capture_id) is not int or capture_id < 0:
            raise ValueError("Capture ID must be a non-negative integer")
        if not math.isfinite(max_delay) or max_delay <= 0:
            raise ValueError("Capture expiry must be finite and positive")
        self._send(f"CAPTURE {capture_id:05d} {max_delay:.6f}")
        self._receive("shutter_sent")
        if on_shutter:
            on_shutter()
        event = self._receive("captured", timeout=70)
        files = [Path(value).resolve() for value in event["files"]]
        expected = self.output / f"{capture_id:05d}"
        if not files or any(not p.is_relative_to(expected) or not p.is_file() or p.stat().st_size == 0 for p in files):
            raise RuntimeError("Camera reported a download without a valid photo file")
        return files

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    self._send("QUIT")
                except (OSError, RuntimeError):
                    pass
                try:
                    # Let an in-progress shutter/transfer finish and restore settings.
                    self.process.wait(timeout=75)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
            if self.reader:
                self.reader.join(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def __exit__(self, *_):
        self.close()
