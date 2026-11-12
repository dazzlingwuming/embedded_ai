import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from pc_backend.camera_relay import CameraRelay, JPEGStream, MAX_FRAME, valid_photo_name, frame_timing

ROOT = Path(__file__).resolve().parent.parent


class CameraFrameTests(unittest.TestCase):
    def test_frame_timing_exposes_a_stall_hidden_by_average_fps(self):
        times = [n*.05 for n in range(150)] + [8.5+n*.05 for n in range(150)]
        stats = frame_timing(times)
        self.assertGreater(stats['max_interval_ms'], 1000)
        self.assertEqual(stats['interval_p95_ms'], 50)
        self.assertLess(stats['low_1pct_fps'], 5)
        self.assertIsNone(frame_timing([0,.05])['low_1pct_fps'])
    def test_tcp_and_multipart_boundaries_do_not_change_jpeg_frames(self):
        first, second = b'\xff\xd8first\xff\xd9', b'\xff\xd8second\xff\xd9'
        stream = b'--boundary\r\nContent-Type: image/jpeg\r\n\r\n'+first+b'\r\n--boundary\r\n'+second
        parser, actual = JPEGStream(), []
        for offset in range(0, len(stream), 3):
            actual.extend(parser.feed(stream[offset:offset+3]))
        self.assertEqual(actual, [first, second])

    def test_incomplete_or_nonimage_stream_has_bounded_memory(self):
        for prefix in (b'header', b'\xff\xd8'):
            with self.assertRaises(ValueError):
                JPEGStream().feed(prefix+b'x'*(MAX_FRAME+1))

    def test_stale_frame_cannot_be_saved_as_a_current_photo(self):
        relay = CameraRelay(SimpleNamespace())
        relay.latest, relay.last_frame = b'\xff\xd8image\xff\xd9', time.monotonic()-4
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache/tmp') as temporary:
            with self.assertRaises(ValueError):
                relay.save_photo(Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])
            relay.last_frame = time.monotonic()
            saved = relay.save_photo(Path(temporary))
            self.assertEqual((Path(temporary)/saved['name']).read_bytes(), relay.latest)
            self.assertTrue(valid_photo_name(saved['name']))
            self.assertFalse(valid_photo_name('../api_key/.env'))


class CameraRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_quality_does_not_send_a_request_to_board(self):
        relay = CameraRelay(SimpleNamespace())
        for value in (True, 19, 91, '40', None):
            with self.assertRaises(ValueError):
                await relay.set_quality(value)

    async def asyncSetUp(self):
        self.connections = self.active_upstreams = 0
        self.disconnect = asyncio.Event()
        self.frame = b'\xff\xd8wire-fixture-not-a-photo\xff\xd9'

        async def stream(request):
            response = web.StreamResponse(headers={'Content-Type': 'multipart/x-mixed-replace;boundary=frame'})
            await response.prepare(request)
            self.connections += 1
            self.active_upstreams += 1
            try:
                while not self.disconnect.is_set():
                    await response.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+self.frame+b'\r\n')
                    await asyncio.sleep(.02)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            finally:
                self.active_upstreams -= 1
            return response

        upstream_app = web.Application()
        upstream_app.router.add_get('/stream', stream)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        self.http = ClientSession(trust_env=False)
        self.relay = CameraRelay(SimpleNamespace(board_url=f'http://127.0.0.1:{self.upstream.port}', http=self.http))
        self.relay.info = {'src': f':{self.upstream.port}/stream', 'currentResolution': {'width': 800, 'height': 800}}
        self.relay.info_seen = time.monotonic()
        local_app = web.Application()
        local_app.router.add_get('/ws/camera', self.relay.websocket)
        self.local = TestServer(local_app)
        await self.local.start_server()
        self.clients = []

    async def connect(self):
        ws = await self.http.ws_connect(self.local.make_url('/ws/camera'))
        self.clients.append(ws)
        self.assertEqual((await ws.receive_json())['source'], 'real_camera')
        return ws

    async def asyncTearDown(self):
        for ws in self.clients:
            await ws.close()
        await self.relay.reset()
        await self.local.close()
        await self.http.close()
        await self.upstream.close()

    async def test_two_viewers_share_one_upstream_and_last_pause_releases_it(self):
        first, second = await self.connect(), await self.connect()
        self.assertEqual(await first.receive_bytes(timeout=2), self.frame)
        self.assertEqual(await second.receive_bytes(timeout=2), self.frame)
        self.assertEqual(self.connections, 1)
        await first.close()
        self.assertEqual(await second.receive_bytes(timeout=2), self.frame)
        self.assertEqual(self.active_upstreams, 1)
        await second.close()
        for _ in range(30):
            if not self.relay.status()['running'] and self.active_upstreams == 0 and self.relay.latest is None:
                break
            await asyncio.sleep(.02)
        tasks = [(task.get_coro().__qualname__, [(frame.f_code.co_name, frame.f_lineno) for frame in task.get_stack()[-2:]])
                 for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        self.assertFalse(self.relay.status()['running'],
                         f'viewers={len(self.relay.viewers)} upstreams={self.active_upstreams} tasks={tasks}')
        self.assertEqual(self.active_upstreams, 0)
        self.assertIsNone(self.relay.latest)

    async def test_upstream_end_is_reported_to_viewer_instead_of_replaying_stale_frames(self):
        ws = await self.connect()
        await ws.receive_bytes(timeout=2)
        self.disconnect.set()
        async with asyncio.timeout(2):
            while True:
                message = await ws.receive()
                if message.type == web.WSMsgType.TEXT:
                    self.assertIn('已结束', message.json()['error'])
                    break


if __name__ == '__main__':
    unittest.main()
