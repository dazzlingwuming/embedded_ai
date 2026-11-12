"""Measure board AEC with a saved speech fixture; no cloud API is called.

Run while the room is quiet. This is a speaker-only echo test, not a near-end
speech, double-talk, ASR-accuracy, or human listening-quality acceptance test.
All artifacts remain inside the project.
"""
from __future__ import annotations

import argparse
from array import array
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import struct
import sys
from urllib.parse import urlsplit
import wave

from aiohttp import ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pc_backend.speaker_client import BoardSpeaker
from pc_backend.voice_protocol import PCMConverter, VoiceError

HEADER = struct.Struct('<4sIQIHHHHHH')
SAMPLES = 320
RATE = 16000
PACKET_BYTES = HEADER.size + SAMPLES * 2 * 3
FRAME_SECONDS = SAMPLES / RATE
CHANNELS = ('clean', 'mic', 'reference')
MIN_ATTENUATION_DB = 6.0


def now():
    return datetime.now(timezone.utc).isoformat()


def samples(pcm):
    result = array('h', pcm)
    if sys.byteorder != 'little':
        result.byteswap()
    return result


def power(pcm):
    values = samples(pcm)
    return sum(value * value for value in values) / max(1, len(values))


def db_ratio(numerator, denominator):
    # Half a PCM unit is a finite quantization floor, not proof of silence.
    return round(10 * math.log10(max(numerator, .25) / max(denominator, .25)), 2)


@dataclass(frozen=True, slots=True)
class DiagnosticFrame:
    sequence: int
    capture_us: int
    flags: int
    mic_rms: int
    reference_rms: int
    clean_rms: int
    speech_ms: int
    clean: bytes
    mic: bytes
    reference: bytes


def unpack_diagnostic(packet):
    if len(packet) != PACKET_BYTES:
        raise VoiceError('BLD1 diagnostic packet has the wrong length')
    magic, sequence, stamp, rate, count, flags, mic, ref, clean, speech = HEADER.unpack_from(packet)
    if magic != b'BLD1' or rate != RATE or count != SAMPLES or flags & ~15 or not flags & 4:
        raise VoiceError('BLD1 diagnostic format or AEC flag is invalid')
    start = HEADER.size
    clean_pcm = packet[start:start + SAMPLES * 2]
    mic_pcm = packet[start + SAMPLES * 2:start + SAMPLES * 4]
    reference_pcm = packet[start + SAMPLES * 4:]
    return DiagnosticFrame(sequence, stamp, flags, mic, ref, clean, speech,
                           clean_pcm, mic_pcm, reference_pcm)


def peak_correlation(reference, observed):
    """Diagnostic correlation at 2 kHz with a +/-150 ms, 4 ms lag search.

    Analysis averaging does not modify the saved 16 kHz signals. The peak lag
    is approximate; it is not a calibrated acoustic propagation measurement.
    """
    def average_blocks(pcm):
        values = samples(pcm)
        return [sum(values[index:index + 8]) / 8
                for index in range(0, len(values) - 7, 8)]

    ref, obs = average_blocks(reference), average_blocks(observed)
    best, best_lag = 0.0, 0
    if len(ref) < 128 or len(obs) != len(ref):
        return {'absolute_peak': None, 'lag_ms': None}
    for lag in range(-296, 297, 8):
        left, right = (ref[:len(ref) - lag], obs[lag:]) if lag >= 0 else (ref[-lag:], obs[:len(obs) + lag])
        if len(left) < 128:
            continue
        mean_left, mean_right = sum(left) / len(left), sum(right) / len(right)
        covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
        left_energy = sum((value - mean_left) ** 2 for value in left)
        right_energy = sum((value - mean_right) ** 2 for value in right)
        score = abs(covariance / math.sqrt(max(1e-30, left_energy * right_energy)))
        if score > best:
            best, best_lag = score, lag
    return {'absolute_peak': round(best, 4), 'lag_ms': best_lag / 2}


