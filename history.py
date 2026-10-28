"""Local visual history, capture facts, and durable per-observation agent journals."""

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
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
        self.previews = []
        self.outcomes = {}

    @property
    def count(self):
        return len(self.previews)

    def append(self, frame):
        path = self.folder / f'{self.count:06d}.jpg'
        preview = Preview(self.count, frame.received_at - self.start, frame.received_at,
                          frame.frame_no, path, *frame.image.size)
        frame.image.save(path, format='JPEG', quality=95, subsampling=0)
        append_json(self.folder / 'index.jsonl', {**preview.metadata(), 'file': path.name})
        self.previews.append(preview)
        return preview

    def describe(self, preview):
        return {**preview.metadata(), 'capture_outcome': self.outcomes.get(preview.id, {
            'frame_id': preview.id, 'capture_status': 'not_requested'})}

    def list_history(self, current_id, arguments):
        if set(arguments) - {'start_s', 'end_s', 'cursor', 'limit'}:
            raise ValueError('Use only start_s, end_s, cursor, and limit.')
        limit = arguments.get('limit', 20)
        cursor = arguments.get('cursor', current_id)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit must be an integer from 1 to 50.')
        if type(cursor) is not int or not 0 <= cursor <= current_id:
            raise ValueError('cursor must be a preview ID no later than the current preview.')
        for key in ('start_s', 'end_s'):
            if key in arguments:
                value = arguments[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError('Time filters must be finite, non-negative seconds.')
        start, end = arguments.get('start_s', 0), arguments.get('end_s', math.inf)
        if start > end:
            raise ValueError('start_s must not exceed end_s.')
        previews = [p for p in reversed(self.previews[:current_id])
                    if p.id < cursor and start <= p.timestamp_s <= end]
        page = previews[:limit]
        return {'previews': [self.describe(p) for p in page],
                'next_cursor': page[-1].id if len(previews) > limit else None}

    def get_history(self, current_id, arguments):
        ids = arguments.get('frame_ids')
        if (set(arguments) != {'frame_ids'} or not isinstance(ids, list) or not 1 <= len(ids) <= 8
                or any(type(value) is not int or not 0 <= value < current_id for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Supply one to eight distinct earlier frame_ids from this session.')
        return [self.previews[value] for value in ids]

    def record_outcome(self, entry):
        self.outcomes[entry['frame_id']] = capture_outcome(entry)

    def record_capture(self, capture_id, preview, files):
        append_json(self.output / 'captures.jsonl', {
            'capture_id': capture_id, 'trigger_frame_id': preview.id,
            'trigger_timestamp_s': preview.timestamp_s,
            'saved_files': [str(p.relative_to(self.output)) for p in files],
        })


class AgentJournal:
    def __init__(self, output, preview, entry, publish):
        self.output, self.preview, self.entry, self.publish = output, preview, entry, publish
        self.folder = output / 'runs' / f'{preview.id:06d}'
        self.folder.mkdir(parents=True)
        self.entry.update(conversation_file=str((self.folder / 'conversation.jsonl').relative_to(output)),
                          events_file=str((self.folder / 'events.jsonl').relative_to(output)), events=[])

    def message(self, message, images=()):
        # Tool results nest images; journal references follow the same traversal order.
        remaining = iter(images)
        def local(blocks):
            result = []
            for value in blocks:
                if value.get('type') == 'image':
                    preview = next(remaining)
                    result.append({'type': 'image', 'file': str(preview.path.relative_to(self.output))})
                elif value.get('type') == 'tool_result' and isinstance(value.get('content'), list):
                    result.append({**value, 'content': local(value['content'])})
                else:
                    result.append(deepcopy(value))
            return result
        record = {'role': message['role'], 'content': local(message['content'])}
        if next(remaining, None) is not None:
            raise ValueError('Unused image references in agent journal')
        append_json(self.folder / 'conversation.jsonl', record)

    def event(self, kind, status, action_id, **details):
        seq = len(self.entry['events'])
        event = {'id': f'{self.preview.id}:{seq}', 'sequence': seq, 'observation_id': self.preview.id,
                 'timestamp': datetime.now(timezone.utc).isoformat(),
                 'elapsed_s': max(0, time.monotonic() - self.preview.received_at),
                 'kind': kind, 'status': status, 'action_id': action_id, **details}
        append_json(self.folder / 'events.jsonl', event)
        self.entry['events'].append(event)
        self.publish()
