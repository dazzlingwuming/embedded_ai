import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pc_backend.duplex_audio import (InterruptionGate, SPEECH_HEADER,
    SpeechFrame, take_sentence, unpack_speech)
from pc_backend.duplex_voice import DuplexVoiceBridge, Segment, SpokenTurn
from pc_backend.server import Console
from pc_backend.voice_config import DEFAULTS, validate_config
from pc_backend.voice_protocol import PlaybackInterrupted, VoiceError


def packet(sequence=1, flags=5, text_pcm=b'\x01\0'*320):
    return SPEECH_HEADER.pack(b'BLV1', sequence, sequence*20000,16000,320,flags,100,200,80,300)+text_pcm


def bridge():
    result = DuplexVoiceBridge(SimpleNamespace(board={}, board_url='http://192.168.2.38'))
    result.config = DEFAULTS.copy()
    result.active, result.phase, result.source = True, 'listening', 'cloud_asr_deepseek_tts'
    result.duplex_enabled = True
    result.metrics = dict(mic_frames=0, uplink_bytes=0, mic_drops=0, downlink_bytes=0,
        speaker_frames=0, interruptions=0, late_audio_discarded=0, input_discontinuities=0)
    return result


class WireAndGateTests(unittest.TestCase):
    def test_only_valid_aec_speech_frames_are_accepted(self):
        frame = unpack_speech(packet())
        self.assertEqual(frame.pcm, b'\x01\0'*320)
        self.assertEqual(frame.reference_rms, 200)
        for bad in (packet()[:-1], b'BLA1'+packet()[4:], packet(flags=1), packet(flags=13)):
            with self.assertRaises(VoiceError):
                unpack_speech(bad)

    def test_vad_and_meaningful_recognition_are_both_required(self):
        gate = InterruptionGate(200)
        frame = unpack_speech(packet())
        self.assertFalse(gate.accepts('等等', 10))
        gate.feed(replace(frame, speech_ms=100), 10)
        self.assertFalse(gate.accepts('先说转速', 10.1))
        gate.feed(frame, 10.2)
        self.assertTrue(gate.accepts('先说转速', 10.3))
        self.assertFalse(gate.accepts('嗯', 10.3))
        self.assertFalse(gate.accepts('先说转速', 12))

    def test_explicit_stop_still_requires_recent_voice(self):
        gate = InterruptionGate(200)
        gate.feed(replace(unpack_speech(packet()),speech_ms=80), 10)
        self.assertTrue(gate.accepts('停！', 10.1))
        self.assertFalse(gate.accepts('停！', 12))

    def test_sentence_units_preserve_remainder_and_bound_unpunctuated_text(self):
        self.assertEqual(take_sentence('第一句。第二句'), ('第一句。','第二句'))
        self.assertEqual(take_sentence('还没有标点'), ('','还没有标点'))
        self.assertEqual(len(take_sentence('字'*120)[0]),80)
        self.assertEqual(take_sentence('结尾', True),('结尾',''))

    def test_raw_capture_is_only_required_for_raw_consumers_in_duplex_mode(self):
        state = SimpleNamespace(viewers={},recording=None,record_pending=False,
                                voice=SimpleNamespace(active=True, uses_separate_audio=True))
        self.assertFalse(Console.audio_required.fget(state))
        state.record_pending=True
        self.assertTrue(Console.audio_required.fget(state))

    def test_unknown_mode_and_unbounded_interrupt_duration_are_rejected(self):
        for update in ({'interaction_mode':'native_realtime'}, {'interruption_min_speech_ms':0}):
            with self.assertRaises(VoiceError):
                validate_config({**DEFAULTS,**update})


