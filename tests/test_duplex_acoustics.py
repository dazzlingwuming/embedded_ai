"""Offline wire/measurement checks; no board, microphone or API is accessed."""
import importlib.util
import asyncio
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('benchlink_acoustic_diagnostic', ROOT/'scripts'/'check-duplex-acoustics.py')
diagnostic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = diagnostic
SPEC.loader.exec_module(diagnostic)


def pcm(values):
    return struct.pack('<' + 'h' * len(values), *values)


def wire(index, clean, mic, reference, flags=4, speech_ms=0):
    raw = [pcm(values) for values in (clean, mic, reference)]
    rms = [int(math.sqrt(diagnostic.power(data))) for data in raw]
    header = diagnostic.HEADER.pack(b'BLD1', index, index*20000,16000,320,flags,
                                    rms[1],rms[2],rms[0],speech_ms)
    return header + b''.join(raw)


def frame(index, clean, mic, reference, flags=4, speech_ms=0):
    return diagnostic.unpack_diagnostic(wire(index, clean, mic, reference, flags, speech_ms))


def baseline(speech=False):
    return diagnostic.summarize_frames([frame(index,[1]*320,[5]*320,[0]*320,
        flags=5 if speech else 4,speech_ms=200 if speech else 0) for index in range(100)])


def trial(silent_mic=False, unrelated_mic=False, vad=False):
    output = []
    for index in range(120):
        reference = [int(1200*math.sin(2*math.pi*440*(index*320+n)/16000)) for n in range(320)]
        if silent_mic:
            mic = [0]*320
        elif unrelated_mic:
            mic = [int(120*math.sin(2*math.pi*555*(index*320+n)/16000)) for n in range(320)]
        else:
            mic = [int(value*.1) for value in reference]
        clean = [int(value*.25) for value in mic]
        output.append(frame(index,clean,mic,reference,flags=5 if vad else 4,
                            speech_ms=500 if vad else 0))
    return output


class DiagnosticTests(unittest.TestCase):
    def test_debug_wire_channel_order_and_sizes(self):
        result = frame(7,[1]*320,[2]*320,[3]*320)
        self.assertEqual(diagnostic.HEADER.size,32)
        self.assertEqual(diagnostic.PACKET_BYTES,1952)
        self.assertEqual(result.clean,pcm([1]*320))
        self.assertEqual(result.mic,pcm([2]*320))
        self.assertEqual(result.reference,pcm([3]*320))

    def test_normal_stream_invalid_header_and_partial_packets_are_rejected(self):
        good = diagnostic.HEADER.pack(b'BLD1',1,20000,16000,320,4,2,3,1,0)+pcm([1]*320+[2]*320+[3]*320)
        for bad in (good[:-1],b'BLV1'+good[4:],good[:16]+struct.pack('<I',48000)+good[20:]):
            with self.assertRaises(diagnostic.VoiceError):
                diagnostic.unpack_diagnostic(bad)

    def test_silent_microphone_cannot_pass_even_with_reference_and_zero_clean_output(self):
        result=diagnostic.analyze_trial(trial(silent_mic=True),baseline())
        self.assertFalse(result['signal_measurable'])
        self.assertEqual(result['status'],'inconclusive')
        status,_=diagnostic.classify(baseline(),[result,result],True)
        self.assertEqual(status,'inconclusive')

    def test_unrelated_loud_room_signal_is_not_proof_of_loudspeaker_pickup(self):
        result=diagnostic.analyze_trial(trial(unrelated_mic=True),baseline())
        self.assertGreater(result['microphone_energy_rise_vs_baseline'],1.7)
        self.assertFalse(result['microphone_echo_detected'])

    def test_measured_correlated_echo_reduction_passes_only_stated_speaker_only_check(self):
        result=diagnostic.analyze_trial(trial(),baseline())
        self.assertTrue(result['microphone_echo_detected'])
        self.assertTrue(result['suppression_observed'])
        self.assertAlmostEqual(result['mic_to_clean_attenuation_db'],12.3,delta=.5)
        status,_=diagnostic.classify(baseline(),[result,result],True)
        self.assertEqual(status,'passed')

    def test_sustained_vad_during_reference_playback_requires_tuning(self):
        result=diagnostic.analyze_trial(trial(vad=True),baseline())
        self.assertTrue(result['suppression_observed'])
        self.assertTrue(result['vad_candidate_during_speaker_only'])
        status,_=diagnostic.classify(baseline(),[result,result],True)
        self.assertEqual(status,'needs_tuning')

    def test_speech_like_baseline_and_transport_failures_cannot_pass(self):
        result=diagnostic.analyze_trial(trial(),baseline())
        self.assertEqual(diagnostic.classify(baseline(speech=True),[result,result],True)[0],'inconclusive')
        self.assertEqual(diagnostic.classify(baseline(),[result,result],False)[0],'failed')

    def test_preflight_rejects_old_firmware_and_busy_board(self):
        board={'firmware':'0.5.1','speech_frontend':{'ready':True},'speaker':{'ready':True}}
        with self.assertRaises(diagnostic.VoiceError):diagnostic.validate_board(board)
        board['firmware']='0.6.0';board['speaker']['connected']=True
        with self.assertRaises(diagnostic.VoiceError):diagnostic.validate_board(board)


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, payload, destination):
        capture=diagnostic.Capture(destination)
        class Content:
            async def iter_chunked(self,size):
                position=0
                for count in (13,625,1777,41,2048,100000):
                    if position<len(payload):
                        yield payload[position:position+count]
                        position+=count
                capture.stopping=True
        class Response:
            status=200
            content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        class Client:
            def get(self,*args,**kwargs):return Response()
        try:await capture.run(Client(),'127.0.0.1')
        finally:capture.close_files()
        return capture

    async def test_arbitrary_http_chunks_preserve_three_wav_channels(self):
        payload=b''.join(wire(i,[1]*320,[2]*320,[3]*320) for i in (1,2,3))
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache'/'tmp') as temporary:
            capture=await self.collect(payload,Path(temporary))
            self.assertIsNone(capture.error)
            self.assertEqual(len(capture.frames),3)
            for channel,value in [('clean',1),('mic',2),('reference',3)]:
                with wave.open(str(Path(temporary)/(channel+'.wav')),'rb') as saved:
                    self.assertEqual(saved.getnframes(),960)
                    self.assertEqual(saved.getframerate(),16000)
                    self.assertEqual(saved.readframes(960),pcm([value]*960))

    async def test_missing_diagnostic_frame_aborts_without_filling_fake_audio(self):
        payload=wire(1,[1]*320,[2]*320,[3]*320)+wire(3,[1]*320,[2]*320,[3]*320)
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache'/'tmp') as temporary:
            capture=await self.collect(payload,Path(temporary))
            self.assertEqual(capture.missing_frames,1)
            self.assertIn('Missing',capture.error)
            self.assertEqual(len(capture.frames),1)
            with wave.open(str(Path(temporary)/'mic.wav'),'rb') as saved:
                self.assertEqual(saved.getnframes(),320)


if __name__=='__main__':unittest.main()
