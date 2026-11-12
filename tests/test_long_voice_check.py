"""Offline diagnostic bookkeeping checks; no paid API or board is accessed."""
import importlib.util
import json
from pathlib import Path
import sys
import asyncio
import tempfile
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('benchlink_long_voice_diagnostic', ROOT/'scripts'/'check-long-voice.py')
diagnostic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = diagnostic
SPEC.loader.exec_module(diagnostic)


class BookkeepingTests(unittest.TestCase):
    def test_full_body_and_real_ending_are_required_separately(self):
        expected='开始。中间内容。完整结尾。'
        complete=diagnostic.completeness(expected,'题目\n'+expected)
        self.assertTrue(complete['complete_body_present'])
        self.assertTrue(complete['expected_ending_present'])
        self.assertFalse(complete['normalized_exact_match'])
        short=diagnostic.completeness(expected,'开始。中间内容。')
        self.assertFalse(short['complete_body_present'])
        self.assertFalse(short['expected_ending_present'])

    def test_reference_is_a_full_labeled_text_fixture(self):
        # Synthetic offline metadata validates the input contract without
        # depending on an ignored artifact that requires a paid API call.
        text=('豫章故郡'+'测试文字'*180+'时维九月。披绣闼。遥襟甫畅。嗟乎。一介书生。胜地不常。各倾陆海云尔。')
        cache=ROOT/'.cache'/'tmp';cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            path=Path(temporary)/'reference.json'
            path.write_text(json.dumps({'output_text':text},ensure_ascii=False),encoding='utf-8')
            text=diagnostic.fixture_from_report(path)
        self.assertGreaterEqual(len(diagnostic.normalized(text)),700)
        self.assertTrue(diagnostic.normalized(text).endswith('各倾陆海云尔'))

    def test_direct_input_never_claims_asr_verified(self):
        config={'volume':75}
        trace=diagnostic.Trace('offline')
        voice=diagnostic.build_voice(SimpleNamespace(board={}),config,SimpleNamespace(),trace)
        self.assertEqual(voice.source,'text_fixture')
        self.assertFalse(voice.asr_verified)
        self.assertIsNot(voice.config,config)
        self.assertEqual(config,{'volume':75})

    def test_observation_definitions_do_not_overwrite_the_first_event(self):
        trace=diagnostic.Trace('offline',started=time.monotonic()-1)
        trace.mark('first')
        first=trace.events['first']
        trace.mark('first')
        self.assertEqual(trace.events['first'],first)
        self.assertGreaterEqual(first,990)


class TaskObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_observer_uses_compact_production_frames(self):
        observer=diagnostic.ReferenceObserver();observer.trace=diagnostic.Trace('offline')
        payload=b''.join(diagnostic.SPEECH_HEADER.pack(b'BLV1',index,index*20000,16000,320,
            4 if index<=100 else 6,5 if index<=100 else 100,0 if index<=100 else 2000,2,0)+b'\x02\0'*320
            for index in range(1,111))
        class Content:
            async def iter_chunked(self,size):
                for index in range(0,len(payload),1777):yield payload[index:index+1777]
                observer.stopping=True
        class Response:
            status=200
            content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        class Client:
            def get(self,url,**kwargs):
                self.requested_url=url
                return Response()
        client=Client()
        await observer.run(client,'127.0.0.1')
        self.assertEqual(client.requested_url,'http://127.0.0.1:84/speech')
        self.assertIsNone(observer.error)
        self.assertEqual(observer.frames,110)
        self.assertEqual(observer.trace.reference_active_frames,10)
        self.assertIn('first_board_reference_observed',observer.trace.events)

    async def test_tts_wrapper_delegates_actual_content_and_tracks_closed_context(self):
        class Actual:
            service='TTS'
            def __init__(self):self.text=[];self.closed=False
            async def __aenter__(self):return self
            async def __aexit__(self,*args):self.closed=True
            async def send_text(self,text):self.text.append(text)
            async def events(self):
                yield b'\x01\0'*480
                yield {'header':{'event':'task-finished'}}
        original=Actual();trace=diagnostic.Trace('offline')
        async with diagnostic.ObservedSpeechTask(original,trace) as task:
            await task.send_text('公版原文。')
            output=[event async for event in task.events()]
            self.assertEqual(trace.active_tts,1)
        self.assertEqual(output[0],b'\x01\0'*480)
        self.assertEqual(original.text,['公版原文。'])
        self.assertEqual(trace.tts_texts,original.text)
        self.assertTrue(original.closed)
        self.assertEqual((trace.opened_tts,trace.closed_tts,trace.active_tts),(1,1,0))

    async def test_asr_cannot_be_started_by_this_text_fixture_diagnostic(self):
        with self.assertRaises(diagnostic.VoiceError):
            diagnostic.ObservedSpeechTask(SimpleNamespace(service='ASR'),diagnostic.Trace('offline'))


if __name__=='__main__':unittest.main()