def summarize_frames(frames):
    if not frames:
        return {'frames': 0, 'seconds': 0, 'rms': {name: 0 for name in CHANNELS},
                'vad_ratio': 0, 'maximum_reported_speech_ms': 0}
    energies = {name: sum(power(getattr(frame, name)) for frame in frames) / len(frames)
                for name in CHANNELS}
    return {'frames': len(frames), 'seconds': round(len(frames) * FRAME_SECONDS, 3),
            'rms': {name: round(math.sqrt(value), 2) for name, value in energies.items()},
            'power': energies,
            'vad_ratio': round(sum(bool(frame.flags & 1) for frame in frames) / len(frames), 4),
            'maximum_reported_speech_ms': max(frame.speech_ms for frame in frames)}


def analyze_trial(frames, baseline):
    """Return an inconclusive result when no physical microphone echo is shown."""
    reference_threshold = max(32.0, baseline['rms']['reference'] * 4)
    active_indices = [index for index, frame in enumerate(frames)
                      if math.sqrt(power(frame.reference)) >= reference_threshold]
    # Give each trial's first 0.5 s of playback time an explicit settling window.
    settled_indices = [index for index in active_indices
                       if index >= active_indices[0] + 25] if active_indices else []
    active = [frames[index] for index in settled_indices]
    result = {'reference_activity_threshold_rms': round(reference_threshold, 2),
              'reference_active_frames': len(active_indices),
              'settled_reference_active_frames': len(active),
              'settling_window_seconds': .5,
              'signal_measurable': False, 'suppression_observed': False,
              'vad_candidate_during_speaker_only': None,
              'status': 'inconclusive'}
    if len(active) < 15:
        result['reason'] = 'Less than 0.3 seconds of settled reference activity'
        return result
    metrics = summarize_frames(active)
    all_reference = b''.join(frame.reference for frame in frames)
    mic_correlation = peak_correlation(all_reference, b''.join(frame.mic for frame in frames))
    clean_correlation = peak_correlation(all_reference, b''.join(frame.clean for frame in frames))
    mic_rms = metrics['rms']['mic']
    energy_rise = mic_rms / max(1.0, baseline['rms']['mic'])
    # A nonzero reference alone only proves that digital playback is present.
    # Require measurable microphone energy correlated with playback. An energy
    # rise alone could be another room sound, so it cannot prove echo pickup.
    echo_present = mic_rms >= 12 and (mic_correlation['absolute_peak'] or 0) >= .12
    attenuation = db_ratio(metrics['power']['mic'], metrics['power']['clean'])
    excess_mic = metrics['power']['mic'] - baseline['power']['mic']
    excess_clean = metrics['power']['clean'] - baseline['power']['clean']
    result.update(metrics=metrics, microphone_energy_rise_vs_baseline=round(energy_rise, 3),
        microphone_reference_correlation=mic_correlation,
        clean_reference_correlation=clean_correlation,
        microphone_echo_detected=echo_present,
        mic_to_clean_attenuation_db=attenuation,
        excess_energy_erle_estimate_db=db_ratio(excess_mic, excess_clean) if excess_mic > 0 else None,
        signal_measurable=echo_present,
        vad_candidate_during_speaker_only=metrics['maximum_reported_speech_ms'] >= 200,
        suppression_observed=echo_present and attenuation >= MIN_ATTENUATION_DB)
    if not echo_present:
        result['reason'] = 'Digital reference is present, but microphone pickup of the loudspeaker is not established'
    else:
        result['status'] = 'passed' if result['suppression_observed'] else 'needs_tuning'
        if not result['suppression_observed']:
            result['reason'] = f'Settled mic-to-clean attenuation is below {MIN_ATTENUATION_DB:g} dB'
    return result


def classify(baseline, trials, integrity_ok):
    measured = trials[-2:]
    if not integrity_ok:
        return 'failed', 'Transport or board health check failed'
    if len(measured) < 2 or any(not trial['signal_measurable'] for trial in measured):
        return 'inconclusive', 'Two measurable settled playback trials are required'
    if baseline['vad_ratio'] > .1:
        return 'inconclusive', 'Baseline contains speech-like activity; repeat in a quiet room'
    if any(not trial['suppression_observed'] for trial in measured):
        return 'needs_tuning', 'Echo suppression did not meet the stated energy criterion'
    if any(trial['vad_candidate_during_speaker_only'] for trial in measured):
        return 'needs_tuning', 'AEC attenuated playback, but VAD still produced sustained speech candidates'
    return 'passed', 'Echo pickup and settled attenuation were measured; speaker-only VAD stayed below 200 ms'


