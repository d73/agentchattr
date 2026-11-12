"""Exercise pagination through the actual websocket handler, with isolated stores."""
import asyncio
import json
import unittest

from fastapi import WebSocketDisconnect
import test_history_delivery as history_fixture
app = history_fixture.app


class HistoryPagingProtocolTests(unittest.TestCase):
    def setUp(self):
        history_fixture.HistoryDeliveryTests.setUp(self)
        self.settings.update(channels=['general', 'exact', 'long', 'empty'], history_limit=50)
        self.saved = {}
        for channel, count in [('general', 10), ('exact', 50), ('long', 225)]:
            self.saved[channel] = [self.messages.add(
                'Ben', f'{channel} message {i}', channel=channel,
                timestamp=1800000000 + i * 3600, _bulk=True,
            ) for i in range(count)]

    def exchange(self, requests=()):
        frames = []
        requests = iter(requests)

        class Socket:
            query_params = {'token': 'test'}
            async def accept(self): pass
            async def send_text(self, raw): frames.append(json.loads(raw))
            async def receive_text(self):
                try: return json.dumps(next(requests))
                except StopIteration: raise WebSocketDisconnect()

        asyncio.run(app.websocket_endpoint(Socket()))
        return frames

    def test_initial_boundaries_and_pages_reach_id_zero_without_duplicates(self):
        frames = self.exchange()
        complete = next(f for f in frames if f['type'] == 'history_complete')
        self.assertEqual(complete['has_more'], {'general': False, 'exact': False, 'long': True, 'empty': False})
        initial = [m for f in frames if f['type'] == 'history' for m in f['messages']]
        self.assertEqual(len(initial), 110)
        loaded = [m for m in initial if m['channel'] == 'long']
        sizes = []
        while True:
            frames = self.exchange([{'type': 'load_earlier', 'channel': 'long', 'before_id': loaded[0]['id'], 'limit': 50}])
            page = next(f for f in frames if f['type'] == 'history_earlier')
            sizes.append(len(page['messages']))
            loaded = page['messages'] + loaded
            if not page['has_more']: break
        self.assertEqual(sizes, [50, 50, 50, 25])
        self.assertEqual([m['id'] for m in loaded], [m['id'] for m in self.saved['long']])
        self.assertEqual(len(self.messages.get_recent(1000)), 285)

    def test_page_limits_and_invalid_requests(self):
        frames = self.exchange([
            {'type': 'load_earlier', 'channel': 'missing', 'before_id': 999},
            {'type': 'load_earlier', 'channel': 'long', 'before_id': 'bad'},
            {'type': 'load_earlier', 'channel': 'long', 'before_id': 999, 'limit': 9999},
            {'type': 'load_earlier', 'channel': 'general', 'before_id': 0, 'limit': 50},
        ])
        pages = [f for f in frames if f['type'] == 'history_earlier']
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(pages[0]['messages']), 200)
        self.assertTrue(pages[0]['has_more'])
        self.assertEqual(pages[1]['messages'], [])
        self.assertFalse(pages[1]['has_more'])

    def test_new_live_message_does_not_move_the_older_cursor(self):
        cursor = self.saved['long'][-50]['id']
        live = self.messages.add('Ben', 'new live message', channel='long', _bulk=True)
        frames = self.exchange([{'type': 'load_earlier', 'channel': 'long', 'before_id': cursor}])
        page = next(f for f in frames if f['type'] == 'history_earlier')
        self.assertEqual([m['id'] for m in page['messages']], [m['id'] for m in self.saved['long'][-100:-50]])
        self.assertNotIn(live['id'], [m['id'] for m in page['messages']])

