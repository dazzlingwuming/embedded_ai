import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pc_backend.chained_voice import ChainedVoiceBridge
from pc_backend.cloud_speech import asr_request, tts_request
from pc_backend.voice_config import DEFAULTS


def bridge():
    result = ChainedVoiceBridge(SimpleNamespace())
    result.config = DEFAULTS.copy()
    result.active, result.phase, result.source = True, 'listening', 'cloud_asr_deepseek_tts'
    result.metrics = {'late_audio_discarded': 0, 'downlink_bytes': 0,
                      'interruptions': 0, 'mic_drops': 0, 'input_discontinuities': 0}
    return result


class ProtocolTests(unittest.TestCase):
    def test_asr_only_requests_recognition_and_tts_only_requests_synthesis(self):
        recognition, synthesis = asr_request(DEFAULTS, 'asr-id'), tts_request(DEFAULTS, 'tts-id')
        self.assertEqual(recognition['payload']['task'], 'asr')
        self.assertEqual(recognition['payload']['model'], 'fun-asr-realtime')
        self.assertEqual(recognition['payload']['parameters']['sample_rate'], 16000)
        self.assertTrue(recognition['payload']['parameters']['heartbeat'])
        self.assertEqual(synthesis['payload']['task'], 'tts')
        self.assertEqual(synthesis['payload']['model'], 'cosyvoice-v3-flash')
        self.assertEqual(synthesis['payload']['parameters']['format'], 'pcm')
        self.assertEqual(synthesis['payload']['parameters']['sample_rate'], 24000)
        self.assertNotIn('modalities', recognition['payload'])


class ChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_final_recognition_can_trigger_a_deepseek_turn(self):
        session = bridge()
        await session.accept_sentence({'sentence_id': 1, 'text': '你好', 'sentence_end': False})
        self.assertTrue(session.turns.empty())
        await session.accept_sentence({'sentence_id': 1, 'text': '你好世界', 'sentence_end': True, 'end_time': 900})
        self.assertEqual(session.turns.get_nowait(), '你好世界')
        session.resume_listening()
        await session.accept_sentence({'sentence_id': 1, 'text': '你好世界', 'sentence_end': True, 'end_time': 900})
        self.assertTrue(session.turns.empty())

    async def test_late_pre_interrupt_recognition_does_not_start_a_new_answer(self):
        session = bridge()
        session.asr_floor_ms = 2000
        await session.accept_sentence({'sentence_id': 1, 'text': '旧句子', 'sentence_end': True, 'end_time': 1900})
        self.assertTrue(session.turns.empty())

    async def test_thinking_uploads_silence_instead_of_microphone_content(self):
        session = bridge(); session.phase = 'thinking'
        session.asr = SimpleNamespace(send_pcm=AsyncMock(side_effect=asyncio.CancelledError))
        session.mic_queue.put_nowait((1, b'\x7f\x7f'*960, time.monotonic()))
        with self.assertRaises(asyncio.CancelledError):
            await session.asr_uplink()
        session.asr.send_pcm.assert_awaited_once_with(b'\0\0'*320)

    async def test_resuming_after_playback_is_not_treated_as_microphone_packet_loss(self):
        session = bridge(); session.metrics['uplink_bytes'] = 0
        sent = []
        async def send(pcm):
            sent.append(pcm)
            if len(sent) == 1:
                session.phase = 'speaking'
            elif len(sent) == 2:
                session.phase = 'listening'
                session.mic_queue.put_nowait((500, b'\x01\0'*960, time.monotonic()))
            elif len(sent) == 3:
                raise asyncio.CancelledError()
        session.asr = SimpleNamespace(send_pcm=send)
        session.mic_queue.put_nowait((1, b'\x01\0'*960, time.monotonic()))
        with self.assertRaises(asyncio.CancelledError):
            await session.asr_uplink()
        self.assertEqual(len(sent),3)
        self.assertEqual(sent[1],b'\0\0'*1600)

    async def test_asr_text_goes_to_deepseek_and_only_its_reply_goes_to_tts(self):
        session = bridge(); seen = {'tts_text': []}
        finished = asyncio.Event()

        class TTS:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send_text(self, text):
                if text: seen['tts_text'].append(text)
            async def finish_text(self): finished.set()
            async def events(self):
                await finished.wait()
                yield b'\x01\0'*240
                yield {'header': {'event': 'task-finished'}, 'payload': {}}

        async def deepseek(text, history, system_prompt=None):
            seen['deepseek_input'] = text
            yield '这是 DeepSeek '
            yield '生成的回答。'

        with patch('pc_backend.chained_voice.SpeechTask', return_value=TTS()), \
             patch('pc_backend.chained_voice.stream_reply', deepseek):
            await session.reply_turn(None, '这是 ASR 识别的提问')
        self.assertEqual(seen['deepseek_input'], '这是 ASR 识别的提问')
        self.assertEqual(''.join(seen['tts_text']), '这是 DeepSeek 生成的回答。')
        self.assertTrue(session.llm_verified and session.tts_verified)
        self.assertEqual(session.history[-1]['content'], '这是 DeepSeek 生成的回答。')
        self.assertEqual(session.play_queue.qsize(), 3)  # begin, actual PCM, end

    async def test_interruption_cancels_llm_tts_turn_but_keeps_call_available(self):
        session = bridge(); session.phase = 'speaking'; session.current_response = 'old'
        session.speaker = SimpleNamespace(cancel=AsyncMock(), total_sent=0)
        session.turn_task = asyncio.create_task(asyncio.Event().wait())
        await session.interrupt()
        self.assertTrue(session.turn_task.cancelled())
        session.speaker.cancel.assert_awaited_once()
        self.assertEqual(session.phase, 'listening')
        self.assertTrue(session.active)
        self.assertIn('old', session.ignored_responses)


if __name__ == '__main__':
    unittest.main()
