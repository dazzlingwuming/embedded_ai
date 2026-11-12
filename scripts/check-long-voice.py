"""Real DeepSeek -> production TTS/playback long-reading acceptance.

Input is a labeled public-domain text fixture, not ASR or live user speech.
This script makes paid DeepSeek/TTS calls and plays the real board speaker.
Run only after the console is idle and the updated backend is deployed.
No saved configuration, API key, or model response is modified.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

from aiohttp import ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pc_backend.duplex_voice as duplex_module
from pc_backend.ai_client import AIError
from pc_backend.duplex_audio import SPEECH_HEADER, SPEECH_PACKET_BYTES, unpack_speech
from pc_backend.duplex_voice import DuplexVoiceBridge
from pc_backend.speaker_client import BoardSpeaker
from pc_backend.voice_config import load_config
from pc_backend.voice_protocol import VoiceError

REFERENCE_PACKET_BYTES = SPEECH_PACKET_BYTES
MARKERS = ('豫章故郡', '时维九月', '披绣闼', '遥襟甫畅', '嗟乎', '一介书生', '胜地不常', '各倾陆海云尔')


def normalized(text):
    return ''.join(char for char in text if char.isalnum())


def fixture_from_report(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    text = data.get('output_text')
    if not isinstance(text, str) or len(normalized(text)) < 700:
        raise VoiceError('The prepared public-domain reference must contain at least 700 characters')
    compact = normalized(text)
    position = 0
    for marker in MARKERS:
        found = compact.find(marker, position)
        if found < 0:
            raise VoiceError('The prepared reference is missing an ordered Tengwang Pavilion preface section')
        position = found + len(marker)
    if not compact.endswith(MARKERS[-1]):
        raise VoiceError('The prepared reference does not reach the complete prose ending')
    return text


def completeness(expected, actual):
    source, output = normalized(expected), normalized(actual)
    return {'expected_characters': len(source), 'actual_characters': len(output),
            'complete_body_present': bool(source) and source in output,
            'expected_ending_present': bool(source) and output.endswith(source[-40:]),
            'normalized_exact_match': source == output}


@dataclass
class Trace:
    phase: str
    started: float = field(default_factory=time.monotonic)
    events: dict = field(default_factory=dict)
    llm_text: str = ''
    tts_texts: list = field(default_factory=list)
    committed_texts: list = field(default_factory=list)
    active_tts: int = 0
    opened_tts: int = 0
    closed_tts: int = 0
    received_tts_bytes: int = 0
    sent_samples: int = 0
    drained_samples: int = 0
    drained_segments: int = 0
    epochs: dict = field(default_factory=dict)
    first_nonzero_epoch: int | None = None
    first_nonzero_sequence: int | None = None
    reference_active_frames: int = 0
    error: str | None = None

    def mark(self, name, overwrite=False):
        if overwrite or name not in self.events:
            self.events[name] = round((time.monotonic() - self.started) * 1000, 2)

    def as_dict(self):
        return {'phase': self.phase, 'events_ms': self.events,
            'llm_output_text': self.llm_text, 'tts_input_text': ''.join(self.tts_texts),
            'committed_segment_texts': self.committed_texts,
            'received_tts_audio_seconds': round(self.received_tts_bytes / 48000, 3),
            'speaker_submitted_audio_seconds': round(self.sent_samples / 48000, 3),
            'speaker_fully_drained_audio_seconds': round(self.drained_samples / 48000, 3),
            'speaker_fully_drained_segments': self.drained_segments,
            'tts_tasks_opened': self.opened_tts, 'tts_tasks_closed': self.closed_tts,
            'tts_tasks_still_active': self.active_tts,
            'reference_active_frames': self.reference_active_frames, 'error': self.error}


class ObservedSpeechTask:
    """Observation only: delegates every request and event to real SpeechTask."""
    def __init__(self, task, trace):
        if task.service != 'TTS':
            raise VoiceError('This text-fixture diagnostic must not start cloud ASR')
        self.task, self.trace, self.entered, self.closed = task, trace, False, False

    def __getattr__(self, name):
        return getattr(self.task, name)

    async def __aenter__(self):
        await self.task.__aenter__()
        self.entered = True
        self.trace.opened_tts += 1; self.trace.active_tts += 1
        self.trace.mark('first_tts_task_ready')
        return self

    def note_closed(self):
        if self.entered and not self.closed:
            self.closed = True
            self.trace.active_tts -= 1; self.trace.closed_tts += 1

    async def __aexit__(self, *args):
        try:
            return await self.task.__aexit__(*args)
        finally:
            self.note_closed()

    async def close(self):
        try:
            await self.task.close()
        finally:
            self.note_closed()

    async def send_text(self, text):
        result = await self.task.send_text(text)
        self.trace.tts_texts.append(text)
        self.trace.mark('first_tts_text_sent')
        return result

    async def events(self):
        async for event in self.task.events():
            if isinstance(event, bytes) and event:
                self.trace.mark('first_tts_audio_received')
                self.trace.received_tts_bytes += len(event)
            yield event


class ObservedSpeaker(BoardSpeaker):
    """Uses real speaker WS, acknowledgements and cancellation unchanged."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace = None

    async def begin(self, volume):
        epoch = await super().begin(volume)
        self.trace.epochs[epoch] = 0
        self.trace.mark('first_speaker_begin_ack')
        return epoch

    async def send(self, pcm, epoch):
        sequence = self.sent
        await super().send(pcm, epoch)
        trace = self.trace
        trace.sent_samples += len(pcm) // 2
        trace.epochs[epoch] = trace.epochs.get(epoch, 0) + len(pcm) // 2
        trace.mark('first_speaker_frame_submitted')
        if any(abs(value[0]) > 8 for value in struct.iter_unpack('<h', pcm)):
            trace.mark('first_nonzero_speaker_frame_submitted')
            if trace.first_nonzero_epoch is None:
                trace.first_nonzero_epoch, trace.first_nonzero_sequence = epoch, sequence

    async def finish(self, epoch):
        await super().finish(epoch)
        self.trace.drained_samples += self.trace.epochs[epoch]
        self.trace.drained_segments += 1
        self.trace.mark('first_segment_drained')
        self.trace.mark('last_segment_drained', overwrite=True)


