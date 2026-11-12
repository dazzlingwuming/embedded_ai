from array import array
import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pc_backend.voice_config import DEFAULTS, public_config, save_config, session_update, validate_host, load_config, read_key
from pc_backend.voice_protocol import PCMConverter, MIC_HEADER, SPEAKER_HEADER, VoiceError, speaker_frame, decode_cloud_audio
from pc_backend.voice_bridge import VoiceBridge


class PCMTests(unittest.TestCase):
    def test_streaming_rate_conversion_preserves_irregular_byte_boundaries(self):
        pcm = array('h', range(-2400, 2400)).tobytes()
        reference = PCMConverter(24000, 48000).convert(pcm)
        converter = PCMConverter(24000, 48000)
        actual = b''.join(converter.convert(pcm[i:i+173]) for i in range(0, len(pcm), 173))
        self.assertEqual(actual, reference)
        self.assertEqual(len(actual)//2, 9599)
        self.assertFalse(converter.pending)

    def test_microphone_format_conversion_does_not_change_original_bytes(self):
        pcm = array('h', [100] * 960).tobytes()
        converted = PCMConverter(48000, 16000).convert(pcm)
        self.assertEqual(len(converted), 640)
        self.assertEqual(pcm, array('h', [100] * 960).tobytes())

    def test_speaker_wire_format_is_epoch_tagged_and_rejects_partial_samples(self):
        packet = speaker_frame(91, 7, b'\x01\0' * 960)
        self.assertEqual(SPEAKER_HEADER.unpack_from(packet), (b'BLP1', 91, 7, 960, 0))
        for bad in (b'', b'x', b'\0' * 1922):
            with self.assertRaises(VoiceError):
                speaker_frame(91, 7, bad)

    def test_invalid_cloud_audio_is_not_forwarded_to_dac(self):
        with self.assertRaises(VoiceError):
            decode_cloud_audio({'delta': 'invalid!base64'})


class VoiceConfigTests(unittest.TestCase):
    def test_shared_env_reads_only_named_aliyun_key(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(dir=root/'.cache/tmp') as temporary:
            shared = Path(temporary)/'.env'
            with patch('pc_backend.voice_config.KEY_PATH', Path(temporary)/'missing.env'), \
                 patch('pc_backend.voice_config.SHARED_KEY_PATH', shared):
                shared.write_text('api_key:sk-deepseek-fixture\n', encoding='utf-8')
                self.assertEqual(read_key(), '')
                shared.write_text('api_key:sk-deepseek-fixture\naliyun_key:sk-ws-test.only.not-a-real-key\n', encoding='utf-8')
                self.assertEqual(read_key(), 'sk-ws-test.only.not-a-real-key')
    def test_real_key_never_appears_in_public_configuration(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(dir=root / '.cache' / 'tmp') as temporary:
            config = Path(temporary)/'realtime.json'
            key_file = Path(temporary)/'realtime.env'
            with patch('pc_backend.voice_config.CONFIG_PATH', config), patch('pc_backend.voice_config.KEY_PATH', key_file):
                public = save_config({'api_host': 'example.cn-beijing.maas.aliyuncs.com',
                                      'api_key': 'sk-test.workspace.signature-fixture-only'})
                self.assertTrue(public['configured'])
                self.assertNotIn('sk-test', repr(public))
                self.assertNotIn('sk-test', config.read_text(encoding='utf-8'))
                self.assertTrue(public_config()['key_present'])

    def test_cloud_key_cannot_be_sent_to_arbitrary_hosts(self):
        for host in ('evil.example', 'maas.aliyuncs.com.evil.example', 'https://example.cn-beijing.maas.aliyuncs.com',
                     'user:secret@dashscope.aliyuncs.com', '127.0.0.1'):
            with self.assertRaises(VoiceError):
                validate_host(host)

    def test_protocol_fixture_declares_pcm_rates(self):
        session = session_update(DEFAULTS)['session']
        self.assertEqual(session['audio']['input']['format'], {'type': 'pcm', 'sample_rate': 16000})
        self.assertEqual(session['audio']['output']['format'], {'type': 'pcm', 'sample_rate': 24000})
        self.assertEqual(session['audio']['output']['voice'], 'protocol-test-tone')
        self.assertEqual(session['turn_detection']['type'], 'server_vad')
        self.assertNotIn('tools', session)

    def test_native_speech_llm_provider_is_not_enabled(self):
        with self.assertRaises(VoiceError):
            save_config({'provider': 'qwen'})

    def test_hand_edited_configuration_cannot_bypass_volume_limit(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(dir=root / '.cache' / 'tmp') as temporary:
            config = Path(temporary)/'realtime.json'
            config.write_text(json.dumps({'volume': 101}))
            with patch('pc_backend.voice_config.CONFIG_PATH', config):
                with self.assertRaises(VoiceError):
                    load_config()
                self.assertFalse(public_config()['configured'])
                self.assertEqual(save_config({'volume': 20})['volume'], 20)


class FakeConsole:
    def __init__(self):
        self.board = {'speaker': {'ready': True}}
        self.recording = self.record_pending = False
        self.ensure_audio = lambda: None

    def status(self):
        return {'board_connected': True}


class VoiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_speech_key_does_not_open_any_connection(self):
        bridge = VoiceBridge(FakeConsole())
        with patch('pc_backend.voice_bridge.load_config', return_value={**DEFAULTS, 'api_key': ''}), \
             patch('pc_backend.voice_bridge.BoardSpeaker') as speaker:
            with self.assertRaises(VoiceError):
                await bridge.start()
            speaker.assert_not_called()
        self.assertFalse(bridge.active)

    async def test_interrupt_flushes_audio_and_discards_late_cloud_output(self):
        bridge = VoiceBridge(FakeConsole())
        bridge.active = True
        bridge.config = DEFAULTS.copy()
        bridge.metrics = {'interruptions': 0, 'late_audio_discarded': 0}
        bridge.current_response = 'old-response'
        bridge.response_open = True
        bridge.speaker = type('Speaker', (), {'cancel': AsyncMock(), 'total_sent': 0})()
        bridge.send_cloud = AsyncMock()
        bridge.play_queue.put_nowait(('audio', 0, 'old-response', b'\0\0'))
        await bridge.interrupt()
        self.assertTrue(bridge.play_queue.empty())
        await bridge.handle_event({'type': 'response.audio.delta', 'response_id': 'old-response',
                                   'delta': base64.b64encode(b'\0\0').decode()})
        self.assertTrue(bridge.play_queue.empty())
        self.assertEqual(bridge.metrics['late_audio_discarded'], 1)
        bridge.send_cloud.assert_awaited_once_with({'type': 'response.cancel'})
        bridge.speaker.cancel.assert_awaited_once()

    async def test_playback_suppresses_uplink_without_changing_capture_packet(self):
        bridge = VoiceBridge(FakeConsole())
        bridge.active, bridge.phase = True, 'speaking'
        bridge.metrics = {'mic_frames': 0, 'suppressed_mic_frames': 0, 'mic_drops': 0}
        packet = MIC_HEADER.pack(b'BLA1', 1, 20000, 48000, 960, 0) + b'\x01\0' * 960
        before = packet
        bridge.offer_audio(packet)
        self.assertTrue(bridge.mic_queue.empty())
        self.assertEqual(bridge.metrics['suppressed_mic_frames'], 1)
        self.assertEqual(packet, before)

    async def test_late_old_response_done_does_not_close_new_response(self):
        bridge = VoiceBridge(FakeConsole())
        bridge.current_response, bridge.response_open = 'new', True
        bridge.ignored_responses.add('old')
        await bridge.handle_event({'type': 'response.done', 'response': {'id': 'old', 'status': 'cancelled'}})
        self.assertTrue(bridge.response_open)

    async def test_cloud_cancel_failure_still_mutes_board_and_ends_session(self):
        bridge = VoiceBridge(FakeConsole())
        bridge.active, bridge.response_open = True, True
        bridge.config = DEFAULTS.copy()
        bridge.metrics = {'interruptions': 0}
        bridge.send_cloud = AsyncMock(side_effect=OSError('connection lost'))
        bridge.speaker = type('Speaker', (), {'cancel': AsyncMock(), 'total_sent': 0})()
        with self.assertRaises(VoiceError):
            await bridge.interrupt()
        bridge.speaker.cancel.assert_awaited_once()
        self.assertTrue(bridge.stop_event.is_set())
        self.assertEqual(bridge.phase, 'error')

    async def test_interrupt_during_echo_tail_does_not_finish_old_turn(self):
        bridge = VoiceBridge(FakeConsole())
        bridge.active, bridge.phase, bridge.source = True, 'speaking', 'protocol_test'
        bridge.config = {**DEFAULTS, 'echo_tail_ms': 100}
        bridge.metrics = {'interruptions': 0}
        bridge.speaker = type('Speaker', (), {
            'begin': AsyncMock(return_value=4), 'finish': AsyncMock(), 'cancel': AsyncMock(), 'total_sent': 0})()
        bridge.play_queue.put_nowait(('begin', 0, 'old', b''))
        bridge.play_queue.put_nowait(('end', 0, 'old', b''))
        task = asyncio.create_task(bridge.playback())
        try:
            while not bridge.speaker.finish.await_count:
                await asyncio.sleep(.001)
            await bridge.interrupt()
            await asyncio.sleep(.13)
            self.assertFalse(bridge.stop_event.is_set())
            self.assertIsNone(bridge.reason)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stopping_finished_self_test_preserves_result(self):
        bridge = VoiceBridge(FakeConsole())
        bridge.reason = 'self_test_complete'
        await bridge.stop()
        self.assertEqual(bridge.reason, 'self_test_complete')

    async def test_microphone_disconnect_cancels_call_instead_of_resuming_upload(self):
        bridge = VoiceBridge(FakeConsole())
        bridge.active = True
        bridge.task = asyncio.create_task(asyncio.Event().wait())
        bridge.input_failed()
        await asyncio.gather(bridge.task, return_exceptions=True)
        self.assertTrue(bridge.task.cancelled())
        self.assertTrue(bridge.stop_event.is_set())
        self.assertEqual(bridge.reason, 'connection_error')


if __name__ == '__main__':
    unittest.main()
