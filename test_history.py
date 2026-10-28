"""Visual retrieval, bounded pagination, and local per-observation journals."""

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from camera import CameraFrame
from history import PreviewArchive, AgentJournal
from model import image_content, text_content


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = PreviewArchive(self.root, 100)
        for i in range(61):
            self.archive.append(CameraFrame(Image.new('RGB', (48, 32), (i, 0, 0)), 100 + i * 60, i))

    def test_pagination_filters_and_capture_facts(self):
        self.archive.record_outcome({'frame_id': 30, 'capture_status': 'downloaded', 'saved_files': ['moments/photo.jpg']})
        page = self.archive.list_history(60, {})
        self.assertEqual([p['frame_id'] for p in page['previews']], list(range(59, 39, -1)))
        self.assertEqual(page['next_cursor'], 40)
        page = self.archive.list_history(60, {'cursor': 40, 'start_s': 30 * 60, 'end_s': 35 * 60, 'limit': 3})
        self.assertEqual([p['frame_id'] for p in page['previews']], [35, 34, 33])
        last = self.archive.list_history(60, {'cursor': page['next_cursor'], 'start_s': 30 * 60})
        self.assertEqual([p['frame_id'] for p in last['previews']], [32, 31, 30])
        self.assertIsNone(last['next_cursor'])
        self.assertEqual(last['previews'][-1]['capture_outcome']['capture_status'], 'downloaded')
        self.assertEqual(self.archive.list_history(0, {})['previews'], [])

    def test_ids_only_bounded_retrieval_and_validated_pagination(self):
        self.assertEqual([p.id for p in self.archive.get_history(60, {'frame_ids': [2, 0]})], [2, 0])
        for args in ({'frame_ids': []}, {'frame_ids': [60]}, {'frame_ids': [-1]}, {'frame_ids': [True]},
                     {'frame_ids': list(range(9))}, {'frame_ids': [2, 2]}, {'frame_ids': ['../.env']}, {'path': 'x'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.archive.get_history(60, args)
        for args in ({'limit': 51}, {'limit': True}, {'cursor': 61}, {'start_s': float('nan')},
                     {'start_s': 2, 'end_s': 1}, {'path': 'x'}, {'end_s': -1}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.archive.list_history(60, args)

    def test_nested_image_references_and_ordered_durable_events(self):
        entry = {}
        preview = self.archive.previews[-1]
        journal = AgentJournal(self.root, preview, entry, lambda: None)
        images = self.archive.get_history(60, {'frame_ids': [1, 2]})
        message = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'get-1',
                   'content': [text_content('images'), *[image_content(p.image()) for p in images]]}]}
        journal.message(message, images)
        journal.message({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'invalid-input',
                         'name': 'list_history', 'input': {'type': 'image'}}]})
        journal.event('tool', 'started', 'get-1', name='get_history')
        journal.event('tool', 'completed', 'get-1', frame_ids=[1, 2])
        raw = (journal.folder / 'conversation.jsonl').read_text()
        self.assertNotIn('base64', raw)
        blocks = json.loads(raw.splitlines()[0])['content'][0]['content']
        self.assertEqual([b['file'] for b in blocks if b['type'] == 'image'], ['previews/000001.jpg', 'previews/000002.jpg'])
        self.assertIn('source', message['content'][0]['content'][1])
        events = [json.loads(line) for line in (journal.folder / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events, entry['events'])
        self.assertEqual([e['id'] for e in events], ['60:0', '60:1'])
        self.assertEqual([e['sequence'] for e in events], [0, 1])
        self.assertTrue(all(e['observation_id'] == 60 and e['timestamp'] for e in events))
        self.assertEqual(len(list(self.archive.folder.glob('*.jpg'))), 61)
        self.archive.record_capture(0, preview, [self.root / 'photo.jpg'])
        self.assertEqual(json.loads((self.root / 'captures.jsonl').read_text())['trigger_frame_id'], 60)


if __name__ == '__main__':
    unittest.main()