class ObservedVoice(DuplexVoiceBridge):
    def __init__(self, console, trace):
        super().__init__(console)
        self.trace = trace

    def commit_heard_segment(self, segment):
        before = segment.turn.heard
        super().commit_heard_segment(segment)
        if segment.turn.heard != before:
            self.trace.committed_texts.append(segment.text)


class ReferenceObserver:
    """Observe production BLV1 mic/ref RMS without a 3-channel diagnostic stream."""
    def __init__(self):
        self.trace = None
        self.frames = self.reference_frames = self.quiet_frames = 0
        self.baseline_mic_power = 0.0
        self.previous = None
        self.error = None
        self.ready = asyncio.Event()
        self.stopping = False

    async def run(self, client, host):
        try:
            async with client.get(f'http://{host}:84/speech',
                    timeout=ClientTimeout(total=None, connect=4, sock_read=6)) as response:
                if response.status != 200:
                    raise VoiceError('The board speech stream is busy or unavailable')
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    buffer.extend(chunk)
                    while len(buffer) >= REFERENCE_PACKET_BYTES:
                        frame = unpack_speech(bytes(buffer[:REFERENCE_PACKET_BYTES]))
                        del buffer[:REFERENCE_PACKET_BYTES]
                        sequence, flags, mic, ref = frame.sequence, frame.flags, frame.mic_rms, frame.reference_rms
                        if self.previous is not None and sequence != (self.previous + 1) & 0xffffffff:
                            raise VoiceError('The board reference observation lost frames')
                        self.previous = sequence
                        self.frames += 1
                        if self.frames <= 100:
                            self.baseline_mic_power += mic * mic / 100
                            if self.frames == 100:
                                self.ready.set()
                        reference_active = bool(flags & 2) and ref >= 32
                        self.quiet_frames = 0 if reference_active else self.quiet_frames + 1
                        if reference_active:
                            self.reference_frames += 1
                            if self.trace:
                                self.trace.reference_active_frames += 1
                                self.trace.mark('first_board_reference_observed')
                                if mic >= max(12, 2 * math.sqrt(self.baseline_mic_power)):
                                    self.trace.mark('first_board_microphone_pickup_candidate')
                if not self.stopping:
                    raise VoiceError('The reference observation ended unexpectedly')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = str(error) if isinstance(error, VoiceError) else type(error).__name__
        finally:
            self.ready.set()


