"""Sequential agent replay with a fake clock/camera and mocked API transport."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from camera import CameraError, CameraFrame, SonyCamera
import waitless as fl
import model
from test_model import answer, compacted, tool


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
        self.compaction_delay = 0
        self.compaction_replies = None
        self.compaction_times = []
        for target, kwargs in [
            ("waitless.SonyCamera", {"return_value": self.camera}),
            ("waitless.load_config", {"return_value": model.ApiConfig(api_key="offline-test")}),
            ("model._post", {"side_effect": self.api}),
            ("waitless.time.monotonic", {"side_effect": self.clock.monotonic}),
            ("waitless.time.sleep", {"side_effect": self.clock.sleep}),
        ]:
            patched = patch(target, **kwargs)
            active = patched.start()
            self.addCleanup(patched.stop)
            if target == "waitless.SonyCamera":
                self.factory = active

    def api(self, endpoint, payload, config, timeout):
        self.requests.append((endpoint, deepcopy(payload), timeout))
        if endpoint == model.TOKEN_COUNT_ENDPOINT:
            self.clock.sleep(self.count_delay)
            return {"input_tokens": self.tokens}
        if "compaction" in payload:
            self.compaction_times.append(self.clock.now)
            self.clock.sleep(self.compaction_delay)
            reply = next(self.compaction_replies) if self.compaction_replies is not None else compacted()
            if isinstance(reply, BaseException):
                raise reply
            return reply
        self.clock.sleep(self.inference_delay)
        if self.replies is not None:
            reply = next(self.replies)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return answer("wait") if self.action == "wait" else tool(self.action, call_id=f"call-{len(self.requests)}")

    def settings(self, **overrides):
        return fl.SessionSettings(**{
            "request": "Take a photo when white becomes black; do not repeat a captured change",
            "duration": 180, "max_requests": 3, "max_shots": 3, "cooldown": 0, "preview_mode": "fixed", **overrides,
        })

    def report(self):
        return json.loads((self.output / "results.json").read_text())

    def journal(self, frame_id=0):
        return [json.loads(line) for line in (self.output / "runs" / f"{frame_id:06d}" / "conversation.jsonl").read_text().splitlines()]

    def tool_outcome(self, frame_id=0):
        return json.loads(self.journal(frame_id)[-1]["content"][0]["content"][0]["text"])

    def inferences(self):
        return [payload for endpoint, payload, _ in self.requests
                if endpoint == model.API_ENDPOINT and "compaction" not in payload]

    def test_session_settings_defaults_and_validation(self):
        settings = fl.SessionSettings(request="landscape")
        self.assertEqual(settings.preview_mode, "auto")
        self.assertEqual((settings.interval, settings.duration, settings.max_requests,
                          settings.max_shots, settings.max_frame_age, settings.cooldown),
                         (60, 3600, 360, 1, 30, 5))
        for invalid in ({"interval": 0}, {"interval": float("nan")}, {"duration": float("inf")},
                        {"interval": True}, {"duration": 10**400}, {"max_shots": 1.5},
                        {"max_requests": False}, {"cooldown": -1}, {"max_frame_age": 0},
                        {"request": " "}, {"request": 3}, {"preview_mode": "adaptive"}, {"preview_mode": True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.settings(**invalid)

    def test_first_observation_is_immediate_and_conversation_persists(self):
        fl.run_session(self.settings(), self.output)
        self.assertEqual(self.camera.frame_times, [100, 160, 220])
        payloads = self.inferences()
        self.assertEqual([len(p["messages"]) for p in payloads], [1, 3, 5])
        self.assertEqual([model.check_capacity(p)["images"] for p in payloads], [1, 2, 3])
        self.assertEqual(payloads[2]["messages"][:3], payloads[1]["messages"])
        self.assertTrue(all('"user_request"' in p["messages"][0]["content"][0]["text"] for p in payloads))
        report = self.report()
        self.assertEqual((report["previews_saved"], report["api_requests"], report["token_count_requests"]), (3, 3, 3))
        self.assertEqual(report["status"], "completed")
        self.assertTrue(self.camera.closed)
        for frame_id in range(3):
            self.assertEqual(len(self.journal(frame_id)), 3)
        self.assertNotIn("base64", (self.output / "runs/000000/conversation.jsonl").read_text())
        self.assertTrue((self.output / "conversation.jsonl").exists())
        self.assertNotIn("base64", (self.output / "conversation.jsonl").read_text())

    def test_auto_slows_down_then_speeds_up_and_captures_with_history(self):
        self.replies = iter([
            tool("set_preview_interval", {"seconds": 300}),
            tool("get_history", {"frame_ids": [0]}, "compare"),
            tool("set_preview_interval", {"seconds": 10}, "timing"),
            tool("shoot", call_id="capture"),
        ])
        fl.run_session(self.settings(preview_mode="auto", duration=500, max_requests=4, max_shots=1), self.output)
        self.assertEqual(self.camera.frame_times, [100, 400, 410])
        self.assertEqual(self.camera.captures, [0])
        report = self.report()
        self.assertEqual((report["previews_saved"], report["api_requests"], report["token_count_requests"]), (3, 4, 4))
        self.assertEqual([entry["outcome"] for entry in report["observations"]],
                         ["preview_interval_set", "preview_interval_set", "downloaded"])
        self.assertTrue(all(entry["run_status"] == "finished" for entry in report["observations"]))
        sampling = json.loads(self.inferences()[1]["messages"][-1]["content"][0]["text"])["sampling"]
        self.assertEqual((sampling["mode"], sampling["min_interval_s"], sampling["max_interval_s"]), ("auto", 10, 300))
        self.assertEqual(sampling["current_interval_s"], 300)
        self.assertEqual(self.tool_outcome()["seconds"], 300)
        events = report["observations"][1]["events"]
        scheduled = next(e for e in events if e.get("name") == "set_preview_interval" and e["status"] == "completed")
        self.assertEqual(scheduled["seconds"], 10)
        self.assertEqual(scheduled["previous_seconds"], 300)
        self.assertEqual(report["current_interval_s"], 10)
        self.assertEqual(report["last_interval_change"], {"previous_seconds": 300, "seconds": 10, "from_frame_id": 1})

    def test_auto_interval_persists_after_plain_end_turn_and_capture(self):
        self.replies = iter([answer("wait"), tool("set_preview_interval", {"seconds": 300}),
                             answer("wait"), tool("shoot", call_id="capture"), answer("wait")])
        fl.run_session(self.settings(preview_mode="auto", interval=45, duration=1000, max_requests=5), self.output)
        self.assertEqual(self.camera.frame_times, [100, 160, 460, 760, 1060])
        self.assertEqual([entry["next_preview_interval_s"] for entry in self.report()["observations"]], [60, 300, 300, 300, 300])
        self.assertEqual(self.report()["current_interval_s"], 300)
        self.assertEqual(self.report()["last_interval_change"]["from_frame_id"], 1)
        sampling = [json.loads(p["messages"][-1]["content"][0]["text"])["sampling"] for p in self.inferences()]
        self.assertEqual([s["current_interval_s"] for s in sampling], [60, 60, 300, 300, 300])
        self.assertEqual(self.camera.captures, [0])
        self.assertEqual(self.report()["api_requests"], 5)

        # A new session starts at 60 seconds instead of inheriting the last session's setting.
        self.replies = iter([answer("wait")])
        destination = self.output / "new-session"
        fl.run_session(self.settings(preview_mode="auto", max_requests=1), destination)
        report = json.loads((destination / "results.json").read_text())
        self.assertEqual(report["current_interval_s"], 60)
        self.assertIsNone(report["last_interval_change"])
        self.assertEqual(len(self.inferences()[-1]["messages"]), 1)

    def test_auto_rejected_changes_and_run_limit_keep_current_interval(self):
        self.replies = iter([tool("set_preview_interval", {"seconds": 300}),
                             *[tool("set_preview_interval", {"seconds": 0}, f"bad-{i}") for i in range(6)],
                             answer("wait")])
        fl.run_session(self.settings(preview_mode="auto", duration=750, max_requests=8), self.output)
        self.assertEqual(self.camera.frame_times, [100, 400, 700])
        self.assertEqual(self.report()["observations"][1]["outcome"], "run_limit")
        self.assertEqual([entry["next_preview_interval_s"] for entry in self.report()["observations"]], [300, 300, 300])
        self.assertEqual(self.report()["last_interval_change"]["from_frame_id"], 0)

    def test_auto_unchanged_initial_interval_is_a_noop_without_another_inference(self):
        self.replies = iter([tool("set_preview_interval", {"seconds": 60})])
        fl.run_session(self.settings(preview_mode="auto", max_requests=1), self.output)
        report = self.report()
        self.assertEqual(report["current_interval_s"], 60)
        self.assertIsNone(report["last_interval_change"])
        self.assertEqual((report["api_requests"], report["token_count_requests"]), (1, 1))
        entry = report["observations"][0]
        self.assertEqual((entry["outcome"], entry["run_status"]), ("preview_interval_unchanged", "finished"))
        self.assertEqual(entry["next_preview_interval_s"], 60)
        result = self.tool_outcome()
        self.assertIs(result["changed"], False)
        self.assertIn("No action.", result["message"])
        self.assertFalse(self.journal()[-1]["content"][0]["is_error"])
        event = [event for event in entry["events"] if event["kind"] == "tool"][-1]
        self.assertEqual(event["status"], "skipped")
        self.assertIs(event["changed"], False)

    def test_auto_repeated_current_interval_preserves_schedule_and_last_actual_change(self):
        self.replies = iter([
            tool("set_preview_interval", {"seconds": seconds}, f"interval-{index}")
            for index, seconds in enumerate([30, 15, 15, 15, 15, 15])
        ] + [tool("shoot", call_id="capture")])
        fl.run_session(self.settings(preview_mode="auto", max_requests=7, max_shots=1), self.output)
        report = self.report()
        self.assertEqual(self.camera.frame_times, [100, 130, 145, 160, 175, 190, 205])
        self.assertEqual(report["api_requests"], 7)
        self.assertEqual(report["token_count_requests"], 7)
        self.assertEqual(report["current_interval_s"], 15)
        self.assertEqual(report["last_interval_change"], {"previous_seconds": 30, "seconds": 15, "from_frame_id": 1})
        self.assertEqual([entry["outcome"] for entry in report["observations"]],
                         ["preview_interval_set"] * 2 + ["preview_interval_unchanged"] * 4 + ["downloaded"])
        self.assertTrue(all(entry["model_calls"] == 1 for entry in report["observations"]))
        self.assertEqual(self.camera.captures, [0])
        # Feedback for each no-op is retained in the next observation's conversation.
        for frame_id in range(2, 6):
            result = self.inferences()[frame_id + 1]["messages"][-2]["content"][0]
            self.assertEqual(result["tool_use_id"], f"interval-{frame_id}")
            self.assertIs(json.loads(result["content"][0]["text"])["changed"], False)

    def test_auto_no_action_reply_keeps_monitoring_without_a_tool_call(self):
        no_action = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "No action."}]}
        self.replies = iter([tool("set_preview_interval", {"seconds": 15}), no_action, no_action])
        fl.run_session(self.settings(preview_mode="auto"), self.output)
        report = self.report()
        self.assertEqual(self.camera.frame_times, [100, 115, 130])
        self.assertEqual(report["current_interval_s"], 15)
        self.assertEqual(report["last_interval_change"]["from_frame_id"], 0)
        self.assertEqual(report["api_requests"], 3)
        for entry in report["observations"][1:]:
            self.assertEqual(entry["outcome"], "no_photo")
            self.assertFalse(any(event["kind"] == "tool" for event in entry["events"]))

    def test_auto_interval_is_measured_from_preview_not_response(self):
        self.camera.frame_delay, self.count_delay, self.inference_delay = 2, 2, 3
        self.replies = iter([tool("set_preview_interval", {"seconds": 10}), answer("wait"), answer("wait")])
        fl.run_session(self.settings(preview_mode="auto"), self.output)
        self.assertEqual(self.camera.frame_times, [100, 110, 120])

    def test_auto_overrun_observes_again_when_ready_without_catchup(self):
        self.inference_delay = 20
        self.replies = iter([tool("set_preview_interval", {"seconds": 10}), answer("wait"), answer("wait")])
        fl.run_session(self.settings(preview_mode="auto"), self.output)
        self.assertEqual(self.camera.frame_times, [100, 120, 140])
        self.assertEqual(self.report()["api_requests"], 3)

    def test_auto_invalid_schedule_is_recoverable_without_applying_bad_interval(self):
        invalid = [{"seconds": seconds} for seconds in (True, 9, 301, 10.5, "60", 10**100)]
        invalid += [{}, {"seconds": 10, "reason": "Clouds."}, {"seconds": 10, "extra": 1}]
        for index, arguments in enumerate(invalid):
            with self.subTest(arguments=arguments):
                destination = self.output / str(index)
                self.replies = iter([tool("set_preview_interval", arguments, "bad"),
                                     tool("set_preview_interval", {"seconds": 20}, "good")])
                fl.run_session(self.settings(preview_mode="auto", max_requests=2), destination)
                report = json.loads((destination / "results.json").read_text())
                self.assertEqual((report["previews_saved"], report["current_interval_s"]), (1, 20))
                entry = report["observations"][0]
                self.assertEqual(entry["outcome"], "preview_interval_set")
                self.assertTrue(any(e["kind"] == "tool" and e["status"] == "failed" for e in entry["events"]))
                transcript = (destination / "runs/000000/conversation.jsonl").read_text()
                self.assertIn('"is_error": true', transcript)

    def test_auto_stale_response_cannot_change_interval(self):
        def inference(*_, **__):
            if self.camera.frames == 1:
                return tool("set_preview_interval", {"seconds": 120})
            self.clock.sleep(31)
            return tool("set_preview_interval", {"seconds": 300}, "stale")
        with patch("waitless.call_model", side_effect=inference):
            fl.run_session(self.settings(preview_mode="auto", max_requests=2), self.output)
        self.assertEqual(self.report()["observations"][1]["outcome"], "stale_preview")
        self.assertEqual(self.report()["current_interval_s"], 120)
        self.assertEqual(self.report()["last_interval_change"]["from_frame_id"], 0)

    def test_auto_long_wait_respects_session_deadline(self):
        self.replies = iter([tool("set_preview_interval", {"seconds": 300})])
        fl.run_session(self.settings(preview_mode="auto", duration=40), self.output)
        self.assertEqual(self.camera.frame_times, [100])
        self.assertEqual(self.clock.now, 140)
        self.assertEqual(self.report()["stop_reason"], "duration")

    def test_auto_stop_during_inference_rejects_scheduling(self):
        stop = threading.Event()
        def inference(*_, **__):
            stop.set()
            return tool("set_preview_interval", {"seconds": 300})
        with patch("waitless.call_model", side_effect=inference):
            fl.run_session(self.settings(preview_mode="auto"), self.output, stop_event=stop)
        self.assertEqual(self.report()["status"], "stopped")
        self.assertEqual(self.report()["current_interval_s"], 60)
        self.assertIsNone(self.report()["last_interval_change"])

    def test_auto_stop_interrupts_long_wait(self):
        stop = threading.Event()
        self.replies = iter([tool("set_preview_interval", {"seconds": 300})])
        with patch.object(stop, "wait", side_effect=lambda delay: stop.set()) as wait:
            fl.run_session(self.settings(preview_mode="auto", duration=600), self.output, stop_event=stop)
        wait.assert_called_once_with(300)
        self.assertEqual(self.camera.frame_times, [100])
        self.assertEqual(self.report()["status"], "stopped")
        self.assertTrue(self.camera.closed)

    def test_changed_then_unchanged_replay_includes_successful_capture_in_chat(self):
        self.replies = iter([answer("wait"), answer("shoot"), answer("wait")])
        fl.run_session(self.settings(), self.output)
        self.assertEqual(self.camera.captures, [0])
        last = self.inferences()[-1]["messages"][-1]["content"]
        outcome = json.loads(last[0]["text"])["latest_capture_outcome"]
        self.assertEqual(outcome["capture_status"], "downloaded")
        self.assertEqual(outcome["frame_id"], 1)
        self.assertTrue(outcome["saved_files"])
        self.assertEqual(self.report()["photos_taken"], 1)
        self.assertEqual(json.loads((self.output / "captures.jsonl").read_text())["trigger_frame_id"], 1)
        with Image.open(self.output / outcome["saved_files"][0]) as image:
            self.assertEqual(image.size, (640, 480))

    def test_processing_time_does_not_drift_the_minute_schedule(self):
        self.camera.frame_delay, self.count_delay, self.inference_delay = 5, 2, 6
        fl.run_session(self.settings(), self.output)
        self.assertEqual(self.camera.frame_times, [100, 160, 220])

    def test_capture_overrun_skips_missed_slots_without_bursting(self):
        self.camera.capture_delay = 75
        self.replies = iter([answer("shoot"), answer("wait"), answer("wait")])
        fl.run_session(self.settings(duration=240), self.output)
        self.assertEqual(self.camera.frame_times, [100, 220, 280])

    def test_finishing_on_a_tick_can_observe_on_that_tick(self):
        self.inference_delay = 60
        fl.run_session(self.settings(), self.output)
        self.assertEqual(self.camera.frame_times, [100, 160, 220])

    def test_request_budget_counts_inference_only_and_stops_without_waiting(self):
        fl.run_session(self.settings(max_requests=2), self.output)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.report()["api_requests"], 2)
        self.assertEqual(self.report()["stop_reason"], "max_requests")
        self.assertEqual(self.clock.now, 160)

    def test_duration_expiring_between_ticks_stops_without_sampling(self):
        fl.run_session(self.settings(duration=30), self.output)
        self.assertEqual(self.camera.frames, 1)
        self.assertEqual(self.clock.now, 130)
        self.assertEqual(self.report()["stop_reason"], "duration")

    def test_duration_expiring_during_preview_retains_image_without_api_call(self):
        self.camera.frame_delay = 31
        fl.run_session(self.settings(duration=30), self.output)
        self.assertEqual(self.report()["previews_saved"], 1)
        self.assertEqual(self.requests, [])
        self.assertTrue((self.output / "previews/000000.jpg").is_file())
        self.assertEqual(len(self.journal()), 2)

    def test_duration_expiring_during_token_count_prevents_inference(self):
        self.count_delay = 31
        fl.run_session(self.settings(duration=30), self.output)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][2], 30)
        self.assertEqual(self.report()["api_requests"], 0)
        self.assertEqual(self.camera.captures, [])

    def test_late_model_response_cannot_shoot_after_duration(self):
        self.action, self.inference_delay = "shoot", 31
        fl.run_session(self.settings(duration=30), self.output)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["capture_status"], "skipped_duration_expired")

    def test_token_count_time_counts_toward_preview_freshness(self):
        self.action, self.count_delay = "shoot", 31
        fl.run_session(self.settings(max_requests=2), self.output)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["outcome"], "stale_preview")
        self.assertEqual(self.inferences(), [])

    def test_history_tool_rounds_retrieve_images_then_shoot_without_an_extra_call(self):
        history_call = tool("list_history", call_id="list-1")
        history_call["content"] = [{"type": "thinking", "thinking": "", "signature": "opaque-signature"},
                                   {"type": "redacted_thinking", "data": "opaque-encrypted-data"},
                                   *history_call["content"]]
        self.replies = iter([answer("wait"), history_call,
                             tool("get_history", {"frame_ids": [0]}, "get-1"), answer("shoot")])
        fl.run_session(self.settings(max_requests=10, max_shots=1), self.output)
        payloads = self.inferences()
        self.assertEqual([len(p["messages"]) for p in payloads], [1, 3, 5, 7])
        self.assertEqual([model.check_capacity(p)["images"] for p in payloads], [1, 2, 2, 3])
        self.assertEqual(payloads[-1]["messages"][-1]["content"][0]["tool_use_id"], "get-1")
        self.assertEqual(self.camera.captures, [0])
        self.assertEqual(self.tool_outcome(1)["capture_status"], "downloaded")
        self.assertEqual(self.report()["observations"][1]["model_calls"], 3)
        for payload in payloads[2:]:
            self.assertEqual(payload["messages"][3]["content"], history_call["content"])
        self.assertEqual(self.journal(1)[2]["content"], history_call["content"])
        counts = [payload for endpoint, payload, _ in self.requests if endpoint == model.TOKEN_COUNT_ENDPOINT]
        for counted, inferred in zip(counts, payloads):
            self.assertEqual(counted, {key: value for key, value in inferred.items() if key != "max_tokens"})
        self.assertEqual(self.report()["model"], "claude-sonnet-5-5")
        self.assertEqual(self.report()["model_settings"], {
            "effort": "high", "thinking": "adaptive", "thinking_display": "omitted",
            "max_output_tokens": 8192, "automatic_prompt_caching": True,
        })

    def test_invalid_history_request_is_recoverable(self):
        self.replies = iter([tool("get_history", {"frame_ids": [999]}, "get-1"),
                             tool("list_history", {"type": "image"}, "list-1"), answer("wait")])
        fl.run_session(self.settings(), self.output)
        self.assertTrue(self.inferences()[1]["messages"][-1]["content"][0]["is_error"])
        self.assertTrue(self.inferences()[2]["messages"][-1]["content"][0]["is_error"])
        self.assertEqual(self.report()["observations"][0]["outcome"], "no_photo")
        self.assertEqual(self.camera.captures, [])

    def test_run_and_session_budgets_have_distinct_outcomes(self):
        self.replies = iter(tool("list_history", call_id=f"list-{i}") for i in range(12))
        fl.run_session(self.settings(max_requests=12), self.output)
        entries = self.report()["observations"]
        self.assertEqual([e["outcome"] for e in entries], ["run_limit", "session_budget"])
        self.assertEqual([e["model_calls"] for e in entries], [6, 6])
        self.assertEqual(self.camera.frame_times, [100, 160])
        self.assertEqual(self.report()["token_count_requests"], 12)
        self.assertEqual(self.report()["api_requests"], 12)
        self.assertEqual(len(self.inferences()[6]["messages"]), 14)
        self.assertEqual(self.inferences()[6]["messages"][-2]["content"][0]["tool_use_id"], "list-5")
        self.assertEqual(self.report()["stop_reason"], "max_requests")

    def test_duplicate_tool_ids_never_execute_a_shutter(self):
        self.replies = iter([tool("list_history", call_id="same"), tool("shoot", call_id="same")])
        with self.assertRaises(ValueError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["status"], "failed")

    def test_tool_id_deduplication_and_thinking_span_observations(self):
        reply = tool("set_preview_interval", {"seconds": 10}, "same")
        reply["content"].insert(0, {"type": "thinking", "thinking": "", "signature": "opaque"})
        self.replies = iter([reply, tool("shoot", call_id="same")])
        with self.assertRaises(ValueError):
            fl.run_session(self.settings(preview_mode="auto"), self.output)
        self.assertEqual(self.camera.frame_times, [100, 110])
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.inferences()[1]["messages"][1]["content"], reply["content"])
        self.assertEqual(self.inferences()[1]["messages"][2]["content"][0]["tool_use_id"], "same")

    def test_history_followup_refreshes_budget_without_rewriting_prefix(self):
        self.count_delay, self.inference_delay = 2, 5
        self.replies = iter([tool("list_history", call_id="list"), answer("wait")])
        fl.run_session(self.settings(max_requests=2), self.output)
        first, second = self.inferences()
        self.assertEqual(second["messages"][:1], first["messages"])
        state = json.loads(second["messages"][-1]["content"][0]["content"][-1]["text"])["session"]
        self.assertEqual(state["remaining_seconds"], 173)
        self.assertEqual(state["preview_remaining_seconds"], 23)
        self.assertEqual(state["model_calls_remaining"], 1)
        self.assertEqual(state["observation_calls_remaining"], 5)

    def test_serial_compaction_delays_sampling_but_capture_uses_fresh_preview(self):
        for mode, expected in (("auto", 415), ("fixed", 460)):
            with self.subTest(mode=mode):
                destination = self.output.parent / mode
                self.clock.now = 100
                self.camera = FakeCamera(self.clock, destination)
                self.factory.return_value = self.camera
                self.requests.clear()
                self.replies = iter([*[answer("wait") for _ in range(5)], answer("shoot")])
                self.compaction_delay = 75
                with patch("agent.COMPACT_INPUT_TOKENS", 1):
                    fl.run_session(self.settings(preview_mode=mode, duration=1000, max_requests=7, max_shots=1), destination)
                report = json.loads((destination / "results.json").read_text())
                self.assertEqual(self.camera.frame_times, [100, 160, 220, 280, 340, expected])
                self.assertEqual(self.camera.max_delays, [30])
                self.assertEqual((report["api_requests"], report["compaction_requests"], report["compactions_completed"]), (7, 1, 1))
                self.assertEqual(report["token_count_requests"], 7)
                self.assertEqual(sum(e["model_calls"] for e in report["observations"]), 6)
                self.assertEqual(self.inferences()[-1]["messages"][0]["content"], compacted()["content"])
                self.assertEqual(report["compaction"]["usage"]["output_tokens"], 80)
                self.assertNotIn("signed-summary", json.dumps(report))
                events = [e for e in report["observations"][4]["events"] if e["kind"] == "compaction"]
                self.assertEqual(len({e["action_id"] for e in events}), 1)
                self.assertEqual([e["status"] for e in events], ["checking", "started", "completed"])

    def test_compaction_failure_continues_and_retries_after_time_and_new_observation(self):
        self.compaction_replies = iter([RuntimeError("temporary failure"), compacted()])
        with patch("agent.COMPACT_INPUT_TOKENS", 1), patch("agent.KEEP_OBSERVATIONS", 1):
            fl.run_session(self.settings(interval=10, duration=120, max_requests=12), self.output)
        self.assertEqual(self.compaction_times, [110, 170])
        self.assertEqual(self.camera.frame_times, list(range(100, 200, 10)))
        self.assertEqual(self.inferences()[2]["messages"][:3], self.inferences()[1]["messages"])
        report = self.report()
        self.assertEqual((report["api_requests"], report["compaction_requests"], report["compactions_completed"]), (12, 2, 1))
        self.assertEqual(report["context_revision"], 1)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(any(e["kind"] == "compaction" and e["status"] == "failed"
                            for entry in report["observations"] for e in entry["events"]))

    def test_invalid_compaction_cannot_execute_tools_or_replace_history(self):
        self.compaction_replies = iter([tool("shoot", call_id="compaction-shoot")])
        with patch("agent.COMPACT_INPUT_TOKENS", 1):
            fl.run_session(self.settings(duration=1000, max_requests=7), self.output)
        self.assertEqual(self.report()["compaction"]["status"], "failed")
        self.assertEqual(self.report()["context_revision"], 0)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(len(self.inferences()[-1]["messages"]), 11)
        self.assertEqual(self.report()["api_requests"], 7)

    def test_history_tool_reloads_an_image_removed_by_compaction(self):
        self.replies = iter([*[answer("wait") for _ in range(5)],
                             tool("get_history", {"frame_ids": [0]}, "old-image"), answer("shoot")])
        with patch("agent.COMPACT_INPUT_TOKENS", 1):
            fl.run_session(self.settings(duration=1000, max_requests=10, max_shots=1), self.output)
        before, after = self.inferences()[-2:]
        self.assertEqual(model.check_capacity(before)["images"], 5)
        self.assertEqual(model.check_capacity(after)["images"], 6)
        fetched = after["messages"][-1]["content"][0]["content"]
        self.assertEqual(json.loads(fetched[0]["text"])["frame_id"], 0)
        self.assertEqual(fetched[1], self.inferences()[0]["messages"][0]["content"][1])
        self.assertEqual(self.report()["photos_taken"], 1)
        self.assertEqual(self.report()["context_revision"], 1)

    def test_compaction_failure_eventually_stops_at_hard_capacity(self):
        self.compaction_replies = iter([RuntimeError("offline"), RuntimeError("offline")])
        with patch("agent.COMPACT_INPUT_TOKENS", 1), patch("model.MAX_IMAGES", 6):
            fl.run_session(self.settings(duration=1000, max_requests=20), self.output)
        self.assertEqual(self.report()["status"], "capacity_reached")
        self.assertEqual(self.report()["capacity"], {"resource": "images", "used": 7, "limit": 6})
        self.assertEqual(len(list((self.output / "previews").glob("*.jpg"))), 7)
        self.assertEqual(self.report()["context_revision"], 0)

    def test_no_compaction_spends_last_remaining_call(self):
        with patch("agent.COMPACT_INPUT_TOKENS", 1):
            fl.run_session(self.settings(duration=1000, max_requests=6), self.output)
        self.assertEqual(self.compaction_times, [])
        self.assertEqual(len(self.inferences()), 6)
        self.assertEqual(self.report()["stop_reason"], "max_requests")

    def test_compaction_count_failure_preserves_context_without_sending_summary(self):
        original = self.api
        def api(endpoint, payload, config, timeout):
            if "compaction" in payload and endpoint == model.TOKEN_COUNT_ENDPOINT:
                raise RuntimeError("unavailable count")
            return original(endpoint, payload, config, timeout)
        with patch("agent.COMPACT_INPUT_TOKENS", 1), patch("model._post", side_effect=api):
            fl.run_session(self.settings(duration=1000, max_requests=6 + 1), self.output)
        self.assertEqual(self.report()["compaction_requests"], 0)
        self.assertEqual(self.report()["context_revision"], 0)
        self.assertEqual(self.report()["compaction"]["status"], "failed")
        self.assertEqual(len(self.inferences()), 7)

    def test_stop_during_compaction_prevents_next_preview(self):
        stop = threading.Event()
        original = self.api
        def api(endpoint, payload, config, timeout):
            if "compaction" in payload and endpoint == model.API_ENDPOINT:
                stop.set()
            return original(endpoint, payload, config, timeout)
        with patch("agent.COMPACT_INPUT_TOKENS", 1), patch("model._post", side_effect=api), \
                patch.object(stop, "wait", side_effect=self.clock.sleep):
            fl.run_session(self.settings(duration=1000, max_requests=10), self.output, stop_event=stop)
        self.assertEqual(self.camera.frames, 5)
        self.assertEqual(self.report()["status"], "stopped")
        self.assertEqual(self.report()["api_requests"], 6)
        self.assertTrue(self.camera.closed)

    def test_stop_during_compaction_count_does_not_spend_ai_budget(self):
        stop = threading.Event()
        original = self.api
        def api(endpoint, payload, config, timeout):
            if "compaction" in payload and endpoint == model.TOKEN_COUNT_ENDPOINT:
                stop.set()
            return original(endpoint, payload, config, timeout)
        with patch("agent.COMPACT_INPUT_TOKENS", 1), patch("model._post", side_effect=api), \
                patch.object(stop, "wait", side_effect=self.clock.sleep):
            fl.run_session(self.settings(duration=1000, max_requests=10), self.output, stop_event=stop)
        self.assertEqual(self.report()["api_requests"], 5)
        self.assertEqual(self.report()["compaction_requests"], 0)
        self.assertEqual(self.report()["status"], "stopped")

    def test_compaction_timeout_is_bounded_by_session_deadline(self):
        self.compaction_delay = 70
        with patch("agent.COMPACT_INPUT_TOKENS", 1):
            fl.run_session(self.settings(duration=310, max_requests=10), self.output)
        calls = [(payload, timeout) for endpoint, payload, timeout in self.requests
                 if endpoint == model.API_ENDPOINT and "compaction" in payload]
        self.assertEqual(calls[0][1], 70)
        self.assertEqual(self.camera.frames, 5)
        self.assertEqual(self.report()["stop_reason"], "duration")
        self.assertEqual(self.clock.now, 410)

    def test_stop_immediately_before_compaction_dispatch_rolls_back_reserved_call(self):
        stop = threading.Event()
        write = fl.write_json
        def publish(path, report):
            write(path, report)
            if (report.get("compaction") or {}).get("status") == "started":
                stop.set()
        with patch("agent.COMPACT_INPUT_TOKENS", 1), patch("waitless.write_json", side_effect=publish), \
                patch.object(stop, "wait", side_effect=self.clock.sleep):
            fl.run_session(self.settings(duration=1000, max_requests=10), self.output, stop_event=stop)
        self.assertEqual(self.report()["api_requests"], 5)
        self.assertEqual(self.report()["compaction_requests"], 0)
        self.assertEqual(self.report()["compaction"]["status"], "skipped")
        self.assertEqual(self.compaction_times, [])

    def test_history_round_that_ages_preview_starts_no_further_inference(self):
        self.inference_delay = 31
        self.replies = iter(tool("list_history", call_id=f"list-{i}") for i in range(3))
        fl.run_session(self.settings(), self.output)
        self.assertEqual([e["model_calls"] for e in self.report()["observations"]], [1, 1, 1])
        self.assertTrue(all(e["outcome"] == "stale_preview" for e in self.report()["observations"]))
        self.assertTrue(self.journal()[3]["content"][0]["is_error"])

    def test_expiry_is_rechecked_after_recording_requested_capture(self):
        self.action = "shoot"
        write = fl.write_json
        def delayed_write(path, report):
            if report.get("observations") and report["observations"][-1].get("capture_status") == "requested":
                self.clock.sleep(31)
            write(path, report)
        with patch("waitless.write_json", side_effect=delayed_write):
            fl.run_session(self.settings(max_requests=1), self.output)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["capture_status"], "skipped_expired_before_shutter")

    def test_remaining_freshness_is_passed_to_camera(self):
        self.action, self.count_delay, self.inference_delay = "shoot", 2, 3
        fl.run_session(self.settings(max_shots=1), self.output)
        self.assertEqual(self.camera.max_delays, [25])
        self.assertEqual(len(self.inferences()), 1)
        self.assertEqual(self.report()["stop_reason"], "max_shots")
        self.assertEqual(self.tool_outcome()["capture_status"], "downloaded")

    def test_cooldown_is_enforced_between_successful_captures(self):
        self.action = "shoot"
        fl.run_session(self.settings(cooldown=90), self.output)
        self.assertEqual(self.camera.captures, [0, 1])
        self.assertEqual(self.report()["observations"][1]["capture_status"], "skipped_cooldown")

    def test_retrieved_image_capacity_preserves_the_run_and_all_previews(self):
        self.replies = iter([answer("wait"), answer("wait"), tool("get_history", {"frame_ids": [0, 1]})])
        with patch("model.MAX_IMAGES", 4):
            fl.run_session(self.settings(max_requests=5), self.output)
        report = self.report()
        self.assertEqual(report["status"], "capacity_reached")
        self.assertEqual(report["capacity"], {"resource": "images", "used": 5, "limit": 4})
        self.assertEqual((report["previews_saved"], report["api_requests"], report["token_count_requests"]), (3, 3, 3))
        self.assertEqual(len(list((self.output / "previews").glob("*.jpg"))), 3)
        retrieved = self.journal(2)[-1]["content"][0]["content"]
        self.assertEqual([b["file"] for b in retrieved if b["type"] == "image"],
                         ["previews/000000.jpg", "previews/000001.jpg"])
        self.assertTrue(self.camera.closed)

    def test_request_size_capacity_stops_before_either_endpoint(self):
        with patch("model.MAX_REQUEST_BYTES", 1):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(self.report()["status"], "capacity_reached")
        self.assertEqual(self.report()["capacity"]["resource"], "request_bytes")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.report()["previews_saved"], 1)
        self.assertEqual(len(self.journal()), 2)

    def test_token_capacity_stops_before_inference(self):
        self.tokens = 990001
        fl.run_session(self.settings(), self.output)
        self.assertEqual(self.report()["status"], "capacity_reached")
        self.assertEqual(self.report()["capacity"]["resource"], "input_tokens")
        self.assertEqual(self.report()["token_count_requests"], 1)
        self.assertEqual(self.report()["api_requests"], 0)
        self.assertEqual(self.camera.captures, [])

    def test_api_failure_stops_without_retry_or_no_photo_outcome(self):
        self.replies = iter([RuntimeError("connection failed")])
        with self.assertRaises(RuntimeError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(self.report()["status"], "failed")
        self.assertNotIn("action", self.report()["observations"][0])
        self.assertEqual(self.report()["api_requests"], 1)
        self.assertEqual(self.camera.captures, [])
        self.assertTrue(self.camera.closed)

    def test_failed_token_count_never_falls_back_to_inference(self):
        with patch("waitless.count_tokens", side_effect=RuntimeError("count failed")), self.assertRaises(RuntimeError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.report()["token_count_requests"], 1)
        self.assertEqual(self.report()["status"], "failed")

    def test_invalid_turn_stops_before_shutter(self):
        self.action = "maybe"
        with self.assertRaises(ValueError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["status"], "failed")

    def test_download_failure_is_recorded_and_never_retried(self):
        self.action = "shoot"
        def failed_capture(index, on_shutter, max_delay):
            on_shutter()
            raise RuntimeError("Disconnected while downloading")
        with patch.object(self.camera, "capture", side_effect=failed_capture) as capture, self.assertRaises(RuntimeError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.report()["photos_taken"], 0)
        self.assertEqual(self.report()["observations"][0]["capture_status"], "failed_after_shutter")
        self.assertEqual(self.tool_outcome()["capture_status"], "failed_after_shutter")
        self.assertFalse((self.output / "captures.jsonl").exists())
        self.assertTrue(self.camera.closed)

    def test_unconfirmed_shutter_is_terminal(self):
        self.action = "shoot"
        with patch.object(self.camera, "capture", side_effect=RuntimeError("no confirmation")) as capture, self.assertRaises(RuntimeError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.report()["observations"][0]["capture_status"], "failed_or_unconfirmed")

    def test_interrupt_closes_camera(self):
        self.replies = iter([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(self.report()["status"], "interrupted")
        self.assertEqual(self.camera.captures, [])
        self.assertTrue(self.camera.closed)

    def test_ui_stop_before_connection_never_opens_camera(self):
        stop = threading.Event()
        stop.set()
        fl.run_session(self.settings(), self.output, stop_event=stop)
        self.factory.assert_not_called()
        self.assertEqual(self.report()["status"], "stopped")

    def test_ui_stop_interrupts_interval_wait(self):
        stop = threading.Event()
        with patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            fl.run_session(self.settings(), self.output, stop_event=stop)
        self.assertEqual(self.camera.frames, 1)
        self.assertEqual(len(self.inferences()), 1)
        self.assertEqual(self.report()["stop_reason"], "user_stop")
        self.assertTrue(self.camera.closed)

    def test_ui_stop_during_count_prevents_inference(self):
        stop = threading.Event()
        def count(*_, **__):
            stop.set()
            return 1000
        with patch("waitless.count_tokens", side_effect=count):
            fl.run_session(self.settings(), self.output, stop_event=stop)
        self.assertEqual(self.inferences(), [])
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["status"], "stopped")

    def test_ui_stop_during_preview_preserves_observation_in_history(self):
        stop = threading.Event()
        frame = self.camera.latest_frame
        def stopped_frame():
            stop.set()
            return frame()
        with patch.object(self.camera, "latest_frame", side_effect=stopped_frame):
            fl.run_session(self.settings(), self.output, stop_event=stop)
        self.assertEqual(self.inferences(), [])
        self.assertEqual(self.report()["previews_saved"], 1)
        self.assertEqual(self.journal()[-1]["content"][-1], {"type": "image", "file": "previews/000000.jpg"})

    def test_ui_stop_during_inference_does_not_execute_returned_shoot(self):
        stop = threading.Event()
        def inference(*_, **__):
            stop.set()
            return answer("shoot")
        with patch("waitless.call_model", side_effect=inference):
            fl.run_session(self.settings(), self.output, stop_event=stop)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["capture_status"], "skipped_user_stop")
        self.assertIn("skipped_user_stop", json.dumps(self.journal()[-1]))

    def test_ui_stop_immediately_before_shutter_prevents_capture(self):
        self.action = "shoot"
        stop = threading.Event()
        write = fl.write_json
        def stopping_write(path, report):
            write(path, report)
            if report.get("observations") and report["observations"][-1].get("capture_status") == "requested":
                stop.set()
        with patch("waitless.write_json", side_effect=stopping_write):
            fl.run_session(self.settings(), self.output, stop_event=stop)
        self.assertEqual(self.camera.captures, [])
        self.assertEqual(self.report()["observations"][0]["capture_status"], "skipped_user_stop")

    def test_ui_stop_after_shutter_preserves_completed_download(self):
        self.action = "shoot"
        stop = threading.Event()
        capture = self.camera.capture
        def stopping_capture(*args, **kwargs):
            stop.set()
            return capture(*args, **kwargs)
        with patch.object(self.camera, "capture", side_effect=stopping_capture):
            fl.run_session(self.settings(), self.output, stop_event=stop)
        self.assertEqual(self.camera.captures, [0])
        self.assertEqual(self.report()["status"], "stopped")
        self.assertEqual(self.report()["photos_taken"], 1)
        self.assertEqual(self.report()["observations"][0]["capture_status"], "downloaded")
        self.assertIn("downloaded", json.dumps(self.journal()[-1]))
        self.assertTrue(self.camera.closed)

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        (self.output / "keep.txt").write_text("existing user output")
        with self.assertRaises(FileExistsError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual((self.output / "keep.txt").read_text(), "existing user output")
        self.factory.assert_not_called()

    def test_journal_initialization_failure_marks_preview_not_sent(self):
        with patch("waitless.AgentJournal", side_effect=OSError("disk full")), self.assertRaises(OSError):
            fl.run_session(self.settings(), self.output)
        entry = self.report()["observations"][0]
        self.assertEqual((entry["run_status"], entry["outcome"], entry["model_calls"]), ("failed", "error", 0))
        self.assertTrue((self.output / entry["preview_file"]).is_file())
        self.assertEqual(self.requests, [])
        self.assertTrue(self.camera.closed)

    def test_archive_failure_stops_before_api_or_shutter(self):
        with patch("history.PreviewArchive.append", side_effect=OSError("disk full")), self.assertRaises(OSError):
            fl.run_session(self.settings(), self.output)
        self.assertEqual(self.requests, [])
        self.assertTrue(self.camera.closed)
        self.assertEqual(self.report()["status"], "failed")

    def test_detected_camera_model_is_reported(self):
        self.camera.model = "ILCE-6700"
        fl.run_session(self.settings(max_requests=1), self.output)
        self.assertEqual(self.report()["source"], "sony:usb:ILCE-6700")

    def test_capability_failure_is_reported_before_any_api_call(self):
        capabilities = {"live_view": True, "capture_ready": False, "preview_issues": [],
                        "capture_issues": ["Still-photo shutter release is not advertised by this camera."]}
        self.factory.side_effect = CameraError("Capture unavailable", "ILME-FR7", capabilities)
        with self.assertRaises(CameraError):
            fl.run_session(self.settings(), self.output)
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
        event = {"model": "ILME-FR7", "connection": "USB",
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
        event = {"event": "ready", "model": "Preview Camera", "connection": "USB",
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

    def test_missing_camera_information_and_invalid_capabilities_fail_closed(self):
        camera = SonyCamera(self.output, enable_capture=True)
        valid = {"model": "ILCE-7RM5", "connection": "USB", "capabilities": self.camera.capabilities}
        for event in ({"model": "ILCE-7M4"}, {**valid, "connection": "IP"},
                      {**valid, "capabilities": {}},
                      {**valid, "capabilities": {**self.camera.capabilities, "capture_ready": 1}}):
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                camera._accept_camera(event)


if __name__ == "__main__":
    unittest.main()
