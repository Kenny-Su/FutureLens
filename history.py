"""Append-only preview, conversation, and capture records for one session."""

from dataclasses import dataclass
import json
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class Preview:
    id: int
    timestamp_s: float
    received_at: float
    frame_no: int
    path: Path
    width: int
    height: int

    def metadata(self):
        return {"frame_id": self.id, "timestamp_s": self.timestamp_s,
                "frame_no": self.frame_no, "width": self.width, "height": self.height}

    def image(self):
        with Image.open(self.path) as image:
            return image.convert("RGB")


class PreviewArchive:
    def __init__(self, output: Path, start: float):
        self.output, self.start = output, start
        self.folder = output / "previews"
        self.folder.mkdir()
        self.count = 0

    def _append_json(self, path, value):
        with path.open("a") as log:
            log.write(json.dumps(value, ensure_ascii=False) + "\n")

    def append(self, frame):
        path = self.folder / f"{self.count:06d}.jpg"
        preview = Preview(self.count, frame.received_at - self.start, frame.received_at,
                          frame.frame_no, path, *frame.image.size)
        frame.image.save(path, format="JPEG", quality=95, subsampling=0)
        self._append_json(self.folder / "index.jsonl", {**preview.metadata(), "file": path.name})
        self.count += 1
        return preview

    def record_message(self, message, preview=None):
        # Keep images as local file references in the journal, not repeated base64 data.
        content = [{"type": "image", "file": str(preview.path.relative_to(self.output))}
                   if block["type"] == "image" else block for block in message["content"]]
        self._append_json(self.output / "conversation.jsonl", {"role": message["role"], "content": content})

    def record_capture(self, capture_id, preview, files):
        self._append_json(self.output / "captures.jsonl", {
            "capture_id": capture_id, "trigger_frame_id": preview.id,
            "trigger_timestamp_s": preview.timestamp_s,
            "saved_files": [str(p.relative_to(self.output)) for p in files],
        })
