"""Offline checks for live timing, capture accounting, and camera cleanup."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from camera import CameraFrame, SonyCamera
import futurelens as fl
from model import ApiConfig


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeCamera:
    model = "ILCE-7M4"

    def __init__(self, clock, output):
        self.clock, self.output = clock, output
        self.captures = []
        self.frames = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def latest_frame(self):
        self.frames += 1
        return CameraFrame(Image.new("RGB", (160, 96), "green"), self.clock.now, self.frames)

    def capture(self, index, on_shutter, max_delay):
        self.captures.append(index)
        on_shutter()
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
        for target, kwargs in [
            ("futurelens.SonyCamera", {"return_value": self.camera}),
            ("futurelens.load_config", {"return_value": ApiConfig(api_key="offline-test")}),
            ("futurelens.time.monotonic", {"side_effect": self.clock.monotonic}),
            ("futurelens.time.sleep", {"side_effect": self.clock.sleep}),
        ]:
            mock = patch(target, **kwargs)
            active = mock.start()
            self.addCleanup(mock.stop)
            if target == "futurelens.SonyCamera":
                self.factory = active

    def args(self, *extra):
        return fl.parse_args(["--request", "green scene",
                              "--output", str(self.output), "--duration", "10",
                              "--max-requests", "5", "--max-shots", "3", "--cooldown", "0", *extra])

    def report(self):
        return json.loads((self.output / "results.json").read_text())

    def test_capture_uses_camera_photo_and_rearms_only_after_false(self):
        with patch("futurelens.decide", side_effect=[False, True, True, False, True]):
            fl.run(self.args())
        self.assertEqual(self.camera.captures, [1, 4])
        report = self.report()
        self.assertEqual(report["photos_taken"], 2)
        entry = report["decisions"][1]
        self.assertEqual(entry["capture_status"], "downloaded")
        self.assertIn("shutter_sent_at", entry)
        with Image.open(self.output / entry["saved_files"][0]) as image:
            self.assertEqual(image.size, (640, 480))
        self.assertTrue(self.camera.closed)

    def test_dry_run_disables_shutter_and_never_calls_model(self):
        with patch("futurelens.load_config") as load_config, patch("futurelens.decide") as decide:
            fl.run(self.args("--dry-run"))
        load_config.assert_not_called()
        decide.assert_not_called()
        self.assertFalse(self.factory.call_args.kwargs["enable_capture"])
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(len(self.report()["decisions"]), 5)
        self.assertEqual(self.report()["status"], "dry_run")
        self.assertTrue(all("shot" not in entry for entry in self.report()["decisions"]))

    def test_false_saves_nothing(self):
        with patch("futurelens.decide", return_value=False):
            fl.run(self.args())
        self.assertEqual(self.report()["status"], "completed")
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(list((self.output / "moments").iterdir()), [])
        self.assertEqual([entry["timestamp_s"] for entry in self.report()["decisions"]], [0, 1, 2, 3, 4])

    def test_api_failure_is_not_a_false_decision(self):
        with patch("futurelens.decide", side_effect=RuntimeError("connection failed")):
            with self.assertRaises(RuntimeError):
                fl.run(self.args())
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("shot", report["decisions"][0])
        self.assertEqual(self.camera.captures, [])
        self.assertTrue(self.camera.closed)

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        (self.output / "keep.txt").write_text("existing user output")
        with self.assertRaises(FileExistsError):
            fl.run(self.args("--dry-run"))
        self.assertEqual((self.output / "keep.txt").read_text(), "existing user output")
        self.factory.assert_not_called()

    def test_stale_true_is_skipped_without_consuming_trigger(self):
        def decide(*_):
            if self.camera.frames == 1:
                self.clock.sleep(4)
            return True
        with patch("futurelens.decide", side_effect=decide):
            fl.run(self.args("--max-shots", "1"))
        self.assertEqual(self.camera.captures, [1])
        self.assertEqual(self.report()["decisions"][0]["capture_status"], "skipped_stale_frame")

    def test_late_model_response_cannot_shoot_after_duration(self):
        def decide(*_):
            self.clock.sleep(11)
            return True
        with patch("futurelens.decide", side_effect=decide):
            fl.run(self.args("--max-frame-age", "30"))
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["decisions"][0]["capture_status"], "skipped_duration_expired")

    def test_cooldown_applies_after_rearming(self):
        with patch("futurelens.decide", side_effect=[True, False, True, True, True]):
            fl.run(self.args("--cooldown", "3"))
        self.assertEqual(self.camera.captures, [0, 3])
        self.assertEqual(self.report()["decisions"][2]["capture_status"], "skipped_cooldown")

    def test_max_shots_stops_before_another_model_request(self):
        with patch("futurelens.decide", return_value=True) as decide:
            fl.run(self.args("--max-shots", "1"))
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(self.camera.frames, 1)

    def test_download_failure_is_recorded_and_never_retried(self):
        def failed_capture(index, on_shutter, max_delay):
            on_shutter()
            raise RuntimeError("Disconnected while downloading")
        with patch.object(self.camera, "capture", side_effect=failed_capture) as capture:
            with patch("futurelens.decide", return_value=True), self.assertRaises(RuntimeError):
                fl.run(self.args())
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report()["decisions"][0]["capture_status"], "failed_after_shutter")
        self.assertTrue(self.camera.closed)

    def test_interrupt_closes_camera(self):
        with patch("futurelens.decide", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            fl.run(self.args())
        self.assertEqual(self.report()["status"], "interrupted")
        self.assertEqual(self.camera.captures, [])
        self.assertTrue(self.camera.closed)

    def test_adapter_rejects_missing_download_and_disabled_capture(self):
        camera = SonyCamera(self.output)
        with self.assertRaises(RuntimeError):
            camera.capture(0)
        camera.enable_capture = True
        with patch.object(camera, "_send"), patch.object(camera, "_receive", side_effect=[
            {"event": "shutter_sent"}, {"event": "captured", "files": [str(self.output / "00000/missing.jpg")]},
        ]), self.assertRaises(RuntimeError):
            camera.capture(0)


if __name__ == "__main__":
    unittest.main()
