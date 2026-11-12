import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from pc_backend.duplex_audio import unpack_speech
from pc_backend.voice_protocol import VoiceError

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('duplex_session_diagnostic',
    ROOT/'scripts/check-duplex-session.py')
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTIC)


def idle_status():
    return {'board_connected':True,'recording':None,'voice':{'active':False}}


def idle_board():
    return {'speaker':{'ready':True,'connected':False,'active':False,'amplifier_enabled':False},
            'speech_frontend':{'ready':True,'streaming':False}}


class DiagnosticGuardTests(unittest.TestCase):
    def test_existing_recording_call_or_speaker_owner_is_not_preempted(self):
        scenarios = []
        status = idle_status();status['recording']={'session_id':'user'}
        scenarios.append((status,idle_board()))
        status = idle_status();status['voice']['active']=True
        scenarios.append((status,idle_board()))
        for field in ('connected','active','amplifier_enabled'):
            board = idle_board();board['speaker'][field]=True
            scenarios.append((idle_status(),board))
        board = idle_board();board['speech_frontend']['streaming']=True
        scenarios.append((idle_status(),board))
        for index,(status,board) in enumerate(scenarios):
            with self.subTest(index=index),self.assertRaises(VoiceError):
                DIAGNOSTIC.check_preflight(status,board)

    def test_only_the_normal_board_speech_get_is_replaced(self):
        calls=[]
        class HTTP:
            def get(self,url,*args,**kwargs):calls.append(('get',url));return 'real_get'
            def ws_connect(self,url,*args,**kwargs):calls.append(('ws',url));return 'real_ws'
            def post(self,url,*args,**kwargs):calls.append(('post',url));return 'real_post'
        source=DIAGNOSTIC.SyntheticSpeechInput([b'\1\0'*320])
        router=DIAGNOSTIC.SpeechInputRouter(HTTP(),'http://192.168.2.38',source)
        self.assertEqual(router.get('http://192.168.2.38/api/status'),'real_get')
        self.assertEqual(router.get('http://192.168.2.38:84/speech?debug=1'),'real_get')
        self.assertEqual(router.get('http://other-board:84/speech'),'real_get')
        self.assertEqual(router.ws_connect('http://192.168.2.38:82/speaker'),'real_ws')
        self.assertEqual(router.post('http://192.168.2.38/api/speaker/cancel'),'real_post')
        response=router.get('http://192.168.2.38:84/speech')
        self.assertEqual(response.status,200)
        self.assertEqual(len(calls),5)

    def test_fixture_conversion_produces_only_20ms_pcm16k_frames(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache/tmp') as directory:
            path=Path(directory)/'fixture.wav'
            with wave.open(str(path),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
                wav.writeframes(b'\x10\0'*960)
            frames=DIAGNOSTIC.fixture_frames(path)
        self.assertEqual(len(frames),2)
        self.assertTrue(all(len(frame)==640 for frame in frames))


class SyntheticInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_and_silence_have_explicit_synthetic_vad_frames(self):
        source=DIAGNOSTIC.SyntheticSpeechInput([b'\x10\0'*320])
        source.inject('question')
        response=source.response()
        async with response:
            stream=response.content.iter_chunked(4096)
            first=unpack_speech(await anext(stream))
            second=unpack_speech(await anext(stream))
            await stream.aclose()
        self.assertEqual(first.pcm,b'\x10\0'*320)
        self.assertEqual(first.flags,5)
        self.assertEqual(first.speech_ms,20)
        self.assertEqual(second.pcm,b'\0'*640)
        self.assertEqual(second.flags,4)
        self.assertEqual(second.sequence,first.sequence+1)
        self.assertEqual(source.completed_utterances,['question'])
        self.assertTrue(source.closed)

    async def test_offline_mode_never_constructs_a_network_client(self):
        output=io.StringIO()
        with patch.object(DIAGNOSTIC,'ClientSession',side_effect=AssertionError('network used')), \
             redirect_stdout(output):
            result=await DIAGNOSTIC.check_offline()
        report=json.loads(output.getvalue())
        self.assertEqual(result,0)
        self.assertFalse(report['cloud_api_used'])
        self.assertFalse(report['board_used'])
        self.assertEqual(report['input_origin'],DIAGNOSTIC.INPUT_ORIGIN)


if __name__=='__main__':unittest.main()