class ControlledSpeaker:
    def __init__(self):
        self.epoch = self.total_sent = 0
        self.finishing = asyncio.Queue()
        self.waiters = {}
        self.cancel = AsyncMock(side_effect=self._cancel)

    async def begin(self, volume):
        self.epoch += 1
        self.waiters[self.epoch] = asyncio.Event()
        return self.epoch

    async def send(self, pcm, epoch):
        if epoch != self.epoch:
            raise PlaybackInterrupted()
        self.total_sent += 1

    async def finish(self, epoch):
        await self.finishing.put(epoch)
        await self.waiters[epoch].wait()
        if epoch != self.epoch:
            raise PlaybackInterrupted()

    async def _cancel(self):
        self.epoch += 1
        for event in self.waiters.values():
            event.set()


class DuplexTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_cloud_start_does_not_consume_the_audio_watchdog_grace_period(self):
        session=bridge();session.phase='connecting'
        session.last_input=time.monotonic()-9
        session.resume_listening()
        self.assertLess(time.monotonic()-session.last_input,.1)
        self.assertEqual(session.phase,'listening')

    async def test_automatic_interrupt_preserves_heard_history_and_next_turn_completes(self):
        session=bridge();session.speaker=ControlledSpeaker()
        old_generation=session.generation
        canceled=asyncio.Event();new_reply=asyncio.Event()
        calls=[]
        class TTS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send_text(self,text):pass
            async def finish_text(self):pass
            async def events(self):
                yield b'\x01\0'*480
                yield {'header':{'event':'task-finished'},'payload':{}}
        async def llm(text,history,system_prompt=None):
            calls.append((text,history,system_prompt))
            if len(calls)==1:
                try:
                    yield '第一句已经播完。'
                    yield '第二句还没播完。'
                    await asyncio.Event().wait()
                finally:canceled.set()
            else:
                new_reply.set()
                yield '现在回答转速问题。'
        processor=asyncio.create_task(session.process_turns(None))
        playback=asyncio.create_task(session.playback())
        try:
            with patch('pc_backend.duplex_voice.SpeechTask',return_value=TTS()), \
                 patch('pc_backend.duplex_voice.stream_reply',llm):
                await session.accept_sentence({'sentence_id':1,'text':'先分析温度和振动',
                                               'sentence_end':True,'end_time':900})
                epoch=await asyncio.wait_for(session.speaker.finishing.get(),1)
                session.speaker.waiters[epoch].set()
                await asyncio.wait_for(session.speaker.finishing.get(),1)
                old_turn=session.current_turn
                session.gate.feed(unpack_speech(packet()))
                await session.accept_sentence({'sentence_id':2,'text':'等等，先告诉我转速',
                                               'sentence_end':False})
                await asyncio.wait_for(canceled.wait(),1)
                self.assertFalse(processor.done())
                self.assertFalse(playback.done())
                self.assertEqual(session.history[-1]['content'],'第一句已经播完。')
                self.assertEqual(session.phase,'listening')
                self.assertTrue(old_turn.interrupted)
                await session.accept_sentence({'sentence_id':2,'text':'等等，先告诉我转速',
                                               'sentence_end':True,'end_time':2000})
                await asyncio.wait_for(new_reply.wait(),1)
                # Even a late queued packet from the canceled turn cannot restart it.
                await session.play_queue.put(('segment_begin',old_generation,
                                               Segment(old_turn,'迟到音频。'),b''))
                epoch=await asyncio.wait_for(session.speaker.finishing.get(),1)
                session.speaker.waiters[epoch].set()
                await asyncio.wait_for(session.current_turn.done.wait(),1)
            self.assertEqual(calls[1][0],'等等，先告诉我转速')
            self.assertEqual(calls[1][1],[{'role':'user','content':'先分析温度和振动'},
                                       {'role':'assistant','content':'第一句已经播完。'}])
            self.assertIn('上一轮回答被用户打断',calls[1][2])
            self.assertEqual(session.history[-1]['content'],'现在回答转速问题。')
            self.assertEqual(session.metrics['auto_interruptions'],1)
            self.assertEqual(session.metrics['played_segments'],2)
            self.assertEqual(session.phase,'listening')
            self.assertTrue(session.active)
        finally:
            processor.cancel();playback.cancel()
            await asyncio.gather(processor,playback,return_exceptions=True)

    async def test_stale_final_cannot_interrupt_a_current_answer(self):
        session=bridge();session.phase='speaking';session.asr_floor_ms=1000
        session.speaker=SimpleNamespace(cancel=AsyncMock(),total_sent=0)
        session.gate.feed(unpack_speech(packet()))
        await session.accept_sentence({'sentence_id':9,'text':'等等',
                                       'sentence_end':True,'end_time':900})
        session.speaker.cancel.assert_not_awaited()
        self.assertEqual(session.phase,'speaking')

    async def test_empty_tts_binary_is_not_a_spoken_segment(self):
        session=bridge();session.speaker=ControlledSpeaker()
        class TTS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send_text(self,text):pass
            async def finish_text(self):pass
            async def events(self):
                yield b''
                yield {'header':{'event':'task-finished'},'payload':{}}
        async def llm(text,history,system_prompt=None):yield '不应标记为已播出的回答。'
        with patch('pc_backend.duplex_voice.SpeechTask',return_value=TTS()), \
             patch('pc_backend.duplex_voice.stream_reply',llm):
            with self.assertRaises(VoiceError):
                await asyncio.wait_for(session.reply_turn(None,'问题'),1)
        self.assertFalse(any(item['role']=='assistant' for item in session.history))

    async def test_unexpected_tts_cancellation_is_reported_as_a_connection_failure(self):
        session=bridge();session.speaker=ControlledSpeaker()
        class TTS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send_text(self,text):pass
            async def finish_text(self):pass
            async def events(self):
                raise asyncio.CancelledError()
                yield b''
        async def llm(text,history,system_prompt=None):
            yield '回答。'
            await asyncio.Event().wait()
        with patch('pc_backend.duplex_voice.SpeechTask',return_value=TTS()), \
             patch('pc_backend.duplex_voice.stream_reply',llm):
            with self.assertRaises(VoiceError):
                await asyncio.wait_for(session.reply_turn(None,'问题'),1)

    async def test_nonspoken_reply_cannot_complete_as_if_it_was_heard(self):
        session=bridge()
        async def llm(text,history,system_prompt=None):yield '😊'
        class PreheatedTTS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        with patch('pc_backend.duplex_voice.stream_reply',llm), \
             patch('pc_backend.duplex_voice.SpeechTask',return_value=PreheatedTTS()):
            with self.assertRaises(VoiceError):
                await asyncio.wait_for(session.reply_turn(None,'问题'),1)
        self.assertFalse(any(item['role']=='assistant' for item in session.history))

    async def test_confirmed_partial_stops_old_output_and_final_keeps_new_words(self):
        session = bridge();session.phase='speaking'
        session.speaker=SimpleNamespace(cancel=AsyncMock(),total_sent=0)
        session.gate.feed(unpack_speech(packet()))
        await session.accept_sentence({'sentence_id':2,'text':'等等，先说转速','sentence_end':False})
        session.speaker.cancel.assert_awaited_once()
        self.assertEqual(session.phase,'listening')
        self.assertEqual(session.metrics['auto_interruptions'],1)
        await session.accept_sentence({'sentence_id':2,'text':'等等，先说转速','sentence_end':True,'end_time':1000})
        self.assertEqual(session.turns.get_nowait(),'等等，先说转速')

    async def test_echo_text_without_recent_vad_does_not_interrupt(self):
        session=bridge();session.phase='speaking'
        session.speaker=SimpleNamespace(cancel=AsyncMock(),total_sent=0)
        await session.accept_sentence({'sentence_id':2,'text':'这是喇叭回声','sentence_end':False})
        session.speaker.cancel.assert_not_awaited()
        self.assertTrue(session.turns.empty())

    async def test_speaker_is_canceled_before_waiting_for_generation_shutdown(self):
        session=bridge();session.phase='speaking'
        order=[]
        async def cancel_speaker(): order.append('speaker')
        async def generation():
            try: await asyncio.Event().wait()
            finally: order.append('generation')
        session.speaker=SimpleNamespace(cancel=cancel_speaker,total_sent=0)
        session.turn_task=asyncio.create_task(generation());await asyncio.sleep(0)
        await session.interrupt()
        self.assertEqual(order,['speaker','generation'])

    async def test_only_drained_segments_enter_history_and_interrupt_cancels_the_rest(self):
        session=bridge();session.speaker=ControlledSpeaker()
        class TTS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send_text(self,text):pass
            async def finish_text(self):pass
            async def events(self):
                yield b'\x01\0'*480
                yield {'header':{'event':'task-finished'},'payload':{}}
        async def llm(text,history,system_prompt=None):
            yield '已经播完。'
            yield '这部分尚未播完。'
        playback=asyncio.create_task(session.playback())
        try:
            with patch('pc_backend.duplex_voice.SpeechTask',return_value=TTS()), \
                 patch('pc_backend.duplex_voice.stream_reply',llm):
                reply=asyncio.create_task(session.reply_turn(None,'用户的问题'))
                session.turn_task=reply
                epoch=await asyncio.wait_for(session.speaker.finishing.get(),1)
                self.assertEqual(list(session.history),[{'role':'user','content':'用户的问题'}])
                self.assertFalse(reply.done())
                session.speaker.waiters[epoch].set()
                await asyncio.wait_for(session.speaker.finishing.get(),1)
                self.assertEqual(session.history[-1]['content'],'已经播完。')
                await session.interrupt()
                self.assertEqual(session.history[-1]['content'],'已经播完。')
                self.assertTrue(reply.cancelled())
                self.assertTrue(session.active)
                self.assertEqual(session.phase,'listening')
                self.assertTrue(session.previous_interrupted)
        finally:
            playback.cancel();await asyncio.gather(playback,return_exceptions=True)

    async def test_canceled_generation_cannot_commit_a_late_drained_segment(self):
        session=bridge()
        turn=SpokenTurn('old',session.generation,'问题')
        session.generation+=1
        session.commit_heard_segment(Segment(turn,'不应进入历史。'))
        self.assertFalse(session.history)

    async def test_full_reply_waits_for_playback_and_commits_short_segments(self):
        session=bridge();session.speaker=ControlledSpeaker()
        class TTS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send_text(self,text):pass
            async def finish_text(self):pass
            async def events(self):
                yield b'\x01\0'*480
                yield {'header':{'event':'task-finished'},'payload':{}}
        async def llm(text,history,system_prompt=None):yield '第一句。第二句。'
        playback=asyncio.create_task(session.playback())
        try:
            with patch('pc_backend.duplex_voice.SpeechTask',return_value=TTS()), \
                 patch('pc_backend.duplex_voice.stream_reply',llm):
                reply=asyncio.create_task(session.reply_turn(None,'问题'))
                for _ in range(2):
                    epoch=await asyncio.wait_for(session.speaker.finishing.get(),1)
                    session.speaker.waiters[epoch].set()
                await asyncio.wait_for(reply,1)
            self.assertEqual(session.history[-1]['content'],'第一句。第二句。')
            self.assertEqual(session.phase,'listening')
            self.assertEqual(session.metrics['played_segments'],2)
        finally:
            playback.cancel();await asyncio.gather(playback,return_exceptions=True)

    async def test_aec_audio_is_uploaded_while_ai_is_speaking(self):
        session=bridge();session.phase='speaking'
        class Content:
            async def iter_chunked(self,size):
                yield packet(1)+packet(2)
                raise asyncio.CancelledError()
        class Response:
            status=200
            content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        session.console.http=SimpleNamespace(get=lambda *args,**kwargs:Response())
        session.asr=SimpleNamespace(send_pcm=AsyncMock())
        with self.assertRaises(asyncio.CancelledError):
            await session.asr_uplink()
        self.assertEqual(session.asr.send_pcm.await_count,2)
        session.asr.send_pcm.assert_awaited_with(b'\x01\0'*320)


if __name__=='__main__':
    unittest.main()
