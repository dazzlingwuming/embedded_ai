"""Opt-in real visual API checks: synthetic labels, then production camera SSE.

Without --run-cloud this only validates fixtures/parsers in memory. Real runs
make at most two direct model turns and one production /api/ai/chat turn, with
no retries. They never start ASR, TTS, microphone capture or speaker playback.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import ClientSession, ClientTimeout
from PIL import Image, ImageDraw, ImageFont

from pc_backend.ai_client import capture_stream_events, settings, stream_reply


PROMPT = (
    'Inspect only the image attached to this turn. Return one JSON object with '
    'keys "shape", "color", and "digits". Use simple lowercase English for the '
    'large geometric shape and its color. Copy the visible six-digit label '
    'exactly as a string, preserving leading zeros. No prose or Markdown. '
    'Earlier conversation describes a different image and must not replace this image.'
)
SYSTEM = 'Observe the current supplied image. Do not infer image contents from filenames or old answers.'
CAMERA_PROMPT = '请自然描述你现在实际看到的画面，简短回答。只说能看清的内容；看不清或缺少图片请直接说明，别猜测。'
EVENT_FIELDS = frozenset((
    'event', 'elapsed_ms', 'request_index', 'model', 'max_tokens',
    'prefix_continuation', 'image_count', 'image_bytes', 'finish_reason',
    'usage', 'response_model', 'characters',
))
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def safe_error(error):
    # Exceptions should never include credentials. Mask token-like text too,
    # without writing loaded configuration/headers to a log or report.
    return re.sub(r'\bsk[-_][A-Za-z0-9_.-]+', '[redacted]', str(error))[:1000] or type(error).__name__


@dataclass
class Fixture:
    jpeg: bytes
    expected: dict
    frame_id: str

    def image(self):
        return {'jpeg': self.jpeg, 'frame_id': self.frame_id,
                'received_utc': utc_now(), 'quality': {'synthetic_fixture': True}}


def make_fixtures():
    first_code = ''.join(secrets.choice('0123456789') for _ in range(6))
    second_code = first_code
    while second_code == first_code:
        second_code = ''.join(secrets.choice('0123456789') for _ in range(6))
    result = []
    for index, (shape, color, fill, digits) in enumerate((
        ('circle', 'red', (220, 30, 25), first_code),
        ('triangle', 'blue', (25, 80, 225), second_code),
    ), 1):
        with Image.new('RGB', (640, 480), 'white') as image:
            draw = ImageDraw.Draw(image)
            if shape == 'circle':
                draw.ellipse((215, 45, 425, 255), fill=fill)
            else:
                draw.polygon(((320, 35), (185, 270), (455, 270)), fill=fill)
            font = ImageFont.load_default(size=82)
            box = draw.textbbox((0, 0), digits, font=font)
            x = (640 - (box[2] - box[0])) / 2 - box[0]
            y = 350 - box[1]
            draw.text((x, y), digits, font=font, fill='black')
            with BytesIO() as output:
                image.save(output, 'JPEG', quality=95, subsampling=0)
                encoded = output.getvalue()
        expected = {'shape': shape, 'color': color, 'digits': digits}
        # The answers exist only in pixels and the separate assertion fixture.
        for value in expected.values():
            if re.search(r'\b' + re.escape(value) + r'\b', PROMPT + SYSTEM, re.I):
                raise RuntimeError('The visual prompt accidentally includes a fixture answer')
        result.append(Fixture(encoded, expected, f'fixture-{index}'))
    return result


def parse_observation(text):
    text = text.strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.S | re.I)
        if not match:
            raise ValueError('Malformed fenced JSON response')
        text = match[1]
    observation = json.loads(text)
    if not isinstance(observation, dict):
        raise ValueError('The model did not return a JSON object')
    return observation


def semantic_checks(observation, expected):
    aliases = {'圆形': 'circle', '圆': 'circle', 'round': 'circle', 'circular': 'circle',
               '三角形': 'triangle', '三角': 'triangle', 'triangular': 'triangle',
               '红': 'red', '红色': 'red', '蓝': 'blue', '蓝色': 'blue'}
    normalized = {}
    for field in ('shape', 'color'):
        value = observation.get(field)
        value = value.strip().lower() if isinstance(value, str) else None
        normalized[field] = aliases.get(value, value)
    digits = observation.get('digits')
    return {'shape_matches': normalized['shape'] == expected['shape'],
            'color_matches': normalized['color'] == expected['color'],
            'digits_matches': isinstance(digits, str)
            and re.fullmatch(r'[0-9]{6}', digits) is not None and digits == expected['digits']}


def safe_image_path(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'/api/vision/frame/[A-Za-z0-9_-]+/[A-Za-z0-9_.:-]+\.jpg', value):
        raise ValueError('Selected image URL is not a local production vision frame path')
    return value


def decode_sse_data(lines):
    if not lines:
        return None
    event = json.loads(b'\n'.join(lines))
    if not isinstance(event, dict):
        raise ValueError('Production SSE event is not an object')
    return event


def offline_check():
    fixtures = make_fixtures()
    for fixture in fixtures:
        with Image.open(BytesIO(fixture.jpeg)) as image:
            assert image.format == 'JPEG' and image.size == (640, 480)
            image.load()
        parsed = parse_observation(json.dumps(fixture.expected))
        assert all(semantic_checks(parsed, fixture.expected).values())
        assert all(semantic_checks(parse_observation('```json\n' + json.dumps(parsed) + '\n```'),
                                   fixture.expected).values())
        assert len(fixture.jpeg) < MAX_IMAGE_BYTES
    assert fixtures[0].expected['digits'] != fixtures[1].expected['digits']
    assert not all(semantic_checks(fixtures[0].expected, fixtures[1].expected).values())
    assert safe_image_path('/api/vision/frame/vs-test/vf-test.jpg').startswith('/')
    for invalid in ('https://example.com/picture.jpg', '//example.com/a.jpg', '/api/vision/frame/../a.jpg'):
        try:
            safe_image_path(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe image path was accepted')
    assert decode_sse_data([b'{"done": true}']) == {'done': True}
    assert decode_sse_data([]) is None
    return {'passed': True, 'mode': 'offline_fixture_and_parser_check',
            'cloud_api_used': False, 'board_used': False,
            'fixture_count': len(fixtures), 'fixture_bytes': [len(item.jpeg) for item in fixtures]}


async def get_json(client, url):
    async with client.get(url, allow_redirects=False, timeout=ClientTimeout(total=5)) as response:
        if response.status != 200:
            raise RuntimeError(f'Status request failed (HTTP {response.status})')
        value = await response.json()
        if not isinstance(value, dict):
            raise ValueError('Status response is not an object')
        return value


async def idle_guard(client, console, require_camera=False):
    status = await get_json(client, console + '/api/status')
    voice = await get_json(client, console + '/api/voice/status')
    if (status.get('recording') or status.get('record_pending') or status.get('recording_pending')
            or voice.get('recording_active') or status.get('voice', {}).get('active')
            or voice.get('session', {}).get('active')):
        raise RuntimeError('User recording or voice session is active; no takeover attempted')
    board_url = status.get('board_url', '').rstrip('/')
    parsed = urlsplit(board_url)
    if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Console did not provide a valid board HTTP address')
    board = await get_json(client, board_url + '/api/status')
    speaker = board.get('speaker', {})
    speech = board.get('speech_frontend', {})
    audio = board.get('audio', {})
    if any(speaker.get(key) for key in ('active', 'connected', 'pa_enabled')) \
            or speech.get('streaming') or audio.get('streaming'):
        raise RuntimeError('Board audio is in use; this check did not acquire or cancel it')
    vision = await get_json(client, console + '/api/vision/status')
    if require_camera and not all(vision.get(key) for key in ('enabled', 'ready', 'observing')):
        raise RuntimeError('Production camera observation is not enabled and ready; configuration unchanged')
    return {'board_connected': bool(status.get('board_connected')),
            'board_boot_id': board.get('boot_id'), 'firmware': board.get('firmware'),
            'recording_active': False, 'voice_active': False, 'speaker_active': False,
            'speech_streaming': False, 'vision_enabled': vision.get('enabled'),
            'vision_ready': vision.get('ready'), 'vision_observing': vision.get('observing')}


async def synthetic_turn(fixture, config, history, destination, index, result):
    filename = destination / f'synthetic-{index}.jpg'
    filename.write_bytes(fixture.jpeg)
    result.update({'input_origin': 'synthetic_jpeg_fixture', 'frame_id': fixture.frame_id,
              'image_file': filename.relative_to(ROOT).as_posix(),
              'image_sha256': hashlib.sha256(fixture.jpeg).hexdigest(),
              'image_bytes': len(fixture.jpeg), 'expected': fixture.expected,
              'prompt': PROMPT, 'history_turns': len(history), 'passed': False})
    chunks = []
    started = time.monotonic()
    events = []
    # Transient test budget only; no file, credential, model or server setting is
    # changed. Disabling prefix continuation makes each fixture exactly one POST.
    bounded_config = {**config, 'max_tokens': 256, 'max_continuations': 0}
    try:
        with patch('pc_backend.ai_client.settings', return_value=bounded_config), capture_stream_events() as events:
            async with aclosing(stream_reply(PROMPT, history, SYSTEM, images=[fixture.image()])) as reply:
                async for part in reply:
                    chunks.append(part)
        text = ''.join(chunks)
        result['output_text'] = text
        result['observation'] = parse_observation(text)
        result['checks'] = semantic_checks(result['observation'], fixture.expected)
        starts = [event for event in events if event['event'] == 'request_started']
        finishes = [event for event in events if event['event'] == 'request_finished']
        result['checks']['one_real_image_sent'] = (len(starts) == 1
            and starts[0].get('image_count') == 1 and starts[0].get('image_bytes') == len(fixture.jpeg))
        result['checks']['provider_finished_normally'] = (len(finishes) == 1
            and finishes[0].get('finish_reason') == 'stop')
        result['passed'] = all(result['checks'].values())
    except Exception as error:
        result['error'] = safe_error(error)
        result['output_text'] = ''.join(chunks)
    except asyncio.CancelledError:
        result['error'] = 'Overall verification deadline or cancellation; request was not retried'
        result['output_text'] = ''.join(chunks)
        raise
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        result['stream_events'] = [{key: value for key, value in event.items() if key in EVENT_FIELDS}
                                   for event in events]
        result['provider_request_count'] = sum(event['event'] == 'request_started' for event in events)
    return result


async def production_turn(client, console, destination, result):
    result.update({'input_origin': 'real_camera_production_text_turn', 'passed': False,
              'frame_transport_passed': False, 'scene_accuracy_verified': False,
              'scene_ground_truth_available': False, 'prompt': CAMERA_PROMPT,
              'asr_used': False, 'tts_used': False, 'human_listening_tested': False,
              'vision_events': [], 'frames': [], 'output_text': '', 'done_received': False,
              'provider_request_count': None,
              'provider_metrics_scope': 'Separate production process; direct stream metrics unavailable here'})
    started = time.monotonic()
    fetched = set()
    data_lines = []

    async def handle(event):
        if isinstance(event.get('vision'), dict):
            vision = event['vision']
            result['vision_events'].append(vision)
            for metadata in vision.get('selected', []):
                identity = (vision.get('selection_id'), metadata.get('frame_id'))
                if identity in fetched:
                    continue
                path = safe_image_path(metadata.get('image_url'))
                async with client.get(console + path, allow_redirects=False,
                                      timeout=ClientTimeout(total=5)) as image_response:
                    if image_response.status != 200:
                        raise RuntimeError(f'Selected camera JPEG fetch failed (HTTP {image_response.status})')
                    chunks = []
                    size_received = 0
                    async for chunk in image_response.content.iter_chunked(65536):
                        size_received += len(chunk)
                        if size_received > MAX_IMAGE_BYTES:
                            raise ValueError('Selected camera JPEG exceeds 2 MiB')
                        chunks.append(chunk)
                    body = b''.join(chunks)
                    if image_response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg':
                        raise ValueError('Selected camera frame did not have JPEG content type')
                actual_sha = hashlib.sha256(body).hexdigest()
                with Image.open(BytesIO(body)) as image:
                    if image.format != 'JPEG':
                        raise ValueError('Selected camera frame is not actual JPEG')
                    size = image.size
                    image.load()
                filename = destination / f'real-camera-{len(fetched) + 1}.jpg'
                filename.write_bytes(body)
                age = metadata.get('age_seconds')
                valid_age = type(age) in (int, float) and 0 <= age <= 3.001
                frame = {**metadata, 'image_file': filename.relative_to(ROOT).as_posix(),
                         'downloaded_bytes': len(body), 'actual_sha256': actual_sha,
                         'sha256_matches': actual_sha == metadata.get('sha256'),
                         'fresh_receipt_time': valid_age, 'decoded_dimensions': list(size)}
                result['frames'].append(frame)
                fetched.add(identity)
        if isinstance(event.get('delta'), str):
            result['output_text'] += event['delta']
        if event.get('done') is True:
            result['done_received'] = True
        if event.get('error'):
            raise RuntimeError(safe_error(event['error']))

    try:
        async with client.post(console + '/api/ai/chat', json={'text': CAMERA_PROMPT, 'history': []},
                               allow_redirects=False, timeout=ClientTimeout(total=60)) as response:
            result['http_status'] = response.status
            if response.status != 200:
                raise RuntimeError(f'Production chat failed (HTTP {response.status}); not retried')
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise ValueError('Production chat did not return SSE')
            received_bytes = 0
            while True:
                line = await response.content.readline()
                if not line:
                    if data_lines:
                        await handle(decode_sse_data(data_lines))
                    break
                received_bytes += len(line)
                if received_bytes > 1024 * 1024:
                    raise ValueError('Production SSE exceeded the diagnostic byte limit')
                stripped = line.rstrip(b'\r\n')
                if not stripped:
                    event = decode_sse_data(data_lines)
                    data_lines.clear()
                    if event is not None:
                        await handle(event)
                elif stripped.startswith(b'data:'):
                    data_lines.append(stripped[5:].lstrip(b' '))
        frames = result['frames']
        result['frame_transport_passed'] = bool(frames and result['done_received']
            and result['output_text'].strip() and all(frame['sha256_matches']
            and frame['fresh_receipt_time'] for frame in frames))
        low_texture = any(frame.get('quality', {}).get('low_texture') for frame in frames)
        result['low_texture'] = low_texture
        result['scene_accuracy_status'] = ('inconclusive_low_texture_and_no_ground_truth'
                                           if low_texture else 'not_verified_no_scene_ground_truth')
        result['passed'] = result['frame_transport_passed']
        if not frames:
            result['failure_reason'] = 'no_real_camera_frame_attached'
        elif not result['frame_transport_passed']:
            result['failure_reason'] = 'missing_done_text_or_valid_fresh_matching_jpeg'
        if low_texture:
            result['semantic_limitation'] = 'Low texture is usable input, but cannot validate detailed scene recognition'
    except Exception as error:
        result['error'] = safe_error(error)
    except asyncio.CancelledError:
        result['error'] = 'Overall verification deadline or cancellation; production request was not retried'
        raise
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result


def write_reports(report, destination):
    report['updated_utc'] = utc_now()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    # Metadata allowlists and the adapter's metric collector exclude image
    # payloads and credentials. A final token pattern guard also covers errors.
    if re.search(r'\bsk[-_][A-Za-z0-9_.-]{12,}', text) or 'data:image/jpeg;base64,' in text:
        raise RuntimeError('Unsafe report content was blocked')
    (destination / 'report.json').write_text(text, encoding='utf-8')
    compact = {key: report.get(key) for key in (
        'started_utc', 'updated_utc', 'status', 'passed', 'scoped_pipeline_checks_passed',
        'cloud_api_used', 'synthetic_semantics_passed', 'production_frame_transport_passed',
        'real_scene_accuracy_verified', 'elapsed_seconds', 'error', 'scope',
    )}
    compact['report_file'] = (destination / 'report.json').relative_to(ROOT).as_posix()
    compact['synthetic'] = [{key: item.get(key) for key in (
        'image_file', 'expected', 'observation', 'checks', 'passed', 'elapsed_seconds',
        'provider_request_count', 'error')} for item in report['synthetic']]
    real = report.get('production_camera')
    if real:
        compact['production_camera'] = {key: real.get(key) for key in (
            'frame_transport_passed', 'scene_accuracy_status', 'scene_accuracy_verified',
            'low_texture', 'failure_reason', 'error', 'done_received', 'elapsed_seconds', 'output_text')}
        compact['production_camera']['frames'] = real['frames']
    (ROOT / 'data' / 'vision-context-check.json').write_text(
        json.dumps(compact, ensure_ascii=False, indent=2), encoding='utf-8')
    return compact


async def run(options):
    console = options.console.rstrip('/')
    parsed = urlsplit(console)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} \
            or parsed.username or parsed.password or parsed.path:
        raise ValueError('Use a loopback console URL, normally http://127.0.0.1:8765')
    offline_check()
    destination = ROOT / 'data' / 'vision-context-checks' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    destination.mkdir(parents=True, exist_ok=False)
    report = {'started_utc': utc_now(), 'passed': False, 'status': 'running',
              'cloud_api_used': False, 'synthetic': [], 'production_frame_transport_passed': False,
              'real_scene_accuracy_verified': False, 'retry_policy': 'no paid retries',
              'asr_used': False, 'tts_used': False, 'human_listening_tested': False,
              'scope': 'Two exact synthetic label checks; optional real-frame linkage/reply, not scene accuracy',
              'timestamp_scope': 'PC receipt/application SSE timing; no sensor exposure-clock claim'}
    start = time.monotonic()
    try:
        async with asyncio.timeout(120):
            config = settings()
            if not config['key']:
                raise RuntimeError('DeepSeek credentials are not configured')
            report['configured_model'] = config['model']
            report['direct_networking'] = config['proxy'] is None
            timeout = ClientTimeout(total=6)
            async with ClientSession(trust_env=False, timeout=timeout) as client:
                report['initial_idle_guard'] = await idle_guard(client, console,
                                                               require_camera=not options.synthetic_only)
                history = []
                for index, fixture in enumerate(make_fixtures(), 1):
                    result = {}
                    report['synthetic'].append(result)
                    await synthetic_turn(fixture, config, history, destination, index, result)
                    report['cloud_api_used'] = report['cloud_api_used'] or result['provider_request_count'] > 0
                    write_reports(report, destination)
                    if result.get('error'):
                        raise RuntimeError('Synthetic model turn failed; remaining paid requests were not attempted')
                    history.extend(({'role': 'user', 'content': PROMPT},
                                    {'role': 'assistant', 'content': result['output_text']}))
                report['synthetic_semantics_passed'] = all(item['passed'] for item in report['synthetic'])
                if not options.synthetic_only:
                    report['before_camera_idle_guard'] = await idle_guard(client, console, require_camera=True)
                    report['production_camera'] = {}
                    report['production_chat_posts_attempted'] = 1
                    await production_turn(client, console, destination, report['production_camera'])
                    report['production_frame_transport_passed'] = report['production_camera']['frame_transport_passed']
                report['final_idle_guard'] = await idle_guard(client, console)
                report['board_boot_unchanged'] = (report['initial_idle_guard']['board_boot_id']
                    == report['final_idle_guard']['board_boot_id'])
        pipeline = report.get('synthetic_semantics_passed', False) and report['board_boot_unchanged']
        if not options.synthetic_only:
            pipeline = pipeline and report['production_frame_transport_passed']
        report['scoped_pipeline_checks_passed'] = bool(pipeline)
        low_texture = report.get('production_camera', {}).get('low_texture', False)
        report['passed'] = bool(pipeline and not low_texture)
        report['status'] = ('inconclusive_real_scene_low_texture' if pipeline and low_texture
                            else 'passed_scoped_checks' if pipeline else 'failed')
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = safe_error(error)
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - start, 3)
        compact = write_reports(report, destination)
    print(json.dumps(compact, ensure_ascii=True), flush=True)
    return 0 if report['passed'] else 2 if report['status'].startswith('inconclusive') else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-cloud', action='store_true', help='Explicitly enable real paid API requests')
    parser.add_argument('--synthetic-only', action='store_true', help='Skip the production camera turn')
    parser.add_argument('--console', default='http://127.0.0.1:8765')
    options = parser.parse_args()
    if not options.run_cloud:
        print(json.dumps(offline_check(), ensure_ascii=True))
        return 0
    return asyncio.run(run(options))


if __name__ == '__main__':
    sys.exit(main())
