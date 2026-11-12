"""Offline long-form and streaming regressions; no cloud or board connections."""
import asyncio
from collections import deque
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from pc_backend.chained_voice import ChainedVoiceBridge
from pc_backend.duplex_audio import take_sentence, timed_speech_events
from pc_backend.duplex_voice import DuplexVoiceBridge
from pc_backend.voice_config import DEFAULTS, validate_config
from pc_backend.voice_protocol import PlaybackInterrupted, VoiceError


class ImmediateSpeaker:
    def __init__(self):
        self.epoch = self.total_sent = self.bytes_sent = 0
        self.error = None
        self.first_send = asyncio.Event()
        self.maximum_packet = 0
        self.nonzero_sent = False

    async def begin(self, volume):
        self.epoch += 1
        return self.epoch

    async def send(self, pcm, epoch):
        if epoch != self.epoch:
            raise PlaybackInterrupted()
        self.total_sent += 1
        self.bytes_sent += len(pcm)
        self.maximum_packet = max(self.maximum_packet,len(pcm))
        self.nonzero_sent |= any(pcm)
        await asyncio.sleep(0)
        self.first_send.set()

    async def finish(self, epoch):
        if epoch != self.epoch:
            raise PlaybackInterrupted()

    async def cancel(self):
        self.epoch += 1


class TTSFactory:
    def __init__(self, chunks, preheated=None, wait_for_finish=False):
        self.chunks = chunks
        self.preheated = preheated
        self.wait_for_finish = wait_for_finish
        self.texts = []
        self.opened = self.closed = self.event_iterators_closed = 0

    def __call__(self,client,config,service):
        factory=self
        class TTS:
            def __init__(self):self.finished=asyncio.Event()
            async def __aenter__(self):
                factory.opened += 1
                if factory.preheated:factory.preheated.set()
                return self
            async def __aexit__(self,*args):factory.closed += 1
            async def send_text(self,text):
                if text:factory.texts.append(text)
            async def finish_text(self):self.finished.set()
            async def events(self):
                try:
                    if factory.wait_for_finish:await self.finished.wait()
                    for chunk in factory.chunks:
                        yield chunk
                    yield {'header':{'event':'task-finished'},'payload':{}}
                finally:factory.event_iterators_closed += 1
        return TTS()


class InspectQueue(asyncio.Queue):
    def __init__(self):
        super().__init__(maxsize=100)
        self.maximum_audio_item = 0
        self.maximum_size = 0

    async def put(self,item):
        if item[0] in ('audio','segment_audio'):
            self.maximum_audio_item=max(self.maximum_audio_item,len(item[3]))
        await super().put(item)
        self.maximum_size=max(self.maximum_size,self.qsize())


def session(duplex=True):
    result=(DuplexVoiceBridge if duplex else ChainedVoiceBridge)(SimpleNamespace())
    result.config={**DEFAULTS,'echo_tail_ms':100}
    result.active,result.phase,result.source=True,'listening','cloud_asr_deepseek_tts'
    if duplex:result.duplex_enabled=True
    result.metrics={'mic_frames':0,'uplink_bytes':0,'mic_drops':0,'downlink_bytes':0,
                    'speaker_frames':0,'interruptions':0,'late_audio_discarded':0,
                    'input_discontinuities':0}
    result.speaker=ImmediateSpeaker()
    result.play_queue=InspectQueue()
    return result


class LongVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_thirty_seconds_of_audio_is_played_with_byte_bounded_queue(self):
        voice=session()
        audio=b'\x01\0'*(24000*32)
        factory=TTSFactory([audio])
        async def llm(text,history,system_prompt=None):yield '这是一段完整的长音频。'
        player=asyncio.create_task(voice.playback())
        try:
            with patch('pc_backend.duplex_voice.SpeechTask',factory), \
                 patch('pc_backend.duplex_voice.stream_reply',llm):
                await asyncio.wait_for(voice.reply_turn(None,'完整朗读'),3)
            self.assertEqual(voice.response_bytes,len(audio))
            self.assertEqual(voice.metrics['downlink_bytes'],len(audio))
            self.assertEqual(voice.current_turn.heard,'这是一段完整的长音频。')
            self.assertEqual(voice.phase,'listening')
            self.assertLessEqual(voice.play_queue.maximum_audio_item,960)
            self.assertLessEqual(voice.play_queue.maximum_size,100)
            self.assertLessEqual(voice.speaker.maximum_packet,1920)
            self.assertGreaterEqual(voice.speaker.bytes_sent,48000*2*31)
        finally:
            player.cancel();await asyncio.gather(player,return_exceptions=True)

    async def test_long_text_is_preserved_in_synthesis_history_and_display(self):
        voice=session()
        output=('原文逐字保留，不省略任何内容。\n'*220)
        factory=TTSFactory([b'\x01\0'*480])
        async def llm(text,history,system_prompt=None):
            for index in range(0,len(output),7):yield output[index:index+7]
        player=asyncio.create_task(voice.playback())
        try:
            with patch('pc_backend.duplex_voice.SpeechTask',factory), \
                 patch('pc_backend.duplex_voice.stream_reply',llm):
                await asyncio.wait_for(voice.reply_turn(None,'请完整朗读全文'),3)
            self.assertGreater(len(output),3000)
            self.assertEqual(''.join(factory.texts).rstrip(),output.rstrip())
            self.assertEqual(voice.current_turn.generated,output)
            self.assertEqual(voice.history[-1]['content'],output)
            self.assertEqual(voice.messages[-1]['text'],output)
        finally:
            player.cancel();await asyncio.gather(player,return_exceptions=True)

    async def test_first_phrase_plays_before_the_model_iterator_finishes(self):
        voice=session()
        preheated=asyncio.Event();release_rest=asyncio.Event();model_done=asyncio.Event()
        factory=TTSFactory([b'\x02\0'*960],preheated=preheated)
        async def llm(text,history,system_prompt=None):
            # The LLM is deliberately paused until the independent TTS task warms.
            await preheated.wait()
            yield '豫章故郡，'
            await release_rest.wait()
            yield '洪都新府。后文继续完整朗读。'
            model_done.set()
        player=asyncio.create_task(voice.playback());reply=None
        try:
            with patch('pc_backend.duplex_voice.SpeechTask',factory), \
                 patch('pc_backend.duplex_voice.stream_reply',llm):
                reply=asyncio.create_task(voice.reply_turn(None,'朗读全文'))
                await asyncio.wait_for(voice.speaker.first_send.wait(),1)
                self.assertFalse(model_done.is_set())
                self.assertFalse(reply.done())
                self.assertTrue(voice.speaker.nonzero_sent)
                timing=voice.metrics['current_turn_timing']
                self.assertIsNone(timing['llm_stream_drained_ms'])
                self.assertIsNotNone(timing['first_speaker_send_ms'])
                self.assertLessEqual(timing['tts_preheat_ready_ms'],timing['llm_first_text_received_ms'])
                self.assertLessEqual(timing['first_tts_audio_received_ms'],timing['first_speaker_send_ms'])
                self.assertFalse(timing['server_generation_end_measured'])
                release_rest.set()
                await asyncio.wait_for(reply,1)
                self.assertLessEqual(timing['first_speaker_send_ms'],timing['llm_stream_drained_ms'])
        finally:
            if reply and not reply.done():reply.cancel()
            player.cancel();await asyncio.gather(*(task for task in (reply,player) if task),return_exceptions=True)

    async def test_interrupting_long_audio_closes_tts_and_model_iterators(self):
        voice=session();model_closed=asyncio.Event()
        factory=TTSFactory([b'\x03\0'*(24000*35)])
        async def llm(text,history,system_prompt=None):
            try:
                yield '这是长朗读的开头，'
                await asyncio.Event().wait()
            finally:model_closed.set()
        player=asyncio.create_task(voice.playback());reply=None
        try:
            with patch('pc_backend.duplex_voice.SpeechTask',factory), \
                 patch('pc_backend.duplex_voice.stream_reply',llm):
                reply=asyncio.create_task(voice.reply_turn(None,'长朗读'))
                voice.turn_task=reply
                await asyncio.wait_for(voice.speaker.first_send.wait(),1)
                await asyncio.wait_for(voice.interrupt(),1)
            self.assertTrue(reply.cancelled())
            self.assertTrue(model_closed.is_set())
            self.assertEqual(factory.opened,factory.closed)
            self.assertEqual(factory.event_iterators_closed,factory.opened)
            self.assertTrue(voice.play_queue.empty())
            self.assertEqual(voice.phase,'listening')
            self.assertTrue(voice.active)
            self.assertLess(voice.speaker.bytes_sent,48000*2*35)
        finally:
            if reply and not reply.done():reply.cancel()
            player.cancel();await asyncio.gather(*(task for task in (reply,player) if task),return_exceptions=True)

    async def test_turn_taking_fallback_also_preserves_long_text_and_audio(self):
        voice=session(duplex=False)
        output='完整长文，不在本地截短。'*310
        audio=b'\x04\0'*(24000*32)
        factory=TTSFactory([audio[index:index+48000] for index in range(0,len(audio),48000)],
                           wait_for_finish=True)
        async def llm(text,history,system_prompt=None):yield output
        player=asyncio.create_task(voice.playback())
        try:
            with patch('pc_backend.chained_voice.SpeechTask',factory), \
                 patch('pc_backend.chained_voice.stream_reply',llm):
                await asyncio.wait_for(voice.reply_turn(None,'完整朗读全文'),3)
            self.assertEqual(voice.response_bytes,len(audio))
            self.assertEqual(''.join(factory.texts),output)
            self.assertEqual(voice.history[-1]['content'],output)
            self.assertEqual(voice.messages[-1]['text'],output)
            self.assertLessEqual(voice.play_queue.maximum_audio_item,960)
        finally:
            player.cancel();await asyncio.gather(player,return_exceptions=True)

    async def test_session_limit_waits_for_current_reading_to_finish(self):
        voice=session();voice.config['max_session_seconds']=30
        voice.started=time.monotonic()-31;voice.last_input=time.monotonic()
        voice.phase='speaking';voice.response_open=True
        watchdog=asyncio.create_task(voice.watchdog(False))
        try:
            async with asyncio.timeout(1):
                while not voice.session_limit_pending:await asyncio.sleep(.01)
            self.assertFalse(voice.stop_event.is_set())
            self.assertFalse(watchdog.done())
            voice.response_open=False
            voice.resume_listening()
            self.assertTrue(voice.stop_event.is_set())
            self.assertEqual(voice.reason,'session_time_limit')
        finally:
            watchdog.cancel();await asyncio.gather(watchdog,return_exceptions=True)

    async def test_zero_session_limit_never_stops_an_otherwise_healthy_call(self):
        voice=session();voice.config['max_session_seconds']=0
        voice.started=time.monotonic()-10000;voice.last_input=time.monotonic()
        watchdog=asyncio.create_task(voice.watchdog(False))
        try:
            await asyncio.sleep(.3)
            voice.resume_listening()
            self.assertFalse(voice.stop_event.is_set())
            self.assertFalse(voice.session_limit_pending)
        finally:
            watchdog.cancel();await asyncio.gather(watchdog,return_exceptions=True)

    async def test_provider_idle_timeout_excludes_deliberate_consumer_backpressure(self):
        async def events():
            yield b'audio'
            yield b'more_audio'
        incoming=timed_speech_events(events(),.02)
        try:
            self.assertEqual(await anext(incoming),b'audio')
            await asyncio.sleep(.04)
            self.assertEqual(await anext(incoming),b'more_audio')
        finally:await incoming.aclose()

    async def test_provider_without_progress_still_times_out(self):
        async def events():
            await asyncio.Event().wait()
            yield b''
        incoming=timed_speech_events(events(),.01)
        try:
            with self.assertRaises(VoiceError):await anext(incoming)
        finally:await incoming.aclose()


class LongVoiceBoundaryTests(unittest.TestCase):
    def test_first_phrase_prefers_early_natural_comma_and_later_units_remain_longer(self):
        text='豫章故郡，洪都新府。'
        self.assertEqual(take_sentence(text,first=True),('豫章故郡，','洪都新府。'))
        self.assertEqual(take_sentence(text), (text,''))
        self.assertEqual(len(take_sentence('字'*90,first=True)[0]),18)

    def test_unlimited_session_value_is_explicit_and_short_invalid_limits_stay_invalid(self):
        self.assertEqual(validate_config({**DEFAULTS,'max_session_seconds':0})['max_session_seconds'],0)
        with self.assertRaises(VoiceError):validate_config({**DEFAULTS,'max_session_seconds':1})


if __name__=='__main__':unittest.main()
