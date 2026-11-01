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
from test_intent import draft, plan, review_reply, bind_snapshot
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

    def start_data(self, request="scene"):
        self.manager.intent_output = self.root / "review"
        self.manager._save_intent(draft(self.manager.intent_output, request))
        return {"intent_id": "review", "revision": 1}

    def test_settings_validate_before_camera_or_api_work(self):
        settings = session_settings({"request": "scene"})
        self.assertEqual((settings.request, settings.max_frame_age, settings.cooldown), ("scene", 30, 5))
        for settings in ({"request": ""}, {"request": 1}, {"request": "x", "interval": 45},
                         {"request": "x", "preview_mode": "fixed"}, {"request": "x", "preview_mode": "auto"},
                         {"request": "x", "duration_minutes": 60}, {"request": "x", "duration": 3600},
                         {"request": "x", "max_shots": 1}, {"request": "x", "max_requests": 360},
                         {"request": "x", "output": "/tmp/x"},
                         {"request": "x", "dry_run": True}, {"request": "x", "camera_helper": "other"}, []):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.manager.start(settings)
        self.assertIsNone(self.manager.operation)
        self.assertFalse(self.manager.output_root.exists())

    def test_ui_settings_drive_real_engine_and_saved_photo_report(self):
        clock = Clock()
        self.manager.camera_check = {"error": "Earlier connection check failed"}
        waiting = threading.Event()
        from waitless import write_json
        def publish(path, report):
            write_json(path, report)
            if report.get("phase") == "waiting":
                waiting.set()
        def camera(output, **kwargs):
            self.assertTrue(kwargs["enable_capture"])
            self.assertEqual(kwargs["helper"], self.helper)
            return FakeCamera(clock, output.parent)
        with patch("waitless.SonyCamera", side_effect=camera), \
                patch("waitless.load_config", return_value=ApiConfig(api_key="offline-test")), \
                patch("waitless.count_tokens", return_value=1000), \
                patch("waitless.call_model", side_effect=lambda payload, *_a, **_k: bind_snapshot(answer("shoot"), payload)) as inference, \
                patch("waitless.write_json", side_effect=publish), \
                patch("waitless.time.monotonic", side_effect=clock.monotonic):
            self.manager.start(self.start_data("Capture this scene"))
            self.assertTrue(waiting.wait(2))
            self.assertEqual(self.manager.operation, "monitor")
            self.manager.stop()
            self.manager.worker.join(2)
            self.assertFalse(self.manager.worker.is_alive())
        state = self.manager.state()
        self.assertIsNone(state["error"])
        self.assertIsNone(state["camera_check"])
        report = state["report"]
        self.assertEqual(report["current_interval_s"], 60)
        self.assertEqual(report["settings"], {"cooldown": 5, "max_frame_age": 30})
        self.assertEqual((report["status"], report["stop_reason"], report["photos_taken"]),
                         ("stopped", "user_stop", 1))
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
        def runner(settings, output, *, approved_intent, camera_helper, stop_event):
            entered.set()
            self.assertTrue(stop_event.wait(2))
        with patch("ui.run_session", side_effect=runner) as run:
            self.manager.start(self.start_data())
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

    def test_start_configuration_errors_do_not_launch_camera_work(self):
        with patch("ui.load_config", return_value=ApiConfig()), patch("ui.run_session") as run:
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
                self.manager.start(self.start_data())
            run.assert_not_called()
        self.helper.unlink()
        with patch("ui.run_session") as run:
            with self.assertRaisesRegex(ValueError, "runtime is missing"):
                self.manager.start(self.start_data())
            run.assert_not_called()
        self.assertIsNone(self.manager.operation)
        self.assertIsNone(self.manager.output)

    def test_failures_release_session_and_do_not_expose_credentials(self):
        with patch("ui.run_session", side_effect=RuntimeError("Camera unavailable")):
            self.manager.start(self.start_data())
            self.manager.close()
        state = self.manager.state()
        self.assertIsNone(state["operation"])
        self.assertEqual(state["error"], "Camera unavailable")
        self.assertNotIn("test-only-secret", json.dumps(state))

    def test_review_preparation_releases_preview_only_camera_before_inference(self):
        with patch("ui.SonyCamera") as camera, patch("ui.review_intent") as propose:
            device = camera.return_value.__enter__.return_value
            device.model = "Test Sony"
            device.capabilities = {"capture_ready": True}
            device.latest_frame.return_value.image = Image.new("RGB", (1920, 1080))
            def interpretation(*args, **kwargs):
                camera.return_value.__exit__.assert_called_once()
                device.capture.assert_not_called()
                self.assertFalse(camera.call_args.kwargs["enable_capture"])
                return {**plan(), **review_reply()}
            propose.side_effect = interpretation
            self.manager.prepare_intent({"request": "The smaller peak"})
            self.manager.worker.join(2)
        state = self.manager.state()
        self.assertEqual(state["intent"]["status"], "ready")
        self.assertIsNone(state["session_id"])
        self.assertEqual((state["intent"]["width"], state["intent"]["height"]), (960, 540))
        self.assertTrue(self.manager.media_file(state["intent"]["id"], "reference.jpg").exists())
        self.assertEqual(json.loads((self.manager.intent_output / "intent.json").read_text()), state["intent"])
        self.assertEqual([m["role"] for m in state["intent"]["conversation"]], ["user", "assistant"])
        self.assertIsNone(state["intent"]["pending_message_id"])

    def test_direct_start_stale_approval_and_approval_reuse_are_rejected(self):
        with patch("ui.run_session") as run:
            with self.assertRaises(ValueError):
                self.manager.start({"request": "scene"})
            identity = self.start_data()
            changed = review_reply("Photograph the smaller peak when clouds clear")
            changed["targets"][0].update(label="The smaller peak", box=[.1, .1, .3, .4])
            with patch("ui.review_intent", return_value=changed):
                self.manager.refine_intent({**identity, "feedback": "I mean the smaller peak."})
                self.manager.worker.join(2)
            for revision in (1, 2):
                with self.assertRaises(ValueError):
                    self.manager.start({**identity, "revision": revision})
            run.assert_not_called()
            self.manager.start({**identity, "revision": 3})
            self.manager.worker.join(2)
            self.assertEqual(run.call_args.kwargs["approved_intent"].data["targets"], changed["targets"])
            with self.assertRaises(ValueError):
                self.manager.start({**identity, "revision": 3})
            run.assert_called_once()

    def test_verbal_correction_replaces_ai_highlights_on_same_reference(self):
        identity = self.start_data()
        changed = review_reply("Only the smaller peak")
        changed["targets"][0].update(label="Smaller peak", box=[.1, .2, .4, .6])
        with patch("ui.SonyCamera") as camera, patch("ui.review_intent", return_value=changed) as explain:
            self.manager.refine_intent({**identity, "feedback": "I mean the smaller peak on the left."})
            self.manager.worker.join(2)
            camera.assert_not_called()
            self.assertEqual(explain.call_args.kwargs["previous"]["targets"], plan()["targets"])
            self.assertFalse(explain.call_args.kwargs["reference_changed"])
        current = self.manager.intent
        self.assertEqual(current["status"], "ready")
        self.assertEqual(current["revision"], 3)
        self.assertEqual(current["condition"], "Only the smaller peak")
        self.assertEqual(current["targets"], changed["targets"])
        self.assertFalse(current["condition_stale"])
        self.assertNotIn("approved_at", current)
        self.assertNotIn("targets", current["conversation"][-2])
        self.assertEqual(current["conversation"][-1]["targets"], changed["targets"])

    def test_text_only_review_clears_highlights_and_can_continue_and_start(self):
        identity = self.start_data("Photograph a bird when it arrives")
        explanation = {**review_reply("Take one photo when a bird enters the frame."), "targets": []}
        with patch("ui.review_intent", return_value=explanation) as review, patch("ui.run_session") as run:
            self.manager.refine_intent({**identity, "feedback": "The bird is not visible yet."})
            self.manager.worker.join(2)
            self.assertEqual(self.manager.intent["targets"], [])
            self.assertEqual(self.manager.intent["question"], "")
            self.assertEqual(self.manager.intent["status"], "ready")
            self.manager.refine_intent({**identity, "revision": 3, "feedback": "Yes, only once."})
            self.manager.worker.join(2)
            self.assertEqual(review.call_args.kwargs["previous"]["targets"], [])
            self.assertFalse(review.call_args.kwargs["reference_changed"])
            self.manager.start({**identity, "revision": 5})
            self.manager.worker.join(2)
            self.assertEqual(run.call_args.kwargs["approved_intent"].data["targets"], [])

    def test_malformed_ai_highlights_cannot_replace_last_review_or_enable_capture(self):
        identity = self.start_data()
        bad = review_reply()
        bad["targets"][0]["box"] = None
        with patch("ui.review_intent", return_value=bad), patch("ui.run_session") as run:
            self.manager.refine_intent({**identity, "feedback": "Explain the missing subject in words."})
            self.manager.worker.join(2)
            self.assertEqual(self.manager.intent["targets"], plan()["targets"])
            self.assertTrue(self.manager.intent["condition_stale"])
            self.assertIsNotNone(self.manager.intent["error"])
            with self.assertRaises(ValueError):
                self.manager.start({**identity, "revision": 2})
            run.assert_not_called()

    def test_cancel_discards_inflight_result_and_retains_exclusive_ownership(self):
        identity = self.start_data()
        entered, finish = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set(); finish.wait(2); return review_reply("An explanation that was cancelled")
        with patch("ui.review_intent", side_effect=slow):
            self.manager.refine_intent({**identity, "feedback": "Different peak"})
            self.assertTrue(entered.wait(1))
            self.manager.cancel_intent({**identity, "revision": 2})
            with self.assertRaises(ValueError):
                self.manager.prepare_intent({"request": "new"})
            finish.set(); self.manager.worker.join(2)
        self.assertEqual(self.manager.intent["status"], "cancelled")
        self.assertIsNone(self.manager.operation)
        self.assertEqual(self.manager.intent["conversation"][-1]["role"], "user")

    def test_failed_refinement_preserves_last_review_and_blocks_approval(self):
        identity = self.start_data()
        with patch("ui.review_intent", side_effect=ValueError("Invalid interpretation")):
            self.manager.refine_intent({**identity, "feedback": "Try again"})
            self.manager.worker.join(2)
        self.assertEqual(self.manager.intent["targets"], plan()["targets"])
        self.assertEqual(self.manager.intent["status"], "ready")
        self.assertIn("Invalid interpretation", self.manager.intent["error"])
        self.assertTrue(self.manager.intent["condition_stale"])
        with patch("ui.run_session") as run, self.assertRaises(ValueError):
            self.manager.start({**identity, "revision": 2})
        run.assert_not_called()

    def test_conversation_keeps_answers_and_blocks_unresolved_or_stale_approval(self):
        identity = self.start_data("Photograph the smaller peak when clouds clear")
        replies = [review_reply("Wait until the smaller peak clears; photo count unresolved", "Take just one photo?"),
                   review_reply("Take one photo when the smaller peak is fully clear"),
                   review_reply("Take one photo when the smaller peak is fully clear")]
        with patch("ui.SonyCamera") as camera, patch("ui.run_session") as run, patch("ui.review_intent", side_effect=replies) as explain:
            self.manager.refine_intent({**identity, "feedback": "Only after it is fully clear"})
            self.manager.worker.join(2)
            self.assertEqual(self.manager.intent["question"], "Take just one photo?")
            with self.assertRaises(ValueError):
                self.manager.start({**identity, "revision": 3})
            with self.assertRaises(ValueError):
                self.manager.refine_intent({**identity, "revision": 3, "feedback": ""})
            run.assert_not_called()
            self.manager.refine_intent({**identity, "revision": 3, "feedback": "Yes"})
            self.manager.worker.join(2)
            self.assertEqual(explain.call_args.kwargs["previous"]["question"], "Take just one photo?")
            self.assertEqual(explain.call_args.kwargs["conversation"][-2]["question"], "Take just one photo?")
            self.assertEqual(explain.call_args.kwargs["conversation"][-1]["text"], "Yes")
            self.manager.refine_intent({**identity, "revision": 5, "feedback": "What if a little cloud remains?"})
            self.manager.worker.join(2)
            restored = self.manager.state()["intent"]
            self.assertEqual(len(restored["conversation"]), 8)
            self.assertEqual(json.loads((self.manager.intent_output / "intent.json").read_text())["conversation"], restored["conversation"])
            self.assertIn("Yes", [m["text"] for m in explain.call_args.kwargs["conversation"]])
            with self.assertRaises(ValueError):
                self.manager.start({**identity, "revision": 5})
            camera.assert_not_called()
            run.assert_not_called()
            self.manager.start({**identity, "revision": 7})
            self.manager.worker.join(2)
            self.assertEqual(run.call_args.kwargs["approved_intent"].data["condition"], replies[-1]["condition"])

    def test_failed_reply_retries_same_message_without_losing_the_exchange(self):
        identity = self.start_data()
        with patch("ui.review_intent", side_effect=[ValueError("Reply unavailable"), review_reply("Capture once")]) as explain:
            self.manager.refine_intent({**identity, "feedback": "Only once"})
            self.manager.worker.join(2)
            pending = self.manager.intent["pending_message_id"]
            messages = self.manager.state()["intent"]["conversation"]
            self.assertEqual(messages[-1]["id"], pending)
            self.manager.refine_intent({**identity, "revision": 2, "feedback": ""})
            self.manager.worker.join(2)
            self.assertEqual(explain.call_args.kwargs["conversation"], messages)
            self.assertEqual(len(self.manager.intent["conversation"]), len(messages) + 1)
            self.assertIsNone(self.manager.intent["pending_message_id"])
            self.assertFalse(self.manager.intent["condition_stale"])

    def test_refresh_retains_discussion_but_proposes_locations_on_new_reference(self):
        identity = self.start_data("Photograph the peak")
        with patch("ui.review_intent", return_value=review_reply("Only one photo when fully clear")):
            self.manager.refine_intent({**identity, "feedback": "Only once, and fully clear"})
            self.manager.worker.join(2)
        old = self.manager.state()["intent"]
        proposal = {**plan(), **review_reply(old["condition"])}
        proposal["targets"][0]["box"] = [.1, .2, .5, .6]
        with patch("ui.SonyCamera") as camera, patch("ui.review_intent", return_value=proposal) as propose:
            device = camera.return_value.__enter__.return_value
            device.model, device.capabilities = "Test", {}
            device.latest_frame.return_value.image = Image.new("RGB", (960, 540))
            with self.assertRaises(ValueError):
                self.manager.prepare_intent(identity)
            camera.assert_not_called()
            self.manager.prepare_intent({**identity, "revision": 3})
            self.manager.worker.join(2)
            device.capture.assert_not_called()
            self.assertFalse(camera.call_args.kwargs["enable_capture"])
            self.assertEqual(propose.call_args.kwargs["previous"]["condition"], old["condition"])
            self.assertEqual(propose.call_args.kwargs["conversation"][:-1], old["conversation"])
        self.assertNotEqual(self.manager.intent["id"], old["id"])
        self.assertEqual(self.manager.intent["condition"], old["condition"])
        self.assertEqual(self.manager.intent["targets"], proposal["targets"])
        self.assertEqual(len(self.manager.intent["conversation"]), len(old["conversation"]) + 2)


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
        status, headers, body = self.request("GET", "/activity.mjs")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn(b"export function sessionActivity", body)
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
        with patch("ui.run_session") as run:
            for removed in ({"interval": 45}, {"preview_mode": "fixed"}, {"preview_mode": "auto"},
                            {"duration_minutes": 60}, {"max_shots": 1}, {"max_requests": 360}):
                with self.subTest(removed=removed):
                    body = json.dumps({"request": "scene", **removed})
                    self.assertEqual(self.request("POST", "/api/start", body, headers)[0], 400)
            run.assert_not_called()

    def test_intent_http_routes_enforce_tokens_and_revisions(self):
        headers = {"X-WaitLess-Token": self.manager.token, "Content-Type": "application/json"}
        self.manager.intent_output = self.root / "review"
        self.manager._save_intent(draft(self.manager.intent_output))
        for route in ("prepare", "update", "refine", "cancel"):
            self.assertEqual(self.request("POST", f"/api/intent/{route}", "{}", {"Content-Type": "application/json"})[0], 403)
        identity = {"intent_id": "review", "revision": 1}
        changed = review_reply("Capture only when the scene is empty")
        changed["targets"] = []
        # Direct geometry/condition mutation is no longer exposed.
        self.assertEqual(self.request("POST", "/api/intent/update", json.dumps({**identity, "plan": plan()}), headers)[0], 404)
        with patch("ui.load_config", return_value=ApiConfig(api_key="offline")), patch("ui.review_intent", return_value=changed):
            status, _, body = self.request("POST", "/api/intent/refine", json.dumps({**identity, "feedback": "Wait for an empty scene"}), headers)
            self.assertEqual(status, 202)
            self.manager.worker.join(2)
        self.assertEqual(self.request("POST", "/api/start", json.dumps(identity), headers)[0], 400)
        restored = json.loads(self.request("GET", "/api/state")[2])["intent"]
        self.assertEqual(restored["targets"], [])
        self.assertEqual(restored["condition"], changed["condition"])
        self.assertEqual(restored["conversation"][-1]["targets"], [])
        self.assertFalse(restored["condition_stale"])
        self.assertEqual(self.request("GET", "/media/review/reference.jpg")[0], 200)
        self.assertEqual(self.request("GET", "/media/review/intent.json")[0], 404)
        self.assertEqual(self.request("POST", "/api/intent/cancel", json.dumps({**identity, "revision": 3}), headers)[0], 202)
        self.assertEqual(self.manager.intent["status"], "cancelled")

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