class Capture:
    def __init__(self, destination):
        self.frames = []
        self.changed = asyncio.Event()
        self.stopping = self.done = False
        self.error = None
        self.missing_frames = self.input_gap_frames = self.header_rms_mismatch_frames = 0
        self.trailing_bytes = 0
        self.wavs = {}
        for channel in CHANNELS:
            output = wave.open(str(destination / (channel + '.wav')), 'wb')
            output.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            self.wavs[channel] = output

    async def run(self, client, host):
        previous = None
        buffer = bytearray()
        try:
            timeout = ClientTimeout(total=None, connect=4, sock_read=6)
            async with client.get(f'http://{host}:84/speech?debug=1', timeout=timeout) as response:
                if response.status != 200:
                    raise VoiceError(f'Speech diagnostics are unavailable or busy (HTTP {response.status})')
                async for chunk in response.content.iter_chunked(8192):
                    buffer.extend(chunk)
                    while len(buffer) >= PACKET_BYTES:
                        frame = unpack_diagnostic(bytes(buffer[:PACKET_BYTES]))
                        del buffer[:PACKET_BYTES]
                        if previous is not None:
                            gap = (frame.sequence - previous.sequence - 1) & 0xffffffff
                            if gap:
                                self.missing_frames += gap
                                raise VoiceError('Missing or reordered diagnostic audio; correlation test stopped')
                            if frame.capture_us - previous.capture_us != 20000:
                                raise VoiceError('Diagnostic capture timestamps are discontinuous')
                        previous = frame
                        if frame.flags & 8:
                            self.input_gap_frames += 1
                            raise VoiceError('Board reports a microphone processing input gap')
                        actual = [math.sqrt(power(getattr(frame, channel))) for channel in CHANNELS]
                        reported = [frame.clean_rms, frame.mic_rms, frame.reference_rms]
                        if any(abs(a - b) > 2 for a, b in zip(actual, reported)):
                            self.header_rms_mismatch_frames += 1
                        self.frames.append(frame)
                        for channel, output in self.wavs.items():
                            output.writeframesraw(getattr(frame, channel))
                        self.changed.set()
                if not self.stopping:
                    raise VoiceError('Diagnostic audio connection ended before the test completed')
        except asyncio.CancelledError:
            if not self.stopping:
                self.error = 'Diagnostic capture was unexpectedly canceled'
            raise
        except Exception as error:
            self.error = str(error) if isinstance(error, (VoiceError, RuntimeError)) else type(error).__name__
        finally:
            self.trailing_bytes = len(buffer)
            self.done = True
            self.changed.set()

    def assert_live(self):
        if self.error or self.done:
            raise VoiceError(self.error or 'Diagnostic audio is no longer running')

    async def wait_frames(self, count, timeout=10):
        async with asyncio.timeout(timeout):
            while len(self.frames) < count:
                self.assert_live()
                self.changed.clear()
                await self.changed.wait()
        self.assert_live()

    def close_files(self):
        for output in self.wavs.values():
            output.close()


def validate_board(board):
    match = re.match(r'^(\d+)\.(\d+)\.(\d+)', str(board.get('firmware', '')))
    if not match or tuple(map(int, match.groups())) < (0, 6, 0):
        raise VoiceError('This diagnostic requires firmware 0.6.0 or newer')
    if not board.get('speech_frontend', {}).get('ready'):
        raise VoiceError('Board AEC/VAD is not ready')
    if not board.get('speaker', {}).get('ready'):
        raise VoiceError('Board loudspeaker is not ready')
    if board.get('speaker', {}).get('connected') or board.get('speaker', {}).get('active'):
        raise VoiceError('The board speaker is in use; this diagnostic did not start')
    if board.get('speech_frontend', {}).get('streaming'):
        raise VoiceError('The board speech stream is in use; this diagnostic did not start')


