"""Session memory, signed compaction boundaries, and durable context reconstruction."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from agent import SessionAgent
from camera import CameraFrame
from history import PreviewArchive, local_message
from model import image_content, text_content
from test_model import compacted, tool


class SessionAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.agent = SessionAgent(self.root)
        self.archive = PreviewArchive(self.root, 100)

    def observe(self, frame_id):
        preview = self.archive.append(CameraFrame(Image.new("RGB", (32, 24), "blue"), 100 + frame_id * 60, frame_id))
        self.agent.append({"role": "user", "content": [text_content(preview.metadata()),
                           image_content(preview.image())]}, frame_id, [preview])
        sent = deepcopy(self.agent.messages)
        reply = tool("set_preview_interval", {"seconds": 60}, f"schedule-{frame_id}")
        reply["content"] = [{"type": "thinking", "thinking": "", "signature": f"thinking-{frame_id}"},
                            {"type": "redacted_thinking", "data": f"opaque-{frame_id}"}, *reply["content"]]
        self.agent.accept_response(reply, frame_id, 100_000)
        self.agent.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"schedule-{frame_id}",
                          "content": [text_content({"seconds": 60})]}]}, frame_id)
        return sent

    def test_recent_four_observations_and_thinking_survive_repeated_compaction(self):
        requests = [self.observe(i) for i in range(5)]
        before = deepcopy(self.agent.messages)
        cut, payload = self.agent.compaction_candidate(4, 400)
        self.assertEqual(payload["messages"], requests[0])
        self.assertEqual(payload["messages"][-1]["role"], "user")
        self.assertEqual(before[cut]["role"], "assistant")
        self.agent.apply_compaction(compacted(), cut)
        self.assertEqual(self.agent.messages[1:], before[cut:])
        self.assertEqual(self.agent.payload()["system"], payload["system"])
        self.assertEqual(self.agent.payload()["tools"], payload["tools"])
        self.assertEqual(self.agent.payload()["thinking"], payload["thinking"])
        # Every image from the latest four observations is retained unmodified.
        images = [b for m in self.agent.messages for b in m["content"] if b["type"] == "image"]
        self.assertEqual(len(images), 4)
        self.assertIsNone(self.agent.compaction_candidate(4, 400))
        self.observe(5)
        before = deepcopy(self.agent.messages)
        cut, payload = self.agent.compaction_candidate(5, 460)
        self.assertEqual(payload["messages"][0]["content"][0]["type"], "compaction")
        self.agent.apply_compaction(compacted("New summary", "new-signature"), cut)
        self.assertEqual(self.agent.messages[1:], before[cut:])
        self.assertEqual(sum(b["type"] == "compaction" for m in self.agent.messages for b in m["content"]), 1)
        self.assertEqual(self.agent.revision, 2)
        self.assertEqual(self.archive.get_history(5, {"frame_ids": [0]})[0].image().size, (32, 24))
        with self.assertRaises(ValueError):
            self.agent.accept_response(tool("shoot", call_id="schedule-0"), 5, 1000)

    def test_failed_summary_does_not_change_history_or_allow_immediate_retry(self):
        for i in range(5):
            self.observe(i)
        before = deepcopy(self.agent.messages)
        cut, _ = self.agent.compaction_candidate(4, 400)
        with self.assertRaises(ValueError):
            self.agent.apply_compaction({"stop_reason": "max_tokens", "content": []}, cut)
        self.assertEqual(self.agent.messages, before)
        self.agent.last_attempt_observation = 4
        self.agent.compaction_failed(400)
        self.assertIsNone(self.agent.compaction_candidate(4, 500))
        self.observe(5)
        self.assertIsNone(self.agent.compaction_candidate(5, 459))
        self.assertIsNotNone(self.agent.compaction_candidate(5, 460))

    def test_empty_end_turn_is_logged_without_replaying_an_empty_assistant_message(self):
        self.agent.append({"role": "user", "content": [text_content("preview 0")]}, 0)
        self.assertIsNone(self.agent.accept_response({"stop_reason": "end_turn", "content": []}, 0, 10))
        self.agent.append({"role": "user", "content": [text_content("preview 1")]}, 1)
        self.assertEqual([m["role"] for m in self.agent.payload()["messages"]], ["user", "user"])
        self.assertIn('"type": "empty_response"', self.agent.log.read_text())

    def test_image_and_byte_pressure_trigger_without_high_token_count(self):
        for i in range(5):
            self.observe(i)
        self.agent.estimated_tokens = 1
        self.assertIsNone(self.agent.compaction_candidate(4, 400))
        for setting in ("COMPACT_IMAGES", "COMPACT_REQUEST_BYTES"):
            with patch("agent." + setting, 1):
                self.assertIsNotNone(self.agent.compaction_candidate(4, 400))

    def test_append_log_reconstructs_active_context_without_duplicate_image_bytes(self):
        for i in range(5):
            self.observe(i)
        cut, _ = self.agent.compaction_candidate(4, 400)
        self.agent.apply_compaction(compacted(), cut)
        self.observe(5)
        cut, _ = self.agent.compaction_candidate(5, 460)
        self.agent.apply_compaction(compacted("Again"), cut)
        reconstructed, revision = [], 0
        raw = self.agent.log.read_text()
        self.assertNotIn("base64", raw)
        for record in map(json.loads, raw.splitlines()):
            self.assertEqual(record["context_revision"], revision)
            if record["type"] == "message":
                self.assertEqual(record["index"], len(reconstructed))
                reconstructed.append(record["message"])
            elif record["type"] == "compaction":
                reconstructed = [record["message"], *reconstructed[record["replaced_messages"]:]]
                revision = record["next_context_revision"]
        expected = []
        for message in self.agent.messages:
            images = []
            if message["role"] == "user" and message["content"][0]["type"] == "text":
                frame_id = json.loads(message["content"][0]["text"])["frame_id"]
                images = [self.archive.previews[frame_id]]
            expected.append(local_message(message, self.root, images))
        self.assertEqual(reconstructed, expected)
        self.assertEqual(revision, 2)


if __name__ == "__main__":
    unittest.main()