def build_voice(console, config, speaker, trace):
    voice = ObservedVoice(console, trace)
    voice.config = config.copy()
    voice.active = voice.duplex_enabled = True
    voice.phase, voice.source = 'listening', 'text_fixture'
    voice.started = voice.last_input = time.monotonic()
    voice.session_id = uuid.uuid4().hex
    voice.speaker = speaker
    voice.metrics = dict(mic_frames=0, uplink_bytes=0, mic_drops=0, suppressed_mic_frames=0,
        downlink_bytes=0, speaker_frames=0, interruptions=0, late_audio_discarded=0,
        input_discontinuities=0, auto_interruptions=0, played_segments=0, synthesized_segments=0)
    return voice


async def check_phase(name, fixture, config, console, speaker, observer, guard, original_stream, original_task, options, outcome):
    trace = Trace(name)
    speaker.trace = observer.trace = trace
    voice = build_voice(console, config, speaker, trace)
    playback = reply = None
    text = '请完整逐字输出并朗读以下《滕王阁序》正文。只输出原文，不要开场白、标题、解释、删节或结束语。请一直读到最后的“各倾陆海云尔”。原文如下：\n' + fixture

    async def observed_stream(*args, **kwargs):
        trace.mark('llm_iterator_started')
        stream = original_stream(*args, **kwargs)
        completed = False
        try:
            async for delta in stream:
                trace.mark('first_llm_text_received')
                trace.mark('last_llm_text_received', overwrite=True)
                trace.llm_text += delta
                yield delta
            completed = True
            trace.mark('llm_iterator_exhausted')
        finally:
            await stream.aclose()
            if not completed:
                trace.mark('llm_iterator_closed_before_completion')

    def observed_task(*args, **kwargs):
        return ObservedSpeechTask(original_task(*args, **kwargs), trace)

    try:
        with patch.object(duplex_module, 'stream_reply', observed_stream), \
             patch.object(duplex_module, 'SpeechTask', observed_task):
            playback = asyncio.create_task(voice.playback())
            reply = asyncio.create_task(voice.reply_turn(console.http, text))
            voice.turn_task = reply
            next_guard = time.monotonic() + 1
            next_progress = time.monotonic() + 15
            while not reply.done():
                if playback.done():
                    await playback
                    raise VoiceError('The production playback loop exited unexpectedly')
                if observer.error:
                    raise VoiceError(observer.error)
                if speaker.error:
                    raise speaker.error
                if (trace.first_nonzero_epoch is not None and speaker.epoch == trace.first_nonzero_epoch and
                        speaker.consumed > trace.first_nonzero_sequence):
                    trace.mark('first_nonzero_board_consumption_observed')
                if time.monotonic() >= next_guard:
                    await guard()
                    next_guard = time.monotonic() + 1
                turn = voice.current_turn
                if time.monotonic() >= next_progress:
                    progress = {'running': True, 'input_origin': 'text_fixture', 'phase': name,
                        'elapsed_seconds': round(time.monotonic()-trace.started, 1),
                        'generated_characters': len(normalized(turn.generated)) if turn else 0,
                        'committed_characters': len(normalized(turn.heard)) if turn else 0,
                        'speaker_drained_audio_seconds': round(trace.drained_samples/48000, 2),
                        'tts_received_audio_seconds': round(trace.received_tts_bytes/48000, 2),
                        'drained_segments': trace.drained_segments, 'events_ms': trace.events.copy()}
                    (ROOT/'data'/'long-voice-progress.json').write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
                    print(json.dumps(progress,ensure_ascii=False),flush=True)
                    next_progress = time.monotonic()+15
                if (name == 'interrupt' and trace.sent_samples / 48000 >= options.interrupt_after and
                        turn and turn.heard and trace.sent_samples > trace.drained_samples + 480):
                    outcome['active_tts_before_interrupt'] = trace.active_tts
                    outcome['llm_iterator_already_complete_before_interrupt'] = 'llm_iterator_exhausted' in trace.events
                    trace.mark('interrupt_requested')
                    await voice.interrupt()
                    trace.mark('interrupt_returned')
                    submitted = trace.sent_samples
                    await asyncio.sleep(.8)
                    status = await console.get_board()
                    assistant_history = ''.join(entry['content'] for entry in voice.history if entry['role'] == 'assistant')
                    reply_error = None if reply.cancelled() or not reply.done() else reply.exception()
                    outcome.update(reply_task_cancelled=reply.cancelled(),
                        old_turn_stopped=reply.done() and turn.interrupted and turn.generation != voice.generation and reply_error is None,
                        reply_task_error=type(reply_error).__name__ if reply_error else None,
                        listening_after_interrupt=voice.phase == 'listening',
                        no_audio_submitted_after_interrupt=trace.sent_samples == submitted,
                        old_queue_empty=voice.play_queue.empty(),
                        generated_text=turn.generated, heard_text=turn.heard,
                        generated_but_unplayed_characters=len(normalized(turn.generated)) - len(normalized(turn.heard)),
                        history_only_committed_segments=assistant_history == turn.heard == ''.join(trace.committed_texts),
                        committed_segments_have_drained_ack=len(trace.committed_texts) == trace.drained_segments,
                        speaker_stopped=not status['speaker']['active'] and not status['speaker']['amplifier_enabled']
                            and status['speaker']['queued_frames'] == 0,
                        production_speaker_stop_ms=voice.metrics.get('speaker_stop_ms'))
                    outcome['passed'] = all(outcome[key] for key in ('old_turn_stopped', 'listening_after_interrupt',
                        'no_audio_submitted_after_interrupt', 'old_queue_empty', 'history_only_committed_segments',
                        'committed_segments_have_drained_ack', 'speaker_stopped')) \
                        and outcome['generated_but_unplayed_characters'] > 0
                    break
                await asyncio.sleep(.01)
            else:
                await reply
                trace.mark('reply_returned')
                if name == 'full':
                    turn = voice.current_turn
                    generated = completeness(fixture, turn.generated)
                    heard = completeness(fixture, turn.heard)
                    exhausted = trace.events.get('llm_iterator_exhausted')
                    first_audio = trace.events.get('first_nonzero_speaker_frame_submitted')
                    first_reference = trace.events.get('first_board_reference_observed')
                    early_audio = exhausted is not None and first_audio is not None and first_audio < exhausted
                    early_reference = exhausted is not None and first_reference is not None and first_reference < exhausted
                    status = await console.get_board()
                    outcome.update(generated_completeness=generated, heard_completeness=heard,
                        tts_text_completeness=completeness(fixture, ''.join(trace.tts_texts)),
                        generated_matches_observed_llm_stream=turn.generated == trace.llm_text,
                        committed_text_matches_expected=normalized(''.join(trace.committed_texts)) == normalized(fixture),
                        committed_segments_have_drained_ack=len(trace.committed_texts) == trace.drained_segments,
                        listening_after_complete=voice.phase == 'listening' and turn.done.is_set(),
                        first_speaker_audio_before_llm_iterator_end=early_audio,
                        first_board_reference_before_llm_iterator_end=early_reference,
                        minimum_played_seconds=options.minimum_audio,
                        production_turn_timing=voice.metrics.get('current_turn_timing'),
                        exceeded_old_30s_audio_boundary=trace.drained_samples / 48000 > 30,
                        exceeded_old_90s_reply_boundary=trace.events['reply_returned'] > 90000,
                        llm_iterator_exceeded_old_75s_boundary=bool(exhausted and exhausted > 75000),
                        speaker_stopped=not status['speaker']['active'] and not status['speaker']['amplifier_enabled'])
                    outcome['passed'] = (all(generated[key] and heard[key] for key in ('complete_body_present', 'expected_ending_present', 'normalized_exact_match'))
                        and outcome['listening_after_complete'] and early_audio and early_reference
                        and outcome['generated_matches_observed_llm_stream']
                        and outcome['committed_text_matches_expected'] and outcome['committed_segments_have_drained_ack']
                        and outcome['tts_text_completeness']['normalized_exact_match']
                        and trace.drained_samples / 48000 >= options.minimum_audio and outcome['speaker_stopped'])
                else:
                    outcome['error'] = 'The long answer finished before an in-progress segment could be interrupted'
    except asyncio.CancelledError:
        trace.error = 'diagnostic canceled or time budget reached'
        raise
    except Exception as error:
        trace.error = str(error) if isinstance(error, (VoiceError, AIError)) else type(error).__name__
        outcome['error'] = trace.error
    finally:
        if reply and not reply.done():
            try:
                async with asyncio.timeout(10):
                    await voice.interrupt()
            except (Exception, asyncio.CancelledError) as error:
                outcome['cleanup_interruption_error'] = type(error).__name__
                outcome['passed'] = False
        if reply:
            reply.cancel()
            await asyncio.gather(reply, return_exceptions=True)
        if playback:
            playback.cancel()
            await asyncio.gather(playback, return_exceptions=True)
        voice.active = False
        observer.trace = None
        outcome['tts_connections_closed'] = trace.active_tts == 0
        if not outcome['tts_connections_closed']:
            outcome['passed'] = False
        outcome['trace'] = trace.as_dict()
    return outcome


