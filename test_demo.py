"""Real video decoding with a simulated playback clock and model."""

from fractions import Fraction
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import av
from PIL import Image

from demo import DemoVideo, VideoCamera, VideoEnded
from model import ApiConfig
from test_camera import Clock
from test_intent import approval, bind_snapshot
from test_model import answer
from waitless import SessionSettings, run_session


def make_video(path, seconds=4, rate=4, rotation=0):
    with av.open(str(path), "w", format="mp4") as output:
        stream = output.add_stream("mpeg4", rate=rate)
        stream.width, stream.height, stream.pix_fmt = 160, 96, "yuv420p"
        stream.codec_context.gop_size = rate
        if rotation:
            stream.set_display_rotation(rotation)
        for index in range(seconds * rate):
            color = ["red", "lime", "blue", "white"][int(index / rate) % 4]
            frame = av.VideoFrame.from_image(Image.new("RGB", (160, 96), color))
            frame.pts, frame.time_base = index, Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "input.mp4"
        make_video(self.path)
        self.video = DemoVideo.inspect(self.path, "Demo.mp4")
        self.clock = Clock()
        timing = patch("demo.time.monotonic", side_effect=self.clock.monotonic)
        timing.start()
        self.addCleanup(timing.stop)

    def test_preview_stays_at_start_and_cannot_capture(self):
        with VideoCamera(self.video, self.root / "photos") as camera:
            self.clock.sleep(100)
            frame = camera.latest_frame()
            self.assertGreater(frame.image.getpixel((20, 20))[0], 240)
            self.assertEqual(camera.last_frame_position, 0)
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                camera.capture(0)
        self.assertFalse((self.root / "photos").exists())

    def test_playback_uses_elapsed_time_and_shutter_saves_current_full_frame(self):
        with VideoCamera(self.video, self.root / "photos", enable_capture=True) as camera:
            self.clock.sleep(1.6)
            frame = camera.latest_frame()
            self.assertGreater(frame.image.getpixel((20, 20))[1], 240)
            self.assertAlmostEqual(camera.last_frame_position, 1.5)
            self.clock.sleep(1)
            calls = []
            files = camera.capture(0, on_shutter=lambda: calls.append("shutter"))
            with Image.open(files[0]) as captured:
                self.assertEqual(captured.size, (160, 96))
                self.assertGreater(captured.getpixel((20, 20))[2], 240)
            self.assertEqual(calls, ["shutter"])
            self.assertAlmostEqual(camera.last_capture_position, 2.5)
            self.clock.sleep(1.4)
            with self.assertRaises(VideoEnded):
                camera.latest_frame()
            with self.assertRaises(VideoEnded):
                camera.capture(1)
            self.assertFalse((self.root / "photos/00001").exists())

    def test_rotated_video_matches_display_orientation(self):
        path = self.root / "portrait.mp4"
        make_video(path, rotation=90)
        video = DemoVideo.inspect(path, "portrait.mp4")
        self.assertEqual((video.width, video.height), (96, 160))
        with VideoCamera(video, self.root / "photos") as camera:
            self.assertEqual(camera.latest_frame().image.size, (96, 160))

    def test_invalid_or_external_sources_are_rejected(self):
        path = self.root / "invalid.mp4"
        for data in (b"not a video", b"#EXTM3U\nhttp://127.0.0.1/private.mp4\n"):
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                DemoVideo.inspect(path, "invalid.mp4")

    def run_demo(self, action="shoot", delay=1.5):
        output = self.root / "session"
        def reply(payload, *_args, **_kwargs):
            self.clock.sleep(delay)
            return bind_snapshot(answer(action), payload)
        with patch("waitless.SonyCamera") as sony, \
                patch("waitless.load_config", return_value=ApiConfig(api_key="offline")), \
                patch("waitless.count_tokens", return_value=1000), \
                patch("waitless.call_model", side_effect=reply), \
                patch("waitless.time.sleep", side_effect=self.clock.sleep):
            run_session(SessionSettings("scene"), output, approved_intent=approval(self.root / "review"), video=self.video)
            sony.assert_not_called()
        return json.loads((output / "results.json").read_text())

    def test_engine_saves_demo_frame_and_stops_at_video_end(self):
        report = self.run_demo()
        self.assertEqual((report["status"], report["stop_reason"], report["mode"]), ("completed", "video_end", "demo_video"))
        self.assertEqual(report["photos_taken"], 1)
        entry = report["observations"][0]
        self.assertEqual(entry["video_position_s"], 0)
        self.assertEqual(entry["video_capture_position_s"], 1.5)
        self.assertTrue((self.root / "session" / entry["saved_files"][0]).is_file())
        self.assertAlmostEqual(self.clock.now, 104)

    def test_late_model_reply_does_not_capture_after_video_end(self):
        report = self.run_demo(delay=5)
        self.assertEqual(report["stop_reason"], "video_end")
        self.assertEqual(report["photos_taken"], 0)
        self.assertEqual(report["observations"][0]["capture_status"], "skipped_video_end")


if __name__ == "__main__":
    unittest.main()
