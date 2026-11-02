"""Native iPhone Continuity Camera previews and processed still photos."""

from dataclasses import dataclass
import io
import json
import math
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
CAMERA_APP = ROOT / "build/WaitLess Camera.app"
DEFAULT_HELPER = CAMERA_APP / "Contents/MacOS/waitless-camera"
BUILD_LOCK = threading.Lock()


def camera_helper():
    """Build the small native adapter on demand; no vendor SDK or binary bundle."""
    if sys.platform != "darwin":
        raise RuntimeError("Native camera input requires macOS 14 or later. You can still import a video.")
    source, info = ROOT / "native/macos_camera.swift", ROOT / "native/Info.plist"
    with BUILD_LOCK:
        if DEFAULT_HELPER.is_file() and DEFAULT_HELPER.stat().st_mtime >= max(source.stat().st_mtime, info.stat().st_mtime):
            return DEFAULT_HELPER
        DEFAULT_HELPER.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(info, CAMERA_APP / "Contents/Info.plist")
        try:
            subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5",
                            "-target", f"{platform.machine()}-apple-macos14.0",
                            "-module-cache-path", str(ROOT / "build/ModuleCache"),
                            str(source), "-o", str(DEFAULT_HELPER)], check=True, capture_output=True, timeout=120)
            subprocess.run(["codesign", "--force", "--sign", "-", str(CAMERA_APP)],
                           check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            DEFAULT_HELPER.unlink(missing_ok=True)
            details = getattr(exc, "stderr", b"") or b""
            raise RuntimeError("Could not build native camera support. Install Apple's Command Line Tools with "
                               f"xcode-select --install. {details.decode(errors='replace')[-2000:]}") from exc
    return DEFAULT_HELPER


def list_cameras():
    try:
        result = subprocess.run([str(camera_helper()), "--list"], capture_output=True, timeout=100)
        event = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Camera permission is waiting. Allow access and try Connect iPhone again.") from None
    except (OSError, ValueError):
        raise RuntimeError("Could not list macOS cameras. Check camera access in System Settings.") from None
    if event.get("event") == "error":
        raise RuntimeError(event["message"])
    devices = event.get("devices")
    if result.returncode or event.get("event") != "devices" or not isinstance(devices, list):
        raise RuntimeError("The native camera adapter returned an invalid device list.")
    if any(not isinstance(d, dict) or d.get("kind") != "iphone"
           or any(not isinstance(d.get(k), str) or not d[k] for k in ("id", "name")) for d in devices):
        raise RuntimeError("The native camera adapter returned an invalid device.")
    return devices


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


class MacCamera:
    """One selected input, one request at a time; only fresh frames are returned."""

    def __init__(self, output: Path, *, device_id, enable_capture=False, stop_event=None):
        self.output = Path(output).resolve()
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("Choose a camera input first.")
        self.device_id = device_id
        self.enable_capture = enable_capture
        self.stop_event = stop_event
        self.process = None
        self.log = None
        self.reader = None
        self.events = queue.Queue()
        self.model = None
        self.capabilities = {}
        self.failure = None
        self.closing = False
        self.last_capture_info = None

    def _accept_camera(self, event):
        if event.get("device_id") != self.device_id or not isinstance(event.get("model"), str) or not event["model"]:
            raise RuntimeError("Camera helper returned a different camera input")
        if (event.get("capture_kind") != "photo" or event.get("quality_prioritization") != "quality"
                or event.get("high_resolution") is not True):
            raise RuntimeError("Camera helper did not enable high-quality still-photo capture")
        self.model = event["model"]
        self.capabilities = {"live_view": True, "capture_ready": True,
                             "preview_issues": [], "capture_issues": [], "capture_kind": "photo",
                             "high_resolution": True, "quality_prioritization": "quality"}

    def _failed(self, message):
        if not self.closing:
            self.failure = message
            if self.stop_event is not None:
                self.stop_event.set()

    def _read_output(self):
        try:
            while line := self.process.stdout.readline():
                event = json.loads(line)
                if event.get("event") in ("frame", "photo"):
                    size = event.get("bytes")
                    if type(size) is not int or not 0 < size <= 64 * 1024 * 1024:
                        raise RuntimeError("Invalid camera image length")
                    event["data"] = self.process.stdout.read(size)
                    if len(event["data"]) != size:
                        raise RuntimeError("Incomplete camera image")
                if event.get("event") == "error":
                    self._failed(event.get("message", "iPhone camera failed"))
                self.events.put(event)
        except (ValueError, OSError, RuntimeError) as exc:
            self._failed(str(exc))
            self.events.put({"event": "error", "message": str(exc)})
        finally:
            if not self.failure:
                self._failed("iPhone camera connection ended; see camera.log")
            self.events.put({"event": "error", "message": "Camera input exited; see camera.log"})

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
        if self.failure and not self.closing:
            raise RuntimeError(self.failure)
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Camera helper is not running")
        self.process.stdin.write((command + "\n").encode())
        self.process.stdin.flush()

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=True)
        command = [str(camera_helper()), "--device", self.device_id]
        if self.enable_capture:
            command.append("--capture")
        self.log = (self.output / "camera.log").open("wb")
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=self.log, start_new_session=True)
            self.reader = threading.Thread(target=self._read_output, daemon=True)
            self.reader.start()
            self._accept_camera(self._receive("ready", timeout=100))
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, RuntimeError) and self.model and not isinstance(exc, CameraError):
                raise CameraError(str(exc), self.model, self.capabilities) from None
            raise

    def _frame(self, timeout=15):
        requested_at = time.monotonic()
        self._send(f"FRAME {timeout:.6f}")
        event = self._receive("frame", timeout=timeout + 5)
        return event, requested_at

    def latest_frame(self):
        event, requested_at = self._frame()
        with Image.open(io.BytesIO(event["data"])) as image:
            frame = ImageOps.exif_transpose(image).convert("RGB")
        return CameraFrame(frame, requested_at, event["frame_no"])

    def capture(self, capture_id: int, on_shutter=None, max_delay=10):
        if not self.enable_capture:
            raise RuntimeError("Still-photo capture is disabled in preview mode")
        if type(capture_id) is not int or capture_id < 0:
            raise ValueError("Capture ID must be a non-negative integer")
        if not math.isfinite(max_delay) or max_delay <= 0:
            raise ValueError("Capture expiry must be finite and positive")
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("Session stopped before capture")
        self._send(f"PHOTO {min(max_delay, 20):.6f}")
        self._receive("shutter", timeout=min(max_delay, 20) + 2)
        if on_shutter:
            on_shutter()
        event = self._receive("photo", timeout=95)
        with Image.open(io.BytesIO(event["data"])) as image:
            if image.format != "JPEG":
                raise RuntimeError("The iPhone returned an unexpected photo format")
            frame = ImageOps.exif_transpose(image)
            self.last_capture_info = {"kind": "photo", "width": frame.width, "height": frame.height,
                                      "format": "JPEG", "quality_prioritization": "quality"}
        folder = self.output / f"{capture_id:05d}"
        folder.mkdir(parents=True, exist_ok=False)
        path = folder / "photo.jpg"
        path.write_bytes(event["data"])
        return [path]

    def close(self):
        self.closing = True
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    self._send("QUIT")
                except (OSError, RuntimeError):
                    pass
                try:
                    self.process.wait(timeout=3)
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
