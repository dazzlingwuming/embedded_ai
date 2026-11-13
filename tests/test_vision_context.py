import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pc_backend.vision_context import VisionContext, prepare_for_voice
from pc_backend.server import configure_vision, get_vision_status, vision_frame, ai_chat

ROOT = Path(__file__).resolve().parent.parent


class FakeSelection:
    def __init__(self):
        self.images = [{'jpeg': b'camera-test-bytes', 'frame_id': 'frame-42',
            'received_utc': '2026-11-13T00:00:00Z', 'age_seconds': .2,
            'sha256': 'test-hash', 'quality': {'low_texture': True}}]

    def public(self):
        return {'selection_id': 'test-selection-42', 'candidate_count': 5,
            'selected': [{key: value for key, value in self.images[0].items() if key != 'jpeg'}],
            'reason': 'selected', 'warnings': ['low_texture']}


class VisionContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT/'.cache/tmp')
        camera = SimpleNamespace(ready=True, consumers={}, task=None, error=None,
            acquire=AsyncMock(), release=AsyncMock())
        self.console = SimpleNamespace(camera=camera, board_seen=time.monotonic(),
            board_url='http://192.168.2.38', board={'boot_id': 'test'},
            ai_lock=asyncio.Lock(), voice=SimpleNamespace(active=False))
        self.vision = VisionContext(self.console, Path(self.temporary.name)/'vision.json')
        self.vision.last_board = self.vision.board_identity()
        self.console.vision = self.vision
        self.vision.sampler = SimpleNamespace(select=AsyncMock(return_value=FakeSelection()),
            status=lambda: {}, start=AsyncMock(), stop=AsyncMock(), offer=lambda *args: None)

    async def asyncTearDown(self):
        await self.vision.close()
        self.temporary.cleanup()

    async def test_question_selects_one_image_and_preview_serves_the_exact_same_bytes(self):
        prepared = await self.vision.prepare('text', 'turn-1')
        self.vision.sampler.select.assert_awaited_once_with(window_seconds=3.0, max_frames=1, timeout_seconds=2.0)
        frame = prepared.public['selected'][0]
        self.assertEqual(self.vision.image(prepared.public['selection_id'], frame['frame_id']), prepared.images[0]['jpeg'])
        self.assertNotIn('jpeg', repr(prepared.public))
        self.assertIn('/api/vision/frame/', frame['image_url'])
        self.assertEqual(prepared.public['phase'], 'selected')

    async def test_offline_camera_and_disabled_mode_never_select_or_supply_old_images(self):
        prepared = await self.vision.prepare('voice')
        self.assertTrue(prepared.images)
        self.console.board_seen = time.monotonic()-8
        missing = await self.vision.prepare('voice')
        self.assertEqual(missing.images, [])
        self.assertEqual(missing.public['reason'], 'camera_unavailable')
        self.assertIn('本轮没有可用', missing.instructions)
        self.vision.enabled = False
        disabled = await self.vision.prepare('voice')
        self.assertEqual(disabled.images, [])
        self.assertEqual(disabled.instructions, '')
        self.assertEqual(self.vision.sampler.select.await_count, 1)

    async def test_phase_from_old_concurrent_question_does_not_replace_new_selection(self):
        first = await self.vision.prepare('voice', 'old')
        self.vision.sampler.select.return_value.public = lambda: {
            'selection_id': 'new-selection', 'selected': [], 'candidate_count': 0,
            'reason': 'no_frames', 'warnings': []}
        self.vision.sampler.select.return_value.images = []
        await self.vision.prepare('text', 'new')
        first.mark('completed')
        self.assertEqual(self.vision.last_selection['turn_id'], 'new')

    async def test_disable_while_selection_is_pending_discards_the_result(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def select(**kwargs):
            started.set()
            await finish.wait()
            return FakeSelection()
        self.vision.sampler.select.side_effect = select
        task = asyncio.create_task(self.vision.prepare('voice'))
        await started.wait()
        disable = asyncio.create_task(self.vision.configure(False))
        await asyncio.sleep(0)
        finish.set()
        await disable
        prepared = await task
        self.assertEqual(prepared.images, [])
        self.assertEqual(prepared.public['reason'], 'vision_changed')
        self.assertIsNone(self.vision.last_selection)

    async def test_new_question_during_cache_invalidation_cannot_adopt_old_frames(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def release(name):
            entered.set()
            await finish.wait()
        self.console.camera.release.side_effect = release
        clear = asyncio.create_task(self.vision._clear())
        await entered.wait()
        prepared = await self.vision.prepare('text')
        self.assertEqual(prepared.images, [])
        self.assertEqual(prepared.public['reason'], 'vision_changed')
        self.vision.sampler.select.assert_not_awaited()
        finish.set()
        await clear

    async def test_board_change_or_disconnect_during_selection_discards_image(self):
        for change in ('boot', 'disconnect'):
            with self.subTest(change=change):
                self.console.board = {'boot_id': 'before'}
                self.console.board_seen = time.monotonic()
                self.vision.last_board = self.vision.board_identity()
                async def select(**kwargs):
                    if change == 'boot':
                        self.console.board = {'boot_id': 'after'}
                    else:
                        self.console.board_seen = time.monotonic()-9
                    return FakeSelection()
                self.vision.sampler.select.side_effect = select
                prepared = await self.vision.prepare('voice')
                self.assertEqual(prepared.images, [])
                self.assertEqual(prepared.public['reason'], 'vision_changed')

    async def test_reboot_before_watcher_runs_cannot_reuse_previous_boot_frames(self):
        self.console.board = {'boot_id': 'new-boot-not-seen-by-watcher'}
        prepared = await self.vision.prepare('voice')
        self.assertEqual(prepared.images, [])
        self.assertEqual(prepared.public['reason'], 'vision_changed')
        self.vision.sampler.select.assert_not_awaited()

    async def test_voice_input_gets_selection_metadata_without_image_bytes(self):
        bridge = SimpleNamespace(console=self.console, metrics={}, messages=[
            {'role': 'user', 'text': '这是什么'}])
        prepared = await prepare_for_voice(bridge, '这是什么', 'voice-turn')
        self.assertEqual(bridge.messages[0]['vision']['turn_id'], 'voice-turn')
        self.assertEqual(bridge.metrics['vision']['source'], 'voice')
        self.assertNotIn('camera-test-bytes', repr(bridge.metrics))
        self.assertEqual(len(prepared.images), 1)

    async def test_model_request_uses_same_image_as_sse_and_local_preview(self):
        actual = []
        async def fake_reply(text, history=None, **kwargs):
            actual.append((text, deepcopy(kwargs)))
            yield '这是测试回答'
        app = web.Application()
        app['console'] = self.console
        app.router.add_post('/api/ai/chat', ai_chat)
        app.router.add_get('/api/vision/status', get_vision_status)
        app.router.add_post('/api/vision/config', configure_vision)
        app.router.add_get('/api/vision/frame/{selection}/{frame}.jpg', vision_frame)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch('pc_backend.server.ai_status', return_value={'configured': True}), \
                    patch('pc_backend.server.stream_reply', fake_reply):
                response = await client.post('/api/ai/chat', json={'text': '我手里是什么'})
                stream = await response.text()
            self.assertIn('"vision"', stream)
            self.assertIn('"done": true', stream)
            self.assertEqual(actual[0][1]['images'][0]['jpeg'], b'camera-test-bytes')
            url = self.vision.last_selection['selected'][0]['image_url']
            photo = await client.get(url)
            self.assertEqual(await photo.read(), actual[0][1]['images'][0]['jpeg'])
            self.assertEqual(photo.headers['Cache-Control'], 'no-store')
            missing = await client.get('/api/vision/frame/unknown/frame.jpg')
            self.assertEqual(missing.status, 404)
            self.console.voice.active = True
            refused = await client.post('/api/vision/config', json={'enabled': False})
            self.assertEqual(refused.status, 409)
        finally:
            await client.close()
