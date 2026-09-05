import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import snapshot


class SnapshotConsistencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        for name, value in [('DATA', str(self.data)),
                            ('THREADS', str(self.data / 'threads')), ('PACE', 0)]:
            p = patch.object(snapshot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def read(self, name):
        return json.loads((self.data / name).read_text())

    def test_activity_does_not_include_threads_created_during_fetch(self):
        snapshot.jdump(str(self.data / 'state.json'), {'last_seq': 10})
        activity = [{'id': 'known', 'seq': 10}]

        def request(method, path, **kwargs):
            if path == '/v1/activity':
                if 'after' in kwargs.get('query', {}):
                    return {'items': []}
                return {'items': list(activity)}
            if path == '/v1/posts':
                return {'items': [{'id': 'known', 'seq': 10}], 'pinned': []}
            if path == '/v1/posts/known':
                # A new thread and a later reply arrive after activity was read.
                activity.insert(0, {'id': 'late-thread', 'seq': 11})
                return {'post': {'id': 'known', 'seq': 10},
                        'replies': {'items': [{'id': 'late-reply', 'seq': 12}]}}
            if path == '/b':
                return ''
            self.fail(f'Unexpected request: {path}')

        with patch.object(snapshot.gpb, 'request', side_effect=request):
            snapshot.main()

        self.assertEqual(self.read('activity.json')['items'], [{'id': 'known', 'seq': 10}])
        self.assertEqual(self.read('state.json')['last_seq'], 10)
        self.assertEqual(self.read('threads/known.json')['replies']['items'][0]['seq'], 12)

    def test_first_snapshot_uses_activity_boundary_and_includes_pinned_threads(self):
        def request(method, path, **kwargs):
            if path == '/v1/activity':
                return {'items': [{'id': 'reply', 'thread_id': 'root', 'seq': 25}]}
            if path == '/v1/posts':
                return {'items': [{'id': 'root', 'seq': 5}], 'pinned': [{'id': 'pin', 'seq': 1}]}
            if path == '/v1/posts/root':
                return {'post': {'id': 'root', 'seq': 5},
                        'replies': {'items': [{'id': 'reply', 'seq': 25},
                                              {'id': 'late-reply', 'seq': 27}]}}
            if path == '/v1/posts/pin':
                return {'post': {'id': 'pin', 'seq': 1}, 'replies': {'items': []}}
            if path == '/b':
                return ''
            self.fail(f'Unexpected request: {path}')

        with patch.object(snapshot.gpb, 'request', side_effect=request):
            snapshot.main()

        self.assertEqual(self.read('state.json')['last_seq'], 25)
        self.assertEqual({t['id'] for t in self.read('index.json')['threads']}, {'root', 'pin'})
        self.assertEqual(self.read('threads/pin.json')['post']['id'], 'pin')

    def test_deleted_thread_is_removed_from_activity_and_index(self):
        snapshot.jdump(str(self.data / 'state.json'), {'last_seq': 10})
        snapshot.jdump(str(self.data / 'threads/gone.json'),
                       {'post': {'id': 'gone', 'seq': 10}, 'replies': {'items': []}})

        def request(method, path, **kwargs):
            if path == '/v1/activity':
                if kwargs['query'].get('after') == 11:
                    return {'items': []}
                return {'items': [{'id': 'reply', 'thread_id': 'gone', 'seq': 11}]}
            if path == '/v1/posts':
                return {'items': [], 'pinned': [{'id': 'gone', 'seq': 10}]}
            if path == '/v1/posts/gone':
                raise snapshot.gpb.ApiError(404, 'NOT_FOUND', 'Removed')
            if path == '/b':
                return ''
            self.fail(f'Unexpected request: {path}')

        with patch.object(snapshot.gpb, 'request', side_effect=request):
            snapshot.main()

        self.assertEqual(self.read('state.json')['last_seq'], 11)
        self.assertEqual(self.read('activity.json')['items'], [])
        self.assertEqual(self.read('index.json')['threads'], [])
        self.assertEqual(self.read('index.json')['pinned'], [])
        self.assertFalse((self.data / 'threads/gone.json').exists())


if __name__ == '__main__':
    unittest.main()
