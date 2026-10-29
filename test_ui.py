"""UI boundaries: camera ownership, settings, local HTTP access, and saved media."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from model import ApiConfig
from test_camera import Clock, FakeCamera
from test_model import answer
from ui import LocalServer, SessionManager, session_settings


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.helper = self.root / "helper"
        self.helper.touch()
        self.manager = SessionManager(self.root / "outputs", self.helper)
        self.addCleanup(self.manager.close)
        config = patch("ui.load_config", return_value=ApiConfig(api_key="test-only-secret"))
        config.start()
        self.addCleanup(config.stop)

    def test_settings_validate_before_camera_or_api_work(self):
        settings = session_settings({"request": "scene"})
        self.assertEqual(settings.preview_mode, "auto")
        self.assertEqual(session_settings({"request": "scene", "preview_mode": "fixed", "interval": 45}).interval, 45)
        self.assertEqual((settings.interval, settings.duration, settings.max_shots, settings.max_requests), (60, 3600, 1, 360))
        for settings in ({"request": ""}, {"request": 1}, {"request": "x", "interval": -1},
                         {"request": "x", "interval": True}, {"request": "x", "duration_minutes": float("nan")},
                         {"request": "x", "duration_minutes": True}, {"request": "x", "preview_mode": "other"},
                         {"request": "x", "duration_minutes": "10"},
                         {"request": "x", "duration_minutes": 10**400},
                         {"request": "x", "max_shots": 1.5}, {"request": "x", "output": "/tmp/x"},
                         {"request": "x", "dry_run": True}, {"request": "x", "camera_helper": "other"}, []):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.manager.start(settings)
        self.assertIsNone(self.manager.operation)
        self.assertFalse(self.manager.output_root.exists())

    def test_ui_settings_drive_real_engine_and_saved_photo_report(self):
        clock = Clock()
        def camera(output, **kwargs):
            self.assertTrue(kwargs["enable_capture"])
            self.assertEqual(kwargs["helper"], self.helper)
            return FakeCamera(clock, output.parent)
        with patch("waitless.SonyCamera", side_effect=camera), \
                patch("waitless.load_config", return_value=ApiConfig(api_key="offline-test")), \
                patch("waitless.count_tokens", return_value=1000), \
                patch("waitless.call_model", return_value=answer("shoot")) as inference, \
                patch("waitless.time.monotonic", side_effect=clock.monotonic):
            self.manager.start({"request": "Capture this scene", "duration_minutes": 2,
                                "preview_mode": "fixed", "interval": 45, "max_shots": 1, "max_requests": 3})
            self.manager.worker.join(2)
            self.assertFalse(self.manager.worker.is_alive())
        state = self.manager.state()
        self.assertIsNone(state["error"])
        report = state["report"]
        self.assertEqual(report["settings"]["preview_mode"], "fixed")
        self.assertEqual((report["settings"]["duration"], report["settings"]["interval"]), (120, 45))
        self.assertEqual((report["status"], report["stop_reason"], report["photos_taken"]),
                         ("completed", "max_shots", 1))
        entry = report["observations"][0]
        self.assertEqual(entry["capture_status"], "downloaded")
        self.assertEqual((entry["run_status"], entry["model_calls"]), ("finished", 1))
        self.assertEqual(entry["events"][-1]["outcome"], "downloaded")
        self.assertTrue((self.manager.output / entry["conversation_file"]).is_file())
        self.assertNotIn('"base64"', json.dumps(state))
        self.assertTrue(self.manager.media_file(state["session_id"], entry["saved_files"][0]).is_file())
        inference.assert_called_once()

    def test_only_one_session_owns_camera_and_stop_reaches_runner(self):
        entered = threading.Event()
        def runner(settings, output, *, camera_helper, stop_event):
            entered.set()
            self.assertTrue(stop_event.wait(2))
        with patch("ui.run_session", side_effect=runner) as run:
            self.manager.start({"request": "scene"})
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.manager.start({"request": "duplicate"})
            with self.assertRaises(ValueError):
                self.manager.check_camera()
            self.manager.stop()
            self.manager.close()
        run.assert_called_once()
        self.assertIsNone(self.manager.operation)
        self.assertIsNone(self.manager.error)

    def test_camera_check_never_enables_capture_and_releases_camera(self):
        with patch("ui.SonyCamera") as camera:
            device = camera.return_value.__enter__.return_value
            device.model = "Test Sony"
            device.capabilities = {"capture_ready": True}
            device.latest_frame.return_value.image = Image.new("RGB", (32, 32))
            self.manager.check_camera()
            self.manager.close()
            self.assertNotIn("enable_capture", camera.call_args.kwargs)
            device.capture.assert_not_called()
            camera.return_value.__exit__.assert_called_once()
        self.assertEqual(self.manager.camera_check["model"], "Test Sony")
        self.assertTrue((self.manager.check_output / "preview.jpg").exists())

    def test_failures_release_session_and_do_not_expose_credentials(self):
        with patch("ui.run_session", side_effect=RuntimeError("Camera unavailable")):
            self.manager.start({"request": "scene"})
            self.manager.close()
        state = self.manager.state()
        self.assertIsNone(state["operation"])
        self.assertEqual(state["error"], "Camera unavailable")
        self.assertNotIn("test-only-secret", json.dumps(state))


class HttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = SessionManager(self.root)
        self.server = LocalServer(("127.0.0.1", 0), self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_local_page_and_state_work_without_external_assets(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/app.js", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertEqual(state["token"], self.manager.token)
        self.assertNotIn("api_key", state)

    def test_untrusted_hosts_origins_and_missing_tokens_cannot_control_camera(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
                        {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("GET", "/api/state", headers=headers)[0], 403)
        with patch.object(self.manager, "start") as start:
            self.assertEqual(self.request("POST", "/api/start", "{}", {"Content-Type": "application/json"})[0], 403)
            start.assert_not_called()

    def test_authorized_post_validates_json_and_calls_control_once(self):
        headers = {"X-WaitLess-Token": self.manager.token, "Content-Type": "application/json"}
        with patch.object(self.manager, "start") as start:
            self.assertEqual(self.request("POST", "/api/start", '{"request":"scene"}', headers)[0], 202)
            start.assert_called_once_with({"request": "scene"})
        self.assertEqual(self.request("POST", "/api/start", "invalid JSON", headers)[0], 400)

    def test_media_only_exposes_recorded_previews_and_successful_originals(self):
        output = self.root / "session"
        (output / "previews").mkdir(parents=True)
        (output / "moments/00000").mkdir(parents=True)
        (output / "previews/000000.jpg").write_bytes(b"preview")
        (output / "moments/00000/photo.arw").write_bytes(b"original")
        (output / "private.txt").write_text("private")
        (self.root / "outside.jpg").write_bytes(b"outside")
        (output / "previews/escape.jpg").symlink_to(self.root / "outside.jpg")
        report = {"observations": [{"preview_file": "previews/000000.jpg", "capture_status": "downloaded",
                                     "saved_files": ["moments/00000/photo.arw"]},
                                    {"preview_file": "previews/escape.jpg"}]}
        (output / "results.json").write_text(json.dumps(report))
        self.manager.output = output
        self.assertEqual(self.request("GET", "/media/session/previews/000000.jpg")[2], b"preview")
        status, headers, body = self.request("GET", "/media/session/moments/00000/photo.arw")
        self.assertEqual((status, body), (200, b"original"))
        self.assertIn("attachment", headers["Content-Disposition"])
        for path in ("private.txt", "../outside.jpg", "previews/escape.jpg", "results.json"):
            self.assertEqual(self.request("GET", "/media/session/" + path)[0], 404)
        report["observations"][0]["capture_status"] = "failed_after_shutter"
        (output / "results.json").write_text(json.dumps(report))
        self.assertEqual(self.request("GET", "/media/session/moments/00000/photo.arw")[0], 404)


if __name__ == "__main__":
    unittest.main()