async def run(options):
    token = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    destination = ROOT / 'data' / 'duplex-acoustic-checks' / token
    report = {'test_type': 'real_board_speaker_only_aec_measurement', 'started_utc': now(),
        'cloud_api_used': False, 'paid_api_used': False, 'passed': False, 'status': 'not_started',
        'human_audibility_confirmed': False, 'human_listening_quality_tested': False,
        'near_end_speech_recognition_tested': False, 'double_talk_intelligibility_tested': False,
        'automatic_interruptions_tested': False,
        'method': {'speech_source': 'previously_synthesized_project_wav', 'rate': RATE,
            'frame_ms': 20, 'baseline_seconds': 2, 'minimum_settled_attenuation_db': MIN_ATTENUATION_DB,
            'correlation_sample_rate': 2000, 'correlation_lag_resolution_ms': 4,
            'assumption': 'Quiet room with no near-end speech during this speaker-only test',
            'limitation': 'Near-end speech and background noise affect energy-based ERLE estimates; this is not a calibrated double-talk test'}}
    capture = task = speaker = None
    acquired_speaker = False
    base = None
    trials = []
    destination.mkdir(parents=True, exist_ok=False)
    async with ClientSession(trust_env=False, timeout=ClientTimeout(total=8)) as client:
        async def get(url):
            async with client.get(url) as response:
                if response.status != 200:
                    raise VoiceError(f'Status request failed (HTTP {response.status})')
                return await response.json()

        async def guard():
            console = await get(options.console.rstrip('/') + '/api/status')
            if console.get('recording') or console.get('record_pending') or console.get('recording_pending'):
                raise VoiceError('A raw recording is active; this diagnostic did not take over')
            if console.get('voice', {}).get('active'):
                raise VoiceError('A voice call is active; this diagnostic did not take over')
            return console

        try:
            console = await guard()
            base = console['board_url'].rstrip('/')
            parsed = urlsplit(base)
            if parsed.scheme != 'http' or not parsed.hostname:
                raise VoiceError('Invalid local board address')
            host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
            before = await get(base + '/api/status')
            validate_board(before)
            report['initial_board'] = before
            config = (await get(options.console.rstrip('/') + '/api/voice/status'))['config']
            volume = config['volume'] if options.volume is None else options.volume
            if not isinstance(volume, int) or isinstance(volume, bool) or not 1 <= volume <= 100:
                raise VoiceError('An audible volume from 1 to 100 is required; no volume was changed')
            report['volume'] = volume
            fixture = ROOT / 'data' / 'cloud-speech-check.wav'
            with wave.open(str(fixture), 'rb') as source:
                if source.getsampwidth() != 2 or source.getnchannels() != 1 or source.getcomptype() != 'NONE':
                    raise VoiceError('The speech fixture must be uncompressed 16-bit mono PCM')
                duration = source.getnframes() / source.getframerate()
                if not .5 <= duration <= 15:
                    raise VoiceError('The saved speech fixture length must be 0.5 to 15 seconds')
                pcm = PCMConverter(source.getframerate(), 48000).convert(source.readframes(source.getnframes()))
            report.update(fixture='data/cloud-speech-check.wav', fixture_seconds=round(duration, 3))
            capture = Capture(destination)
            task = asyncio.create_task(capture.run(client, host))
            await capture.wait_frames(100, timeout=10)
            baseline_count = len(capture.frames)
            report['baseline'] = summarize_frames(capture.frames[:100])
            # Check again after the baseline, before acquiring or playing sound.
            await guard()
            speaker = BoardSpeaker(client, base)
            await speaker.connect()
            acquired_speaker = True
            for repeat in range(options.repeats):
                await guard()
                capture.assert_live()
                start = len(capture.frames)
                epoch = await speaker.begin(volume)
                for offset in range(0, len(pcm), 1920):
                    capture.assert_live()
                    await speaker.send(pcm[offset:offset + 1920], epoch)
                await speaker.finish(epoch)
                # Include delayed DSP/network packets and the acoustic/DMA tail.
                await capture.wait_frames(len(capture.frames) + 20, timeout=6)
                end = len(capture.frames)
                trials.append({'repeat': repeat + 1, 'start_frame': start, 'end_frame': end,
                    'speaker_consumed_frames': speaker.consumed})
            await capture.wait_frames(len(capture.frames) + 50, timeout=6)
            report['baseline_frame_count'] = baseline_count
            report['sent_speaker_frames'] = speaker.total_sent
            report['status'] = 'measured'
        except Exception as error:
            report['status'] = 'failed' if acquired_speaker else 'refused_or_unavailable'
            report['error'] = str(error) if isinstance(error, (VoiceError, RuntimeError)) else type(error).__name__
        finally:
            if speaker:
                if acquired_speaker:
                    try:
                        await speaker.close()
                    except Exception as error:
                        report['speaker_cleanup_error'] = type(error).__name__
                elif speaker.ws:
                    # A rejected/busy handshake must not issue the independent
                    # HTTP cancellation that could mute another owner's sound.
                    try:
                        await speaker.ws.close()
                    except Exception as error:
                        report['unowned_socket_close_error'] = type(error).__name__
            if capture:
                capture.stopping = True
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if capture:
                capture.close_files()
                report['capture'] = {'frames': len(capture.frames), 'seconds': round(len(capture.frames) * FRAME_SECONDS, 3),
                    'missing_frames': capture.missing_frames, 'input_gap_frames': capture.input_gap_frames,
                    'header_rms_mismatch_frames': capture.header_rms_mismatch_frames,
                    'error': capture.error, 'trailing_transport_bytes_on_close': capture.trailing_bytes,
                    'artifacts': {channel: str((destination / (channel + '.wav')).relative_to(ROOT)).replace('\\', '/')
                                  for channel in CHANNELS}}
            if base:
                try:
                    after = await get(base + '/api/status')
                    report['final_board'] = after
                except Exception as error:
                    report['final_status_error'] = type(error).__name__
    if report['status'] == 'measured':
        before, after = report['initial_board'], report.get('final_board', {})
        frontend = after.get('speech_frontend', {})
        health = {
            'same_boot': after.get('boot_id') == before.get('boot_id'),
            'microphone_read_errors_unchanged': after.get('audio', {}).get('read_errors') == before.get('audio', {}).get('read_errors'),
            'speaker_write_errors_unchanged': after.get('speaker', {}).get('write_errors') == before.get('speaker', {}).get('write_errors'),
            'speech_processing_errors_unchanged': frontend.get('process_errors') == before.get('speech_frontend', {}).get('process_errors'),
            'speech_input_drops_zero': frontend.get('input_drops') == 0,
            'speech_output_drops_zero': frontend.get('output_drops') == 0,
            'speaker_muted_after_test': not after.get('speaker', {}).get('active', True) and not after.get('speaker', {}).get('amplifier_enabled', True),
            'diagnostic_stream_intact': not capture.error and not capture.missing_frames and not capture.input_gap_frames,
            'header_rms_matches_pcm': capture.header_rms_mismatch_frames == 0,
            'cleanup_succeeded': not report.get('speaker_cleanup_error') and not report.get('final_status_error')}
        report['health_checks'] = health
        report['trials'] = [{**trial, **analyze_trial(capture.frames[trial['start_frame']:trial['end_frame']], report['baseline'])}
                            for trial in trials]
        status, reason = classify(report['baseline'], report['trials'], all(health.values()))
        report.update(status=status, passed=status == 'passed', conclusion=reason)
    report['finished_utc'] = now()
    report_path = destination / 'report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT / 'data' / 'duplex-acoustic-check.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'passed': report['passed'], 'error': report.get('error'),
        'conclusion': report.get('conclusion'), 'report': str(report_path.relative_to(ROOT)).replace('\\', '/')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 2 if report['status'] == 'inconclusive' else 1


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console', default='http://127.0.0.1:8765')
    parser.add_argument('--volume', type=int, choices=range(1, 101), metavar='1..100', default=None,
                        help='Default: current saved voice volume; this test does not change the saved setting')
    parser.add_argument('--repeats', type=int, choices=(2, 3), default=3)
    options = parser.parse_args()
    parsed = urlsplit(options.console)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username or parsed.password:
        parser.error('--console must be a local HTTP console URL')
    return options


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(run(arguments())))
