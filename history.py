"""Save preview images and their metadata."""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class Preview:
    id: int
    timestamp_s: float
    received_at: float
    path: Path

    def metadata(self):
        return {'frame_id': self.id, 'timestamp_s': self.timestamp_s}

    def image(self):
        with Image.open(self.path) as image:
            return image.convert('RGB')


class PreviewArchive:
    def __init__(self, output: Path, start: float):
        self.start = start
        self.folder = output / 'previews'
        self.folder.mkdir()
        self.count = 0

    def append(self, frame):
        path = self.folder / f'{self.count:06d}.jpg'
        preview = Preview(self.count, frame.received_at - self.start, frame.received_at,
                          path)
        frame.image.save(path, format='JPEG', quality=95, subsampling=0)
        self.count += 1
        return preview
