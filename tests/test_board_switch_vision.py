"""Offline admission/transaction regressions: no cloud, audio or real board I/O."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pc_backend.server import (ai_chat, configure_vision, selftest_voice,
                               set_config, start_recording, start_voice)
from pc_backend.voice_bridge import VoiceBridge
from pc_backend.voice_protocol import VoiceError

ROOT = Path(__file__).resolve().parent.parent


class Body:
    def __init__(self, state, payload, gated=False):
        self.app = {'console': state}
        self.payload = payload
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        if not gated:
            self.release.set()

    async def json(self):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def state():
    result = SimpleNamespace(board_url='http://192.168.1.2', board_revision=0,
        board={'boot_id': 'old-board', 'audio': {'ready': True}, 'speaker': {'ready': True}},
        board_seen=123, recording=None, record_pending=False, record_lock=asyncio.Lock(),
        voice=SimpleNamespace(active=False, start=AsyncMock(return_value={'active': True})),
        ai_lock=asyncio.Lock(), board_switch_lock=asyncio.Lock(), switching_board=False,
        fleet=None, audio_task=None, audio_event=asyncio.Event(), viewers=set(),
        vision=SimpleNamespace(close=AsyncMock(), start=AsyncMock(), configure=AsyncMock()),
        camera=SimpleNamespace(reset=AsyncMock()), ensure_audio=Mock(),
        log_event=Mock())
    result.status = lambda: {'board_connected': True}
    return result


class BoardSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT/'.cache/tmp')
        self.root = Path(self.directory.name)
        (self.root/'local').mkdir()
        self.root_patch = patch('pc_backend.server.ROOT', self.root)
        self.root_patch.start()
        self.state = state()

    async def asyncTearDown(self):
        self.root_patch.stop()
        self.directory.cleanup()

    def config_request(self, payload=None, gated=False):
        return Body(self.state, payload if payload is not None else {'board_url': 'http://192.168.1.3'}, gated)

    def assert_released(self):
        self.assertFalse(self.state.switching_board)
        self.assertFalse(self.state.board_switch_lock.locked())

    def assert_old_connection_untouched(self):
        self.assertEqual(self.state.board_url, 'http://192.168.1.2')
        self.assertEqual(self.state.board['boot_id'], 'old-board')
        self.state.vision.close.assert_not_awaited()
        self.state.camera.reset.assert_not_awaited()
        self.assertFalse((self.root/'local/pc.json').exists())

    async def test_config_reserves_before_json_and_blocks_chat_voice_selftest_recording_and_second_switch(self):
        request = self.config_request(gated=True)
        switching = asyncio.create_task(set_config(request))
        try:
            await asyncio.wait_for(request.entered.wait(), 1)
            self.assertTrue(self.state.switching_board)
            self.assertTrue(self.state.board_switch_lock.locked())
            with patch('pc_backend.server.ai_status') as ai_status, \
                    patch('pc_backend.server.stream_reply') as reply:
                for handler, payload in ((ai_chat, {'text': 'question'}),
                                         (start_voice, {}), (selftest_voice, {}),
                                         (start_recording, {}), (set_config, {'board_url':'http://192.168.1.4'})):
                    with self.subTest(handler=handler.__name__):
                        blocked = Body(self.state, payload)
                        response = await handler(blocked)
                        self.assertEqual(response.status, 409)
                        self.assertEqual(blocked.calls, 0)
                ai_status.assert_not_called()
                reply.assert_not_called()
            self.state.voice.start.assert_not_awaited()
            self.assertFalse(self.state.record_pending)
            self.assert_old_connection_untouched()
            request.release.set()
            response = await switching
            self.assertEqual(response.status, 200)
            self.assertEqual(self.state.board_url, 'http://192.168.1.3')
            self.assertEqual(self.state.board, {})
            self.assertEqual(self.state.board_revision, 1)
            self.assertEqual(json.loads((self.root/'local/pc.json').read_text()),
                             {'board_url':'http://192.168.1.3'})
            self.state.vision.close.assert_awaited_once()
            self.state.camera.reset.assert_awaited_once()
            self.state.vision.start.assert_awaited_once()
            self.assert_released()
        finally:
            request.release.set()
            if not switching.done():
                switching.cancel()
            await asyncio.gather(switching, return_exceptions=True)

    async def test_existing_text_voice_or_recording_rejects_switch_before_reading_body(self):
        for busy in ('text', 'voice', 'recording', 'record_pending'):
            with self.subTest(busy=busy):
                self.state = state()
                if busy == 'text':
                    await self.state.ai_lock.acquire()
                elif busy == 'voice':
                    self.state.voice.active = True
                elif busy == 'recording':
                    self.state.recording = object()
                else:
                    self.state.record_pending = True
                request = self.config_request()
                response = await set_config(request)
                self.assertEqual(response.status, 409)
                self.assertEqual(request.calls, 0)
                self.assert_released()
                self.assert_old_connection_untouched()

    async def test_busy_state_is_rechecked_after_awaiting_config_body(self):
        for busy in ('text', 'voice'):
            with self.subTest(busy=busy):
                self.state = state()
                request = self.config_request(gated=True)
                task = asyncio.create_task(set_config(request))
                await asyncio.wait_for(request.entered.wait(), 1)
                if busy == 'text':
                    await self.state.ai_lock.acquire()
                else:
                    self.state.voice.active = True
                request.release.set()
                self.assertEqual((await task).status, 409)
                self.assert_released()
                self.assert_old_connection_untouched()

    async def test_invalid_body_or_address_keeps_old_camera_and_vision_connections(self):
        for payload in ([], {}, {'board_url': 123}, {'board_url': 'https://example.com'},
                        ValueError('invalid JSON fixture')):
            with self.subTest(kind=type(payload).__name__):
                self.state = state()
                with self.assertRaises(ValueError):
                    await set_config(Body(self.state, payload))
                self.assert_released()
                self.assert_old_connection_untouched()

    async def test_canceled_body_releases_flag_and_lock_without_touching_connections(self):
        request = self.config_request(gated=True)
        task = asyncio.create_task(set_config(request))
        await asyncio.wait_for(request.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()
        self.assert_old_connection_untouched()
        self.assertEqual((await set_config(self.config_request())).status, 200)

    async def test_camera_reset_failure_releases_transaction_and_restarts_old_vision_watcher(self):
        self.state.camera.reset.side_effect = RuntimeError('offline reset failure')
        with self.assertRaisesRegex(RuntimeError, 'reset failure'):
            await set_config(self.config_request())
        self.assert_released()
        self.assertEqual(self.state.board_url, 'http://192.168.1.2')
        self.assertEqual(self.state.board_revision, 0)
        self.assertFalse((self.root/'local/pc.json').exists())
        self.state.vision.start.assert_awaited_once()
        self.state.camera.reset.side_effect = None
        self.assertEqual((await set_config(self.config_request())).status, 200)

    async def test_cancellation_during_reset_releases_transaction_and_restores_local_watcher(self):
        entered = asyncio.Event()
        async def reset():
            entered.set()
            await asyncio.Event().wait()
        self.state.camera.reset.side_effect = reset
        task = asyncio.create_task(set_config(self.config_request()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()
        self.assertEqual(self.state.board_url, 'http://192.168.1.2')
        self.state.vision.start.assert_awaited_once()

    async def test_persistence_failure_keeps_the_previous_board_address(self):
        with patch('pc_backend.server.ROOT', self.root/'missing-directory'):
            with self.assertRaises(OSError):
                await set_config(self.config_request())
        self.assert_released()
        self.assertEqual(self.state.board_url, 'http://192.168.1.2')
        self.assertEqual(self.state.board_revision, 0)
        self.state.vision.start.assert_awaited_once()

    async def test_chat_body_already_in_flight_is_rejected_during_switch_without_model_or_key_access(self):
        chat = Body(self.state, {'text': 'question'}, gated=True)
        config = self.config_request(gated=True)
        with patch('pc_backend.server.ai_status') as ai_status, \
                patch('pc_backend.server.stream_reply') as reply:
            chat_task = asyncio.create_task(ai_chat(chat))
            await asyncio.wait_for(chat.entered.wait(), 1)
            config_task = asyncio.create_task(set_config(config))
            await asyncio.wait_for(config.entered.wait(), 1)
            chat.release.set()
            self.assertEqual((await chat_task).status, 409)
            ai_status.assert_not_called()
            reply.assert_not_called()
            config.release.set()
            self.assertEqual((await config_task).status, 200)
        self.assert_released()

    async def test_chat_body_cannot_resume_against_another_board_after_switch_completed(self):
        chat = Body(self.state, {'text': 'question'}, gated=True)
        with patch('pc_backend.server.ai_status') as ai_status:
            chat_task = asyncio.create_task(ai_chat(chat))
            await asyncio.wait_for(chat.entered.wait(), 1)
            self.assertEqual((await set_config(self.config_request())).status, 200)
            chat.release.set()
            self.assertEqual((await chat_task).status, 409)
            ai_status.assert_not_called()

    async def test_recording_reserves_pending_state_while_reading_json_and_cleans_up_on_cancel(self):
        recording = Body(self.state, {'label': 'fixture'}, gated=True)
        task = asyncio.create_task(start_recording(recording))
        await asyncio.wait_for(recording.entered.wait(), 1)
        self.assertTrue(self.state.record_pending)
        config = self.config_request()
        self.assertEqual((await set_config(config)).status, 409)
        self.assertEqual(config.calls, 0)
        self.state.ensure_audio.assert_not_called()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.state.record_pending)
        self.assertFalse(self.state.record_lock.locked())
        self.assert_old_connection_untouched()

    async def test_vision_configuration_rechecks_new_text_voice_or_switch_after_body_wait(self):
        for busy in ('text', 'voice', 'switch'):
            with self.subTest(busy=busy):
                self.state = state()
                request = Body(self.state, {'enabled': False}, gated=True)
                task = asyncio.create_task(configure_vision(request))
                await asyncio.wait_for(request.entered.wait(), 1)
                if busy == 'text':
                    await self.state.ai_lock.acquire()
                elif busy == 'voice':
                    self.state.voice.active = True
                else:
                    self.state.switching_board = True
                request.release.set()
                self.assertEqual((await task).status, 409)
                self.state.vision.configure.assert_not_awaited()

    async def test_direct_voice_start_checks_transition_before_configuration_or_connections(self):
        self.state.switching_board = True
        bridge = VoiceBridge(self.state)
        with patch('pc_backend.voice_bridge.load_config') as load_config:
            with self.assertRaisesRegex(VoiceError, '切换开发板'):
                await bridge.start(protocol_test=True)
            load_config.assert_not_called()
        self.assertFalse(bridge.active)
        self.assertIsNone(bridge.task)

    async def test_voice_start_waiting_for_lifecycle_rechecks_switch_and_finished_switch_revision(self):
        for transition in ('switching', 'already_switched'):
            with self.subTest(transition=transition):
                self.state = state()
                bridge = VoiceBridge(self.state)
                await bridge.lifecycle_lock.acquire()
                with patch('pc_backend.voice_bridge.load_config') as load_config:
                    task = asyncio.create_task(bridge.start(protocol_test=True))
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    if transition == 'switching':
                        self.state.switching_board = True
                    else:
                        self.state.board_revision += 1
                    bridge.lifecycle_lock.release()
                    with self.assertRaisesRegex(VoiceError, '切换开发板'):
                        await task
                    load_config.assert_not_called()
                self.assertFalse(bridge.active)


if __name__ == '__main__':
    unittest.main()
