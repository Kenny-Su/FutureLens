"""Saved previews, capture records, and per-observation logs."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from PIL import Image


def append_json(path, value):
    with path.open('a', encoding='utf-8') as log:
        log.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')


def capture_outcome(entry):
    return {key: entry[key] for key in ('frame_id', 'capture_status', 'capture_id',
                                      'shutter_sent_at', 'saved_files') if key in entry}


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
        return {'frame_id': self.id, 'timestamp_s': self.timestamp_s,
                'frame_no': self.frame_no, 'width': self.width, 'height': self.height}

    def image(self):
        with Image.open(self.path) as image:
            return image.convert('RGB')


class PreviewArchive:
    def __init__(self, output: Path, start: float):
        self.output, self.start = output, start
        self.folder = output / 'previews'
        self.folder.mkdir()
        self.count = 0

    def append(self, frame):
        path = self.folder / f'{self.count:06d}.jpg'
        preview = Preview(self.count, frame.received_at - self.start, frame.received_at,
                          frame.frame_no, path, *frame.image.size)
        frame.image.save(path, format='JPEG', quality=95, subsampling=0)
        append_json(self.folder / 'index.jsonl', {**preview.metadata(), 'file': path.name})
        self.count += 1
        return preview

    def record_capture(self, capture_id, preview, files):
        append_json(self.output / 'captures.jsonl', {
            'capture_id': capture_id, 'trigger_frame_id': preview.id,
            'trigger_timestamp_s': preview.timestamp_s,
            'saved_files': [str(p.relative_to(self.output)) for p in files],
        })


class ObservationJournal:
    def __init__(self, output, preview, entry, publish):
        self.output, self.preview, self.entry, self.publish = output, preview, entry, publish
        self.folder = output / 'runs' / f'{preview.id:06d}'
        self.folder.mkdir(parents=True)
        self.entry.update(conversation_file=str((self.folder / 'conversation.jsonl').relative_to(output)),
                          events_file=str((self.folder / 'events.jsonl').relative_to(output)), events=[])

    def message(self, message):
        append_json(self.folder / 'conversation.jsonl', message)

    def event(self, kind, status, action_id, **details):
        seq = len(self.entry['events'])
        event = {'id': f'{self.preview.id}:{seq}', 'sequence': seq, 'observation_id': self.preview.id,
                 'timestamp': datetime.now(timezone.utc).isoformat(),
                 'elapsed_s': max(0, time.monotonic() - self.preview.received_at),
                 'kind': kind, 'status': status, 'action_id': action_id, **details}
        append_json(self.folder / 'events.jsonl', event)
        self.entry['events'].append(event)
        self.publish()
