"""Local preview and conversation persistence without retrieval or sampling threads."""

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from camera import CameraFrame
from history import PreviewArchive
from model import image_content, text_content


class HistoryTests(unittest.TestCase):
    def test_archive_and_journal_keep_images_and_factual_capture_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = PreviewArchive(root, 100)
            for i in range(5):
                preview = archive.append(CameraFrame(Image.new("RGB", (48, 32), "red"), 100 + i * 60, i))
                message = {"role": "user", "content": [text_content(preview.metadata()), image_content(preview.image())]}
                archive.record_message(message, preview)
                self.assertIn("source", message["content"][1])
            records = [json.loads(line) for line in (archive.folder / "index.jsonl").read_text().splitlines()]
            self.assertEqual([r["frame_id"] for r in records], list(range(5)))
            self.assertEqual([r["timestamp_s"] for r in records], [0, 60, 120, 180, 240])
            self.assertEqual(archive.count, 5)
            self.assertEqual(preview.image().size, (48, 32))
            self.assertEqual(len(list(archive.folder.glob("*.jpg"))), 5)
            journal = (root / "conversation.jsonl").read_text()
            self.assertNotIn("base64", journal)
            for line in journal.splitlines():
                for block in json.loads(line)["content"]:
                    if block["type"] == "image":
                        self.assertTrue((root / block["file"]).is_file())
            archive.record_capture(0, preview, [root / "photo.jpg"])
            record = json.loads((root / "captures.jsonl").read_text())
            self.assertEqual(record, {"capture_id": 0, "trigger_frame_id": 4,
                                     "trigger_timestamp_s": 240, "saved_files": ["photo.jpg"]})


if __name__ == "__main__":
    unittest.main()
