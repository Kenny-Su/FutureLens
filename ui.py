"""Serve the WaitLess camera interface locally, without a frontend build step."""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
from urllib.parse import unquote, urlsplit
import webbrowser
from PIL import Image

from camera import MacCamera, list_cameras
from demo import DemoVideo, VideoCamera
from waitless import SessionSettings, run_session, write_json
from openai_client import load_config, validate_config, model_image
from planner import review_intent
from intent import ApprovedIntent, validate_review

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"
MAX_VIDEO_BYTES = 2 * 1024**3


def session_settings(data):
    if not isinstance(data, dict) or set(data) - {"request", "interval_s", "start_delay_s"}:
        raise ValueError("Invalid session settings.")
    return SessionSettings(**{"request": "", **data})


class SessionManager:
    """One owner of the camera. UI requests never touch an active capture session."""

    def __init__(self, output_root=ROOT / "outputs"):
        self.output_root = Path(output_root).resolve()
        self.lock = threading.Lock()
        self.worker = None
        self.operation = None
        self.stop_event = threading.Event()
        self.output = None
        self.error = None
        self.camera_check = None
        self.check_output = None
        self.intent = None
        self.intent_output = None
        self.intent_generation = 0
        self.preview_camera = None
        self.preview_context = ExitStack()
        self.demo_video = None
        self.selected_camera = None
        self.source_error = None
        self.token = secrets.token_urlsafe(32)

    def _folder(self, prefix):
        return self.output_root / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")

    def _launch(self, operation, target):
        if self.operation is not None:
            raise ValueError("A camera operation is already running.")
        self.operation = operation
        self.worker = threading.Thread(target=target, name="waitless-camera")
        self.worker.start()

    def start(self, data):
        with self.lock:
            if self.operation is not None:
                raise ValueError("A camera operation is already running.")
            draft = self._draft(data)
            if (set(data) - {"intent_id", "revision", "interval_s", "start_delay_s"} or draft["status"] != "ready"
                    or draft.get("plan_ready") is not True or draft.get("condition_stale")
                    or draft.get("question") or draft.get("pending_message_id")):
                raise ValueError("Review the current interpretation before starting.")
            output = self._folder("session")
            settings = session_settings({"request": draft["request"],
                **{key: data[key] for key in ("interval_s", "start_delay_s") if key in data}})
            validate_config(load_config())
            self._require_source()
            approved = ApprovedIntent.create(draft, self.intent_output / "reference.jpg",
                                             datetime.now(timezone.utc).isoformat())
            self._save_intent({**draft, "status": "approved", "approved_at": approved.data["approved_at"]})
            self.output, self.error = output, None
            self.camera_check = None
            self.stop_event = threading.Event()
            self._launch("monitor", lambda: self._monitor(settings, approved))

    def _monitor(self, settings, approved):
        try:
            self._close_preview()
            source = {"video": self.demo_video} if self.demo_video else {"camera_id": self.selected_camera["id"]}
            run_session(settings, self.output, approved_intent=approved,
                        stop_event=self.stop_event, **source)
        except Exception as exc:
            with self.lock:
                self.error = str(exc) if isinstance(exc, (RuntimeError, ValueError, OSError)) else type(exc).__name__
        finally:
            with self.lock:
                self.operation = None

    def _draft(self, data):
        if (not isinstance(data, dict) or not self.intent or data.get("intent_id") != self.intent["id"]
                or type(data.get("revision")) is not int or data["revision"] != self.intent["revision"]):
            raise ValueError("The intent changed. Reload the current review and try again.")
        return self.intent

    def _save_intent(self, draft):
        write_json(self.intent_output / "intent.json", draft)
        self.intent = draft

    @staticmethod
    def _message(role, message, **details):
        return {"id": secrets.token_hex(8), "role": role, "text": message, **details}

    def prepare_intent(self, data):
        config = load_config()
        validate_config(config)
        with self.lock:
            if self.operation is not None:
                raise ValueError("Wait for the current camera operation to finish.")
            self._require_source()
            previous = None
            if isinstance(data, dict) and set(data) == {"intent_id", "revision"}:
                previous = self._draft(data)
                if previous["status"] not in ("ready", "failed"):
                    raise ValueError("Only a pending review can refresh its reference.")
                settings = session_settings({"request": previous["request"]})
            else:
                if not isinstance(data, dict) or set(data) != {"request"}:
                    raise ValueError("Enter an instruction to begin the review.")
                settings = session_settings(data)
            self.intent_generation += 1
            generation = self.intent_generation
            self.intent_output = self._folder("intent")
            self.intent_output.mkdir(parents=True)
            conversation = deepcopy(previous["conversation"]) if previous else []
            message = self._message("user", "I refreshed the preview. Recheck our interpretation against this image."
                                    if previous else settings.request,
                                    kind="refresh" if previous else "message", reference_id=self.intent_output.name)
            conversation.append(message)
            draft = {"id": self.intent_output.name, "revision": 1, "request": settings.request,
                     "source": self.source(),
                     "status": "preparing", "condition": "", "targets": [], "error": None,
                     "preview": None, "condition_stale": True, "question": "", "plan_ready": False,
                     "conversation": conversation, "pending_message_id": message["id"],
                     "previous_interpretation": {k: previous[k] for k in ("condition", "targets", "question")} if previous else None}
            self._save_intent(draft)
            self._launch("intent", lambda: self._interpret(generation, deepcopy(draft), config, fresh=True))

    def refine_intent(self, data):
        with self.lock:
            draft = self._draft(data)
            if (self.operation or draft["status"] not in ("ready", "failed") or not draft.get("preview")
                    or set(data) != {"intent_id", "revision", "feedback"}):
                raise ValueError("Wait for the reference image before updating the interpretation.")
            if not isinstance(data["feedback"], str) or len(data["feedback"]) > 8000:
                raise ValueError("Feedback must be text up to 8,000 characters.")
            feedback = data["feedback"].strip()
            retry = not feedback and draft.get("pending_message_id") and draft.get("error")
            if not feedback and not retry:
                raise ValueError("Send a message to continue the conversation.")
            config = load_config()
            validate_config(config)
            self.intent_generation += 1
            generation = self.intent_generation
            draft = {**draft, "revision": draft["revision"] + 1, "status": "refining", "error": None,
                     "condition_stale": True, "plan_ready": False}
            if not retry:
                message = self._message("user", feedback, reference_id=draft["id"])
                draft["conversation"] = [*draft["conversation"], message]
                draft["pending_message_id"] = message["id"]
            self._save_intent(draft)
            self._launch("intent", lambda: self._interpret(generation, deepcopy(draft), config))

    def cancel_intent(self, data):
        with self.lock:
            draft = self._draft(data)
            if set(data) != {"intent_id", "revision"} or draft["status"] == "approved":
                raise ValueError("Only a pending review can be cancelled.")
            self.intent_generation += 1
            self._save_intent({**draft, "status": "cancelled", "revision": draft["revision"] + 1})
            # An in-flight camera/API call finishes before another operation can own the camera.

    def _interpret(self, generation, draft, config, *, fresh=False):
        folder = self.intent_output
        try:
            if fresh:
                frame, model, capabilities = self._preview_frame(folder)
                image = model_image(frame.image)
                draft.update(camera_model=model, camera_capabilities=capabilities)
                image.save(folder / "reference.jpg", quality=95, subsampling=0)
                draft.update(width=image.width, height=image.height,
                             reference_at=datetime.now(timezone.utc).isoformat(),
                             preview=f"/media/{folder.name}/reference.jpg")
                with self.lock:
                    if generation != self.intent_generation:
                        return
                    self._save_intent(deepcopy(draft))
            with Image.open(folder / "reference.jpg") as image:
                previous = ({k: draft[k] for k in ("condition", "targets", "question")}
                            if draft["condition"] else draft.get("previous_interpretation"))
                reply = review_intent(draft["request"], image, config, conversation=deepcopy(draft["conversation"]),
                                      previous=deepcopy(previous), reference_changed=fresh)
            # Validate model output before replacing the current interpretation.
            reply = validate_review(reply)
            plan = {k: reply[k] for k in ("condition", "targets")}
            draft.update(plan, plan_ready=reply["plan_ready"], question=reply["question"], status="ready", revision=draft["revision"] + 1,
                         error=None, condition_stale=False, pending_message_id=None)
            draft["conversation"].append(self._message("assistant", reply["reply"], condition=reply["condition"],
                                                       question=reply["question"], plan_ready=reply["plan_ready"], targets=deepcopy(reply["targets"]),
                                                       reference_id=draft["id"], revision=draft["revision"]))
        except Exception as exc:
            draft.update(status="ready" if draft["condition"] else "failed",
                         error=str(exc) if isinstance(exc, (RuntimeError, ValueError, OSError)) else type(exc).__name__)
        finally:
            with self.lock:
                try:
                    if generation == self.intent_generation:
                        self._save_intent(draft)
                finally:
                    self.operation = None

    def _preview_frame(self, folder):
        # Camera operations share one worker; retain the connection, not a frame queue.
        try:
            if self.preview_camera is None:
                device = (VideoCamera(self.demo_video, folder / "moments") if self.demo_video else
                          MacCamera(folder / "moments", device_id=self.selected_camera["id"]))
                self.preview_camera = self.preview_context.enter_context(device)
            camera = self.preview_camera
            return camera.latest_frame(), camera.model, deepcopy(camera.capabilities)
        except Exception:
            self._close_preview()
            raise

    def _close_preview(self):
        self.preview_camera = None
        self.preview_context.close()

    def source(self):
        return self.demo_video.metadata() if self.demo_video else {"mode": "camera", **(self.selected_camera or {"id": None})}

    def _require_source(self):
        if self.demo_video is None and self.selected_camera is None:
            raise ValueError("Connect an iPhone with Continuity Camera or import a video first.")

    def import_video(self, path, name):
        with self.lock:
            if self.operation is not None:
                raise ValueError("Wait for the current operation to finish before importing a video.")
            self.source_error = None
            self._launch("import", lambda: self._import_video(path, name))

    def _import_video(self, path, name):
        try:
            video = DemoVideo.inspect(path, name)
            self._close_preview()
            with self.lock:
                self.demo_video = video
                self.intent_generation += 1
                self.intent = self.intent_output = None
                self.camera_check = None
                self.check_output = path.parent
            self._check()
        except Exception as exc:
            path.unlink(missing_ok=True)
            with self.lock:
                self.source_error = str(exc) if isinstance(exc, (RuntimeError, ValueError, OSError)) else type(exc).__name__
                self.operation = None

    def connect_camera(self):
        with self.lock:
            if self.operation is not None:
                raise ValueError("Stop the current operation before changing the source.")
            self.source_error = None
            self._launch("connect", self._connect_camera)

    def _connect_camera(self):
        try:
            devices = list_cameras()
            if not devices:
                raise ValueError("No iPhone found. Lock your iPhone and bring it nearby or connect by USB, then try again.")
            previous_id = (self.selected_camera or {}).get("id")
            device = next((camera for camera in devices if camera["id"] == previous_id), devices[0])
            self._close_preview()
            with self.lock:
                self.demo_video = None
                self.selected_camera = deepcopy(device)
                self.intent_generation += 1
                self.intent = self.intent_output = None
                self.camera_check = None
                self.check_output = self._folder("camera-check")
            self._check()
        except Exception as exc:
            with self.lock:
                self.source_error = str(exc) if isinstance(exc, (RuntimeError, ValueError, OSError)) else type(exc).__name__
                self.operation = None

    def disconnect_camera(self):
        with self.lock:
            if self.operation is not None:
                raise ValueError("Stop monitoring or wait for the current operation before disconnecting your iPhone.")
            if self.demo_video or self.selected_camera is None:
                raise ValueError("No iPhone is connected.")
            self.source_error = None
            self._launch("disconnect", self._disconnect_camera)

    def _disconnect_camera(self):
        try:
            self._close_preview()
            with self.lock:
                self.selected_camera = None
                self.camera_check = self.check_output = None
                self.intent_generation += 1
                self.intent = self.intent_output = None
        except Exception as exc:
            with self.lock:
                self.source_error = str(exc) if isinstance(exc, (RuntimeError, ValueError, OSError)) else type(exc).__name__
        finally:
            with self.lock:
                self.operation = None

    def check_camera(self):
        with self.lock:
            if self.operation is not None:
                raise ValueError("Wait for the current camera operation to finish.")
            self._require_source()
            self.camera_check = None
            self.check_output = self._folder("camera-check")
            self._launch("check", self._check)

    def _check(self):
        result = {}
        try:
            self.check_output.mkdir(parents=True, exist_ok=True)
            frame, model, capabilities = self._preview_frame(self.check_output)
            result.update(model=model, capabilities=capabilities)
            frame.image.save(self.check_output / "preview.jpg", quality=95)
            result["preview"] = f"/media/{self.check_output.name}/preview.jpg"
        except Exception as exc:
            result["error"] = str(exc) if isinstance(exc, (RuntimeError, ValueError, OSError)) else type(exc).__name__
        finally:
            with self.lock:
                self.camera_check = result
                self.operation = None

    def stop(self):
        with self.lock:
            if self.operation == "monitor":
                self.stop_event.set()

    def close(self):
        self.stop()
        with self.lock:
            self.intent_generation += 1
        if self.worker:
            self.worker.join()
        self._close_preview()

    def _report(self):
        if self.output is None:
            return None
        try:
            return json.loads((self.output / "results.json").read_text())
        except FileNotFoundError:
            return None

    def state(self):
        with self.lock:
            return {"token": self.token, "operation": self.operation,
                    "stopping": self.operation == "monitor" and self.stop_event.is_set(),
                    "session_id": self.output.name if self.output else None,
                    "output": str(self.output) if self.output else None,
                    "report": self._report(), "error": self.error, "camera_check": self.camera_check,
                    "source": self.source(), "source_error": self.source_error,
                    "intent": deepcopy(self.intent)}

    def media_file(self, session, name):
        with self.lock:
            if self.intent_output and session == self.intent_output.name and name == "reference.jpg":
                folder, allowed = self.intent_output, {"reference.jpg"}
            elif self.check_output and session == self.check_output.name and name == "preview.jpg":
                folder, allowed = self.check_output, {"preview.jpg"}
            elif self.output and session == self.output.name:
                folder = self.output
                report = self._report() or {}
                allowed = set()
                if report.get("intent"):
                    allowed.add("intent-reference.jpg")
                for entry in report.get("observations", []):
                    allowed.add(entry["preview_file"])
                    if entry.get("capture_status") == "downloaded":
                        allowed.update(entry.get("saved_files", []))
            else:
                raise FileNotFoundError()
            path = (folder / name).resolve()
            if name not in allowed or not path.is_relative_to(folder.resolve()) or not path.is_file():
                raise FileNotFoundError()
            return path


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, manager):
        self.manager = manager
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _trusted(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return (self.headers.get("Host") in hosts
                and (origin is None or origin in {"http://" + host for host in hosts})
                and self.headers.get("Sec-Fetch-Site") not in {"cross-site"})

    def _headers(self, status, mime, length, attachment=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header("Content-Disposition", "attachment; filename=\"" + attachment + "\"")
        self.end_headers()

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _file(self, path, download=False):
        # Stream saved photos, including files from older sessions.
        with path.open("rb") as source:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            name = "".join(c for c in path.name if c.isascii() and (c.isalnum() or c in ".-_")) or "photo"
            self._headers(200, mime, path.stat().st_size, name if download else None)
            while chunk := source.read(256 * 1024):
                self.wfile.write(chunk)

    def do_GET(self):
        if not self._trusted():
            self._json({"error": "Local access only."}, 403)
            return
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/api/state":
                self._json(self.server.manager.state())
            elif path in ("/", "/app.js", "/activity.mjs", "/intent.mjs", "/review.js", "/style.css"):
                self._file(STATIC / ("index.html" if path == "/" else path[1:]))
            elif path.startswith("/media/"):
                _, _, session, name = path.split("/", 3)
                self._file(self.server.manager.media_file(session, name), download=name.startswith("moments/"))
            else:
                self._json({"error": "Not found."}, 404)
        except (FileNotFoundError, ValueError):
            self._json({"error": "Not found."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if (not self._trusted() or not secrets.compare_digest(self.headers.get("X-WaitLess-Token", ""),
                                                             self.server.manager.token)):
            self._json({"error": "Reload the local interface and try again."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path == "/api/demo":
                self._import_video(length)
                return
            if not 0 < length <= 131_072 or self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a small JSON request.")
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/start":
                self.server.manager.start(data)
            elif self.path == "/api/stop":
                self.server.manager.stop()
            elif self.path == "/api/check":
                self.server.manager.check_camera()
            elif self.path == "/api/source":
                if data != {"mode": "camera"}:
                    raise ValueError("Connect your iPhone to use the camera.")
                self.server.manager.connect_camera()
            elif self.path == "/api/camera/disconnect":
                if data != {}:
                    raise ValueError("Invalid disconnect request.")
                self.server.manager.disconnect_camera()
            elif self.path == "/api/intent/prepare":
                self.server.manager.prepare_intent(data)
            elif self.path == "/api/intent/refine":
                self.server.manager.refine_intent(data)
            elif self.path == "/api/intent/cancel":
                self.server.manager.cancel_intent(data)
            else:
                self._json({"error": "Not found."}, 404)
                return
            self._json(self.server.manager.state(), 202)
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _import_video(self, length):
        if not 0 < length <= MAX_VIDEO_BYTES or self.headers.get_content_type() != "application/octet-stream":
            raise ValueError("Choose a video file up to 2 GB.")
        name = unquote(self.headers.get("X-Video-Name", "video"))
        name = name.replace("\\", "/").split("/")[-1]
        if not name.strip() or len(name) > 255 or any(ord(c) < 32 for c in name):
            raise ValueError("Invalid video filename.")
        manager = self.server.manager
        with manager.lock:
            if manager.operation is not None:
                raise ValueError("Wait for the current operation to finish before importing a video.")
        folder = manager._folder("demo")
        folder.mkdir(parents=True)
        path = folder / "source.video"
        try:
            self.connection.settimeout(30)
            with path.open("xb") as target:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("The video upload was interrupted.")
                    target.write(chunk)
                    remaining -= len(chunk)
            manager.import_video(path, name)
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            folder.rmdir()
            raise ValueError("Could not import the video. Wait for any active operation and try again.") from None
        self._json(manager.state(), 202)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()
    manager = SessionManager()
    with LocalServer(("127.0.0.1", args.port), manager) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"WaitLess: {url}\nKeep this terminal open. Ctrl-C stops monitoring and closes the server.", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopping; allowing an active camera/API operation to finish…", flush=True)
        finally:
            manager.close()


if __name__ == "__main__":
    main()
