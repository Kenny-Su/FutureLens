"""Sequential conversation replay with a fake clock/camera and mocked API transport."""

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from camera import CameraError, CameraFrame, SonyCamera
import futurelens as fl
import model
from test_model import answer


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeCamera:
    model = "ILCE-7M4"
    capabilities = {"live_view": True, "capture_ready": True, "preview_issues": [], "capture_issues": []}

    def __init__(self, clock, output):
        self.clock, self.output = clock, output
        self.captures, self.frame_times, self.max_delays = [], [], []
        self.frames = 0
        self.frame_delay = self.capture_delay = 0
        self.closed = False
        self.colors = ["white", "black", "black"]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def latest_frame(self):
        requested = self.clock.now
        self.frame_times.append(requested)
        self.clock.sleep(self.frame_delay)
        color = self.colors[min(self.frames, len(self.colors) - 1)]
        self.frames += 1
        return CameraFrame(Image.new("RGB", (160, 96), color), requested, self.frames)

    def capture(self, index, on_shutter, max_delay):
        self.captures.append(index)
        self.max_delays.append(max_delay)
        on_shutter()
        self.clock.sleep(self.capture_delay)
        file = self.output / "moments" / f"{index:05d}" / "photo.jpg"
        file.parent.mkdir()
        Image.new("RGB", (640, 480), "blue").save(file)
        return [file]


class CameraWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = (Path(temp.name) / "run").resolve()
        self.clock = Clock()
        self.camera = FakeCamera(self.clock, self.output)
        self.requests = []
        self.action = "wait"
        self.replies = None
        self.count_delay = self.inference_delay = 0
        self.tokens = 1000
        for target, kwargs in [
            ("futurelens.SonyCamera", {"return_value": self.camera}),
            ("futurelens.load_config", {"return_value": model.ApiConfig(api_key="offline-test")}),
            ("model._post", {"side_effect": self.api}),
            ("futurelens.time.monotonic", {"side_effect": self.clock.monotonic}),
            ("futurelens.time.sleep", {"side_effect": self.clock.sleep}),
            ("sys.stdout", {"new_callable": io.StringIO}),
            ("sys.stderr", {"new_callable": io.StringIO}),
        ]:
            patched = patch(target, **kwargs)
            active = patched.start()
            self.addCleanup(patched.stop)
            if target == "futurelens.SonyCamera":
                self.factory = active

    def api(self, endpoint, payload, config, timeout):
        self.requests.append((endpoint, deepcopy(payload), timeout))
        if endpoint == model.TOKEN_COUNT_ENDPOINT:
            self.clock.sleep(self.count_delay)
            return {"input_tokens": self.tokens}
        self.clock.sleep(self.inference_delay)
        if self.replies is not None:
            reply = next(self.replies)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return answer(self.action)

    def args(self, *extra):
        return fl.parse_args(["--request", "Take a photo when white becomes black; do not repeat a captured change",
                              "--output", str(self.output), "--duration", "180", "--max-requests", "3",
                              "--max-shots", "3", "--cooldown", "0", *extra])

    def report(self):
        return json.loads((self.output / "results.json").read_text())

    def journal(self):
        return [json.loads(line) for line in (self.output / "conversation.jsonl").read_text().splitlines()]

    def inferences(self):
        return [payload for endpoint, payload, _ in self.requests if endpoint == model.API_ENDPOINT]

    def test_defaults_and_removed_options(self):
        args = fl.parse_args(["--request", "landscape"])
        self.assertEqual((args.interval, args.duration, args.max_requests, args.max_shots, args.max_frame_age),
                         (60, 3600, 60, 1, 30))
        for option, value in (("--fps", "1"), ("--max-agent-turns", "6"), ("--max-context-images", "24"),
                              ("--interval", "0"), ("--interval", "nan"), ("--interval", "inf")):
            with self.subTest(option=option, value=value), self.assertRaises(SystemExit):
                fl.parse_args(["--request", "scene", option, value])

    def test_first_observation_is_immediate_and_chat_keeps_every_turn(self):
        fl.run(self.args())
        self.assertEqual(self.camera.frame_times, [100, 160, 220])
        payloads = self.inferences()
        self.assertEqual([len(p["messages"]) for p in payloads], [1, 3, 5])
        self.assertEqual([sum(b["type"] == "image" for m in p["messages"] for b in m["content"])
                          for p in payloads], [1, 2, 3])
        final = payloads[-1]["messages"]
        self.assertEqual([m["role"] for m in final], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(sum('"user_request"' in b.get("text", "") for m in final for b in m["content"]), 1)
        self.assertEqual(final[:3], payloads[1]["messages"])
        report = self.report()
        self.assertEqual((report["previews_saved"], report["api_requests"], report["token_count_requests"]), (3, 3, 3))
        self.assertEqual(report["status"], "completed")
        self.assertTrue(self.camera.closed)
        self.assertEqual(len(self.journal()), 8)  # System, three exchanges, final host outcome.
        self.assertNotIn("base64", (self.output / "conversation.jsonl").read_text())

    def test_changed_then_unchanged_replay_includes_successful_capture_in_chat(self):
        self.replies = iter([answer("wait"), answer("shoot"), answer("wait")])
        fl.run(self.args())
        self.assertEqual(self.camera.captures, [0])
        last = self.inferences()[-1]["messages"][-1]["content"]
        outcome = json.loads(last[0]["text"])["capture_outcome"]
        self.assertEqual(outcome["capture_status"], "downloaded")
        self.assertEqual(outcome["frame_id"], 1)
        self.assertTrue(outcome["saved_files"])
        self.assertEqual(self.report()["photos_taken"], 1)
        self.assertEqual(json.loads((self.output / "captures.jsonl").read_text())["trigger_frame_id"], 1)
        with Image.open(self.output / outcome["saved_files"][0]) as image:
            self.assertEqual(image.size, (640, 480))

    def test_processing_time_does_not_drift_the_minute_schedule(self):
        self.camera.frame_delay, self.count_delay, self.inference_delay = 5, 2, 6
        fl.run(self.args())
        self.assertEqual(self.camera.frame_times, [100, 160, 220])

    def test_capture_overrun_skips_missed_slots_without_bursting(self):
        self.camera.capture_delay = 75
        self.replies = iter([answer("shoot"), answer("wait"), answer("wait")])
        fl.run(self.args("--duration", "240"))
        self.assertEqual(self.camera.frame_times, [100, 220, 280])

    def test_finishing_on_a_tick_can_observe_on_that_tick(self):
        self.inference_delay = 60
        fl.run(self.args())
        self.assertEqual(self.camera.frame_times, [100, 160, 220])

    def test_dry_run_only_archives_at_the_interval(self):
        with patch("futurelens.load_config") as config:
            fl.run(self.args("--dry-run", "--max-requests", "1"))
        config.assert_not_called()
        self.assertFalse(self.factory.call_args.kwargs["enable_capture"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.camera.frame_times, [100, 160, 220])
        self.assertEqual(self.report()["status"], "dry_run")
        self.assertFalse((self.output / "conversation.jsonl").exists())

    def test_request_budget_counts_inference_only_and_stops_without_waiting(self):
        fl.run(self.args("--max-requests", "2"))
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.report()["api_requests"], 2)
        self.assertEqual(self.report()["stop_reason"], "max_requests")
        self.assertEqual(self.clock.now, 160)

    def test_duration_expiring_between_ticks_stops_without_sampling(self):
        fl.run(self.args("--duration", "30"))
        self.assertEqual(self.camera.frames, 1)
        self.assertEqual(self.clock.now, 130)
        self.assertEqual(self.report()["stop_reason"], "duration")

    def test_duration_expiring_during_preview_retains_image_without_api_call(self):
        self.camera.frame_delay = 31
        fl.run(self.args("--duration", "30"))
        self.assertEqual(self.report()["previews_saved"], 1)
        self.assertEqual(self.requests, [])
        self.assertTrue((self.output / "previews/000000.jpg").is_file())
        self.assertEqual(len(self.journal()), 2)

    def test_duration_expiring_during_token_count_prevents_inference(self):
        self.count_delay = 31
        fl.run(self.args("--duration", "30"))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][2], 30)
        self.assertEqual(self.report()["api_requests"], 0)
        self.assertEqual(self.camera.captures, [])

    def test_late_model_response_cannot_shoot_after_duration(self):
        self.action, self.inference_delay = "shoot", 31
        fl.run(self.args("--duration", "30"))
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["capture_status"], "skipped_duration_expired")

    def test_token_count_time_counts_toward_preview_freshness(self):
        self.action, self.count_delay = "shoot", 31
        fl.run(self.args("--max-requests", "2"))
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["capture_status"], "skipped_stale_frame")
        outcome = json.loads(self.inferences()[1]["messages"][-1]["content"][0]["text"])["capture_outcome"]
        self.assertEqual(outcome["capture_status"], "skipped_stale_frame")

    def test_expiry_is_rechecked_after_recording_requested_capture(self):
        self.action = "shoot"
        write = fl.write_json
        def delayed_write(path, report):
            if report.get("observations") and report["observations"][-1].get("capture_status") == "requested":
                self.clock.sleep(31)
            write(path, report)
        with patch("futurelens.write_json", side_effect=delayed_write):
            fl.run(self.args("--max-requests", "1"))
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["capture_status"], "skipped_expired_before_shutter")

    def test_remaining_freshness_is_passed_to_camera(self):
        self.action, self.count_delay, self.inference_delay = "shoot", 2, 3
        fl.run(self.args("--max-shots", "1"))
        self.assertEqual(self.camera.max_delays, [25])
        self.assertEqual(len(self.inferences()), 1)
        self.assertEqual(self.report()["stop_reason"], "max_shots")
        self.assertEqual(json.loads(self.journal()[-1]["content"][0]["text"])["capture_outcome"]["capture_status"], "downloaded")

    def test_cooldown_is_enforced_between_successful_captures(self):
        self.action = "shoot"
        fl.run(self.args("--cooldown", "90"))
        self.assertEqual(self.camera.captures, [0, 1])
        self.assertEqual(self.report()["observations"][1]["capture_status"], "skipped_cooldown")

    def test_image_capacity_preserves_full_chat_and_next_archived_preview(self):
        with patch("model.MAX_IMAGES", 2):
            fl.run(self.args())
        report = self.report()
        self.assertEqual(report["status"], "capacity_reached")
        self.assertEqual(report["capacity"], {"resource": "images", "used": 3, "limit": 2})
        self.assertEqual((report["previews_saved"], report["api_requests"], report["token_count_requests"]), (3, 2, 2))
        self.assertEqual(len(list((self.output / "previews").glob("*.jpg"))), 3)
        self.assertEqual(len([m for m in self.journal() if any(b["type"] == "image" for b in m["content"])]), 3)
        self.assertTrue(self.camera.closed)

    def test_request_size_capacity_stops_before_either_endpoint(self):
        with patch("model.MAX_REQUEST_BYTES", 1):
            fl.run(self.args())
        self.assertEqual(self.report()["status"], "capacity_reached")
        self.assertEqual(self.report()["capacity"]["resource"], "request_bytes")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.report()["previews_saved"], 1)
        self.assertEqual(len(self.journal()), 2)

    def test_token_capacity_stops_before_inference(self):
        self.tokens = 990001
        fl.run(self.args())
        self.assertEqual(self.report()["status"], "capacity_reached")
        self.assertEqual(self.report()["capacity"]["resource"], "input_tokens")
        self.assertEqual(self.report()["token_count_requests"], 1)
        self.assertEqual(self.report()["api_requests"], 0)
        self.assertEqual(self.camera.captures, [])

    def test_api_failure_stops_without_retry_or_wait_action(self):
        self.replies = iter([RuntimeError("connection failed")])
        with self.assertRaises(RuntimeError):
            fl.run(self.args())
        self.assertEqual(self.report()["status"], "failed")
        self.assertNotIn("action", self.report()["observations"][0])
        self.assertEqual(self.report()["api_requests"], 1)
        self.assertEqual(self.camera.captures, [])
        self.assertTrue(self.camera.closed)

    def test_failed_token_count_never_falls_back_to_inference(self):
        with patch("futurelens.count_tokens", side_effect=RuntimeError("count failed")), self.assertRaises(RuntimeError):
            fl.run(self.args())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.report()["token_count_requests"], 1)
        self.assertEqual(self.report()["status"], "failed")

    def test_invalid_action_stops_before_shutter(self):
        self.action = "maybe"
        with self.assertRaises(ValueError):
            fl.run(self.args())
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["status"], "failed")

    def test_download_failure_is_recorded_and_never_retried(self):
        self.action = "shoot"
        def failed_capture(index, on_shutter, max_delay):
            on_shutter()
            raise RuntimeError("Disconnected while downloading")
        with patch.object(self.camera, "capture", side_effect=failed_capture) as capture, self.assertRaises(RuntimeError):
            fl.run(self.args())
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.report()["photos_taken"], 0)
        self.assertEqual(self.report()["observations"][0]["capture_status"], "failed_after_shutter")
        self.assertEqual(json.loads(self.journal()[-1]["content"][0]["text"])["capture_outcome"]["capture_status"], "failed_after_shutter")
        self.assertFalse((self.output / "captures.jsonl").exists())
        self.assertTrue(self.camera.closed)

    def test_unconfirmed_shutter_is_terminal(self):
        self.action = "shoot"
        with patch.object(self.camera, "capture", side_effect=RuntimeError("no confirmation")) as capture, self.assertRaises(RuntimeError):
            fl.run(self.args())
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.report()["observations"][0]["capture_status"], "failed_or_unconfirmed")

    def test_interrupt_closes_camera(self):
        self.replies = iter([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            fl.run(self.args())
        self.assertEqual(self.report()["status"], "interrupted")
        self.assertEqual(self.camera.captures, [])
        self.assertTrue(self.camera.closed)

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        (self.output / "keep.txt").write_text("existing user output")
        with self.assertRaises(FileExistsError):
            fl.run(self.args("--dry-run"))
        self.assertEqual((self.output / "keep.txt").read_text(), "existing user output")
        self.factory.assert_not_called()

    def test_archive_failure_stops_before_api_or_shutter(self):
        with patch("history.PreviewArchive.append", side_effect=OSError("disk full")), self.assertRaises(OSError):
            fl.run(self.args())
        self.assertEqual(self.requests, [])
        self.assertTrue(self.camera.closed)
        self.assertEqual(self.report()["status"], "failed")

    def test_detected_camera_model_is_reported(self):
        self.camera.model = "ILCE-6700"
        fl.run(self.args("--max-requests", "1"))
        self.assertEqual(self.report()["source"], "sony:usb:ILCE-6700")

    def test_capability_failure_is_reported_before_any_api_call(self):
        capabilities = {"live_view": True, "capture_ready": False, "preview_issues": [],
                        "capture_issues": ["Still-photo shutter release is not advertised by this camera."]}
        self.factory.side_effect = CameraError("Capture unavailable", "ILME-FR7", capabilities)
        with self.assertRaises(CameraError):
            fl.run(self.args())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.report()["source"], "sony:usb:ILME-FR7")
        self.assertEqual(self.report()["camera_capabilities"], capabilities)

    def test_adapter_rejects_missing_download_and_disabled_capture(self):
        camera = SonyCamera(self.output)
        with self.assertRaises(RuntimeError):
            camera.capture(0)
        camera.enable_capture = True
        with patch.object(camera, "_send"), patch.object(camera, "_receive", side_effect=[
            {"event": "shutter_sent"}, {"event": "captured", "files": [str(self.output / "00000/missing.jpg")]},
        ]), self.assertRaises(RuntimeError):
            camera.capture(0)

    def test_preview_only_camera_is_accepted_without_capture(self):
        event = {"protocol": 2, "model": "ILME-FR7", "connection": "USB",
                 "capabilities": {"live_view": True, "capture_ready": False, "preview_issues": [],
                                  "capture_issues": ["Still-photo shutter is unavailable."]}}
        camera = SonyCamera(self.output)
        camera._accept_camera(event)
        self.assertEqual(camera.model, "ILME-FR7")
        camera.enable_capture = True
        with self.assertRaises(CameraError):
            camera._accept_camera(event)

    def test_unsupported_camera_never_receives_prepare(self):
        helper = self.output.parent / "fake-helper"
        helper.touch()
        event = {"event": "ready", "protocol": 2, "model": "Preview Camera", "connection": "USB",
                 "capabilities": {"live_view": True, "capture_ready": False, "preview_issues": [],
                                  "capture_issues": ["No still shutter."]}}
        camera = SonyCamera(self.output / "moments", enable_capture=True, helper=helper)
        with patch("camera.subprocess.Popen") as process, patch("camera.threading.Thread"), \
                patch.object(camera, "_receive", return_value=event), patch.object(camera, "_send") as send:
            process.return_value.poll.return_value = 0
            with self.assertRaises(CameraError):
                camera.__enter__()
            send.assert_not_called()
        self.assertIsNone(camera.process)
        self.assertIsNone(camera.log)

    def test_stale_helper_and_invalid_capabilities_fail_closed(self):
        camera = SonyCamera(self.output, enable_capture=True)
        valid = {"protocol": 2, "model": "ILCE-7RM5", "connection": "USB", "capabilities": self.camera.capabilities}
        for event in ({"model": "ILCE-7M4"}, {**valid, "connection": "IP"},
                      {**valid, "capabilities": {}},
                      {**valid, "capabilities": {**self.camera.capabilities, "capture_ready": 1}}):
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                camera._accept_camera(event)


if __name__ == "__main__":
    unittest.main()