async def run(options):
    started = time.monotonic()
    destination = ROOT/'data'/'long-voice-checks'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    destination.mkdir(parents=True, exist_ok=False)
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'passed': False,
        'input_origin': 'text_fixture', 'cloud_api_requested': True, 'cloud_api_used': False, 'cloud_asr_used': False,
        'microphone_audio_uploaded': False, 'live_human_interruption_tested': False,
        'human_listening_quality_confirmed': False, 'maximum_wall_seconds': options.max_seconds,
        'production_methods': ['DuplexVoiceBridge.reply_turn', 'DuplexVoiceBridge.playback', 'DuplexVoiceBridge.interrupt', 'BoardSpeaker'],
        'observation_definitions': {
            'llm_iterator_exhausted': 'The real provider async iterator returned after its last delta; this includes application backpressure, not the model server computation completion time.',
            'first_nonzero_speaker_frame_submitted': 'A real nonzero PCM frame was submitted through the production board WebSocket. This is not a human acoustic onset measurement.',
            'first_nonzero_board_consumption_observed': 'The production consumed counter covered the first nonzero frame; ACK is observed by polling and has DMA/ACK latency.',
            'first_board_reference_observed': 'A real production BLV1 frame reported PA enabled and DAC reference RMS >=32; the timestamp is PC reception, not a synchronized acoustic timestamp. The compact 672-byte production format avoids adding a 3-channel diagnostic bandwidth load.',
            'first_board_microphone_pickup_candidate': 'Reference activity plus mic RMS above baseline; an acoustic pickup candidate, not an isolated human listening confirmation.',
            'speaker_fully_drained_audio_seconds': 'Only real PCM samples in segments acknowledged drained by the board, including its DMA-tail handling.',
            'observation_wrappers': 'All model/TTS events and board acknowledgements delegate to actual production implementations; none are synthesized.'},
        'phases': []}
    speaker = observer_task = observer = None
    owned = False
    base = None
    async with ClientSession(trust_env=False, timeout=ClientTimeout(total=None, connect=8)) as http:
        async def get(url):
            async with http.get(url, timeout=ClientTimeout(total=6)) as response:
                if response.status != 200:
                    raise VoiceError(f'Status request failed (HTTP {response.status})')
                return await response.json()

        async def guard():
            state = await get('http://127.0.0.1:8765/api/status')
            if state.get('recording') or state.get('record_pending') or state.get('recording_pending') or state.get('voice', {}).get('active'):
                raise VoiceError('The user console is recording or in a voice call; this diagnostic did not take over')
            return state

        try:
            async with asyncio.timeout(options.max_seconds):
                state = await guard()
                base = state['board_url'].rstrip('/')
                board = await get(base+'/api/status')
                if (not board.get('speech_frontend', {}).get('ready') or board['speech_frontend'].get('streaming')
                        or not board.get('speaker', {}).get('ready') or board['speaker'].get('connected') or board['speaker'].get('active')):
                    raise VoiceError('The board speaker or speech stream is unavailable or busy')
                config = load_config()
                if config['interaction_mode'] != 'automatic_interrupt' or not config['volume']:
                    raise VoiceError('Use an audible saved volume and automatic-interruption mode before this diagnostic')
                fixture = fixture_from_report(ROOT/'data'/'deepseek-long-stream-check.json')
                report['fixture'] = {'source': 'data/deepseek-long-stream-check.json', 'title': '滕王阁序',
                    'public_domain': True, 'characters': len(normalized(fixture)),
                    'sha256_utf8': hashlib.sha256(fixture.encode()).hexdigest(), 'expected_ending': MARKERS[-1]}
                report['initial_board'] = board
                report['volume'] = config['volume']
                report['models'] = {'asr': 'not used', 'tts': config['tts_model']}
                host = urlsplit(base).hostname
                observer = ReferenceObserver()
                observer_task = asyncio.create_task(observer.run(http, host))
                await asyncio.wait_for(observer.ready.wait(), 10)
                if observer.error:
                    raise VoiceError(observer.error)
                speaker = ObservedSpeaker(http, base)
                await speaker.connect(); owned = True
                console = SimpleNamespace(board=board, board_url=base, http=http, get_board=lambda: get(base+'/api/status'))
                original_stream, original_task = duplex_module.stream_reply, duplex_module.SpeechTask
                for phase in (('full', 'interrupt') if options.phase == 'both' else (options.phase,)):
                    await guard()
                    while observer.quiet_frames < 20:
                        if observer.error:
                            raise VoiceError(observer.error)
                        await asyncio.sleep(.02)
                    outcome = {'phase': phase, 'passed': False}
                    report['phases'].append(outcome)
                    await check_phase(phase, fixture, config, console, speaker, observer,
                        guard, original_stream, original_task, options, outcome)
                    print(json.dumps({'phase': phase, 'passed': outcome['passed'],
                        'drained_seconds': outcome['trace']['speaker_fully_drained_audio_seconds'],
                        'error': outcome.get('error')}, ensure_ascii=False), flush=True)
                    if not outcome['passed']:
                        break
                report['passed'] = bool(report['phases']) and all(item['passed'] for item in report['phases']) \
                    and (len(report['phases']) == 2 if options.phase == 'both' else True)
        except Exception as error:
            report['error'] = str(error) if isinstance(error, (VoiceError, AIError)) else type(error).__name__
        finally:
            if speaker:
                if owned:
                    await speaker.close()
                elif speaker.ws:
                    await speaker.ws.close()
            if observer:
                observer.stopping = True
            if observer_task:
                observer_task.cancel(); await asyncio.gather(observer_task, return_exceptions=True)
            if base:
                try:
                    final = await get(base+'/api/status')
                    for _ in range(10):
                        if not final['speaker']['connected'] and not final.get('speech_frontend', {}).get('streaming'):
                            break
                        await asyncio.sleep(.1)
                        final = await get(base+'/api/status')
                    report['final_board'] = final
                    report['cleanup_verified'] = (not final['speaker']['active'] and not final['speaker']['amplifier_enabled']
                        and not final['speaker']['connected'] and not final.get('speech_frontend', {}).get('streaming'))
                    if not report['cleanup_verified']:
                        report['passed'] = False
                except Exception as error:
                    report.update(cleanup_error=type(error).__name__, passed=False)
            report['elapsed_seconds'] = round(time.monotonic()-started, 2)
            report['cloud_api_used'] = any('llm_iterator_started' in phase.get('trace', {}).get('events_ms', {})
                or phase.get('trace', {}).get('tts_tasks_opened', 0) for phase in report['phases'])
            report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    (destination/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT/'data'/'long-voice-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT/'data'/'long-voice-progress.json').write_text(json.dumps({'running': False, 'passed': report['passed'],
        'input_origin': 'text_fixture', 'elapsed_seconds': report['elapsed_seconds'],
        'report': str((destination/'report.json').relative_to(ROOT)).replace('\\','/')},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'elapsed_seconds': report['elapsed_seconds'],
        'error': report.get('error'), 'report': str((destination/'report.json').relative_to(ROOT)).replace('\\','/')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('both','full','interrupt'), default='both')
    parser.add_argument('--max-seconds', type=int, default=480)
    parser.add_argument('--minimum-audio', type=float, default=90,
                        help='Required fully drained audio seconds for the complete-reading phase')
    parser.add_argument('--interrupt-after', type=float, default=12,
                        help='Interrupt an unfinished segment after this much real PCM has been submitted')
    options = parser.parse_args()
    if not 60 <= options.max_seconds <= 480 or not 31 <= options.minimum_audio <= 300 or not 3 <= options.interrupt_after <= 30:
        parser.error('Use max-seconds 60..480, minimum-audio 31..300, interrupt-after 3..30')
    return options


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(run(arguments())))
