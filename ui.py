"""Serve the FutureLens camera interface locally, without a frontend build step."""

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
from urllib.parse import unquote, urlsplit
import webbrowser

from camera import SonyCamera, DEFAULT_HELPER
from futurelens import SessionSettings, run_session
from model import load_config, validate_config

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"


def session_settings(data):
    if not isinstance(data, dict) or set(data) - {"request", "interval", "duration_minutes", "max_shots", "max_requests"}:
        raise ValueError("Invalid session settings.")
    values = dict(data)
    minutes = values.pop("duration_minutes", 60)
    if type(minutes) not in (int, float):
        raise ValueError("Duration must be a positive number of minutes.")
    return SessionSettings(**{"request": "", **values, "duration": minutes * 60})


class SessionManager:
    """One owner of the camera. UI requests never touch an active SDK session."""

    def __init__(self, output_root=ROOT / "outputs", helper=DEFAULT_HELPER):
        self.output_root, self.helper = Path(output_root), Path(helper)
        self.lock = threading.Lock()
        self.worker = None
        self.operation = None
        self.stop_event = threading.Event()
        self.output = None
        self.error = None
        self.camera_check = None
        self.check_output = None
        self.token = secrets.token_urlsafe(32)

    def _folder(self, prefix):
        return self.output_root / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")

    def _launch(self, operation, target):
        if self.operation is not None:
            raise ValueError("A camera operation is already running.")
        self.operation = operation
        self.worker = threading.Thread(target=target, name="futurelens-camera")
        self.worker.start()

    def start(self, data):
        with self.lock:
            if self.operation is not None:
                raise ValueError("A camera operation is already running.")
            output = self._folder("session")
            settings = session_settings(data)
            validate_config(load_config())
            if not self.helper.is_file():
                raise ValueError("The bundled camera runtime is missing. Restore runtime/sony from the repository.")
            self.output, self.error = output, None
            self.stop_event = threading.Event()
            self._launch("monitor", lambda: self._monitor(settings))

    def _monitor(self, settings):
        try:
            run_session(settings, self.output, camera_helper=self.helper, stop_event=self.stop_event)
        except Exception as exc:
            with self.lock:
                self.error = str(exc) if isinstance(exc, (RuntimeError, ValueError, OSError)) else type(exc).__name__
        finally:
            with self.lock:
                self.operation = None

    def check_camera(self):
        with self.lock:
            if self.operation is not None:
                raise ValueError("Wait for the current camera operation to finish.")
            self.camera_check = None
            self.check_output = self._folder("camera-check")
            self._launch("check", self._check)

    def _check(self):
        result = {}
        try:
            self.check_output.mkdir(parents=True)
            with SonyCamera(self.check_output / "moments", helper=self.helper) as camera:
                result.update(model=camera.model, capabilities=camera.capabilities)
                camera.latest_frame().image.save(self.check_output / "preview.jpg", quality=95)
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
        if self.worker:
            self.worker.join()

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
                    "api_ready": bool(load_config().api_key), "helper_ready": self.helper.is_file()}

    def media_file(self, session, name):
        with self.lock:
            if self.check_output and session == self.check_output.name and name == "preview.jpg":
                folder, allowed = self.check_output, {"preview.jpg"}
            elif self.output and session == self.output.name:
                folder = self.output
                report = self._report() or {}
                allowed = set()
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
        # Stream original camera files; a RAW download need not fit in memory.
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
            elif path in ("/", "/app.js", "/style.css"):
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
        if (not self._trusted() or not secrets.compare_digest(self.headers.get("X-FutureLens-Token", ""),
                                                             self.server.manager.token)):
            self._json({"error": "Reload the local interface and try again."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32_768 or self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a small JSON request.")
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/start":
                self.server.manager.start(data)
            elif self.path == "/api/stop":
                self.server.manager.stop()
            elif self.path == "/api/check":
                self.server.manager.check_camera()
            else:
                self._json({"error": "Not found."}, 404)
                return
            self._json(self.server.manager.state(), 202)
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"error": str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()
    manager = SessionManager()
    with LocalServer(("127.0.0.1", args.port), manager) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"FutureLens: {url}\nKeep this terminal open. Ctrl-C stops monitoring and closes the server.", flush=True)
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
