"""Loopback-only BenchLink test console, real microphone capture and simulated RPC."""
import argparse
from array import array
import asyncio
from collections import deque
from contextlib import aclosing
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import re
import struct
import time
import urllib.parse
import uuid
import wave

from aiohttp import web, ClientSession, ClientTimeout, ClientError, WSMsgType, TCPConnector
from simulators.instrument import SimulatorFleet
from pc_backend.ai_client import AIError, public_status as ai_status, stream_reply
from pc_backend.duplex_voice import DuplexVoiceBridge
from pc_backend.voice_config import public_config as voice_config, save_config as save_voice_config
from pc_backend.voice_protocol import VoiceError
from pc_backend.camera_relay import CameraRelay, valid_photo_name

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
UI = ROOT / 'pc_ui' / 'dist'
HEADER = struct.Struct('<4sIQIHH')
TERMINAL = {'completed', 'applied', 'rejected', 'expired', 'unknown'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def valid_board_url(value):
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme != 'http' or parts.username or parts.password or parts.port not in (None, 80) or parts.path not in ('', '/') or parts.query or parts.fragment:
        raise ValueError('请输入开发板的局域网 HTTP 地址，例如 http://192.168.2.38')
    host = parts.hostname or ''
    try:
        allowed = ipaddress.ip_address(host).is_private
    except ValueError:
        allowed = host.endswith('.local') and re.fullmatch(r'[A-Za-z0-9.-]+', host)
    if not allowed:
        raise ValueError('开发板地址应是局域网 IP 或 .local 主机名')
    return f'http://{host}'


class Recording:
    def __init__(self, board, label):
        self.session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8]
        self.directory = DATA / 'sessions' / self.session_id
        self.directory.mkdir(parents=True)
        self.wav = wave.open(str(self.directory / 'audio.wav'), 'wb')
        self.wav.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        self.meta = {'session_id': self.session_id, 'label': label[:100], 'source': 'real_microphone',
                     'started_utc': utc_now(), 'status': 'recording', 'board': board,
                     'sample_rate': 48000, 'bits_per_sample': 16, 'channels': 1,
                     'samples_written': 0, 'samples_received': 0, 'missing_frames': 0,
                     'silence_samples_inserted': 0, 'clipped_samples': 0, 'gaps': [],
                     'timestamp_meaning': 'capture_us is gateway completion time minus frame duration; not synchronized instrument sampling time',
                     'processing': 'No speech denoise, AGC, VAD, silence trimming, or PC recording resampling. ES8311 fixed codec filters remain.'}
        self.previous_sequence = None
        self.started_monotonic = time.monotonic()
        self._save()

    def _save(self):
        temporary = self.directory / 'metadata.tmp'
        temporary.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.directory / 'metadata.json')

    def event(self, payload):
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(payload, ensure_ascii=False) + '\n')

    def add(self, pcm, sequence, capture_us, count):
        if self.previous_sequence is not None:
            missing = (sequence - self.previous_sequence - 1) & 0xffffffff
            if missing > 250:
                raise RuntimeError('Audio sequence discontinuity exceeds five seconds')
            if missing:
                silence = missing * count
                self.wav.writeframesraw(b'\0' * silence * 2)
                self.meta['missing_frames'] += missing
                self.meta['silence_samples_inserted'] += silence
                self.meta['gaps'].append({'after_sample': self.meta['samples_written'],
                                          'missing_frames': missing, 'silence_samples': silence})
                self.meta['samples_written'] += silence
        self.previous_sequence = sequence
        self.wav.writeframesraw(pcm)
        values = array('h', pcm)
        self.meta['clipped_samples'] += sum(abs(value) >= 32760 for value in values)
        self.meta['samples_received'] += count
        self.meta['samples_written'] += count
        self.meta['last_capture_us'] = capture_us
        if 'first_capture_us' not in self.meta:
            self.meta['first_capture_us'] = capture_us
        if sequence % 50 == 0:
            self._save()
        if self.meta['samples_written'] >= 48000 * 7200:
            raise RuntimeError('Two-hour WAV recording limit reached; start a new recording')

    def finish(self, status='completed', reason='user_stop'):
        self.wav.close()
        self.meta.update(status=status, stop_reason=reason, stopped_utc=utc_now(),
                         duration_seconds=self.meta['samples_written'] / 48000,
                         elapsed_seconds=round(time.monotonic() - self.started_monotonic, 3))
        self._save()
        return self.meta.copy()


class Console:
    def __init__(self, board_url):
        self.board_url = valid_board_url(board_url)
        self.board = {}
        self.board_seen = 0.0
        self.board_error = None
        self.http = None
        self.poll_task = None
        self.audio_task = None
        self.recording = None
        self.record_pending = False
        self.record_lock = asyncio.Lock()
        self.fleet = None
        self.viewers = {}
        self.audio_event = asyncio.Event()
        self.audio = {'source': 'real_microphone', 'connected': False, 'frames': 0,
                      'missing_frames': 0, 'viewer_drops': 0, 'peak_dbfs': -120.0,
                      'rms_dbfs': -120.0, 'last_error': None}
        self.last_audio = 0.0
        self.previous_audio_sequence = None
        self.commands = deque(maxlen=100)
        self.ai_lock = asyncio.Lock()
        self.camera = CameraRelay(self)
        self.voice = DuplexVoiceBridge(self)

    async def request(self, method, path, payload=None):
        async with self.http.request(method, self.board_url + path, json=payload,
                                     timeout=ClientTimeout(total=4)) as response:
            result = await response.json(content_type=None)
            if response.status >= 400:
                raise ValueError(result.get('error', f'HTTP {response.status}'))
            return result

    async def execute_command(self, command):
        deadline = time.monotonic() + min(30, max(.2, command['ttl_ms'] / 1000)) + 6
        path = '/api/commands/' + urllib.parse.quote(command['request_id'], safe='')
        ambiguous_submission = False
        try:
            result = await self.request('POST', '/api/commands', command)
        except ValueError as error:
            return {**command, 'state': 'rejected', 'reason': str(error), 'source': 'simulated'}
        except (ClientError, asyncio.TimeoutError, OSError):
            # Never repeat the POST: a lost response can hide an applied write.
            ambiguous_submission = True
            result = {**command, 'state': 'unknown', 'reason': 'gateway_response_lost', 'source': 'simulated'}
        self.log_event({'kind': 'command_state', **result})
        previous = result.get('state')
        while time.monotonic() < deadline and (ambiguous_submission or result.get('state') not in TERMINAL):
            await asyncio.sleep(.15)
            try:
                observed = await self.request('GET', path)
            except (ClientError, asyncio.TimeoutError, OSError):
                # The execution TTL remains unchanged. Only confirmation waits
                # longer to survive a brief network interruption.
                continue
            except ValueError:
                result.update(state='unknown', reason='gateway_state_not_available')
                break
            result = observed
            if ambiguous_submission:
                result['recovered_response'] = True
                ambiguous_submission = False
            if result.get('state') != previous:
                self.log_event({'kind': 'command_state', **result})
                previous = result.get('state')
        if result.get('state') not in TERMINAL:
            result.update(state='unknown', reason='result_not_confirmed')
        return result

    def log_event(self, event):
        event = {'pc_received_utc': utc_now(), **event}
        DATA.mkdir(exist_ok=True)
        with (DATA / 'commands.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(event, ensure_ascii=False) + '\n')
        if self.recording:
            self.recording.event(event)

    async def poll(self):
        while True:
            try:
                camera_only = self.board.get('mode') == 'camera_only'
                if camera_only:
                    try:
                        await self.camera.probe()
                    except ValueError:
                        camera_only = False
                if not camera_only:
                    async with self.http.get(self.board_url+'/api/status', allow_redirects=False,
                                             timeout=ClientTimeout(total=4)) as response:
                        camera_only = response.status == 404
                        if not camera_only:
                            if response.status != 200:
                                raise ValueError('开发板状态接口暂时不可用')
                            board = await response.json(content_type=None)
                    if camera_only:
                        await self.camera.probe()
                if camera_only:
                    board = {'board': 'Waveshare ESP32-P4-NANO', 'firmware': '摄像头程序',
                             'mode': 'camera_only', 'video_enabled': True, 'devices': [],
                             'audio': {'ready': False}, 'speaker': {'ready': False},
                             'physical_instruments_verified': False}
                elif board.get('video_enabled'):
                    await self.camera.probe()
                else:
                    self.camera.info = None
                previous_boot = self.board.get('boot_id')
                self.board, self.board_seen, self.board_error = board, time.monotonic(), None
                if previous_boot and board.get('boot_id') != previous_boot and self.recording:
                    self.finish_recording('interrupted', 'gateway_restarted')
            except (ClientError, ValueError, asyncio.TimeoutError, OSError) as error:
                self.board_error = str(error) or type(error).__name__
            await asyncio.sleep(1)

    @property
    def audio_required(self):
        return bool(self.viewers or self.recording or self.record_pending or
                    (self.voice.active and not self.voice.uses_separate_audio))

    def ensure_audio(self):
        if not self.audio_required:
            return
        if self.audio_task is None or self.audio_task.done():
            self.audio_task = asyncio.create_task(self.capture())

    def finish_recording(self, status='completed', reason='user_stop'):
        if not self.recording:
            return None
        recording, self.recording = self.recording, None
        result = recording.finish(status, reason)
        self.log_event({'kind': 'recording_finished', 'session_id': result['session_id'],
                        'status': status, 'reason': reason})
        return result

    async def capture(self):
        while self.audio_required:
            try:
                host = urllib.parse.urlsplit(self.board_url).hostname
                timeout = ClientTimeout(total=None, connect=3, sock_read=8)
                async with self.http.get(f'http://{host}:81/audio', timeout=timeout) as response:
                    if response.status != 200:
                        raise RuntimeError(f'Audio HTTP {response.status}')
                    buffer = bytearray()
                    self.previous_audio_sequence = None
                    self.audio.update(frames=0, missing_frames=0, viewer_drops=0)
                    async for chunk in response.content.iter_chunked(8192):
                        if not self.audio_required:
                            break
                        buffer.extend(chunk)
                        while len(buffer) >= HEADER.size:
                            magic, sequence, capture_us, rate, count, flags = HEADER.unpack_from(buffer)
                            if magic != b'BLA1' or rate != 48000 or count != 960:
                                raise RuntimeError('Invalid audio protocol or format')
                            length = HEADER.size + count * 2
                            if len(buffer) < length:
                                break
                            packet = bytes(buffer[:length])
                            del buffer[:length]
                            pcm = packet[HEADER.size:]
                            samples = array('h', pcm)
                            peak = max(abs(value) for value in samples)
                            rms = math.sqrt(sum(value * value for value in samples) / count)
                            if self.previous_audio_sequence is not None:
                                missed = (sequence - self.previous_audio_sequence - 1) & 0xffffffff
                                self.audio['missing_frames'] += missed if missed < 10000 else 0
                            self.previous_audio_sequence = sequence
                            self.audio.update(connected=True, last_error=None,
                                frames=self.audio['frames'] + 1, sequence=sequence,
                                peak_dbfs=round(20 * math.log10(max(1e-6, peak / 32768)), 1),
                                rms_dbfs=round(20 * math.log10(max(1e-6, rms / 32768)), 1))
                            self.last_audio = time.monotonic()
                            self.voice.offer_audio(packet)
                            if self.recording:
                                try:
                                    self.recording.add(pcm, sequence, capture_us, count)
                                except (OSError, RuntimeError) as error:
                                    self.finish_recording('interrupted', str(error))
                            for queue in tuple(self.viewers.values()):
                                if queue.full():
                                    queue.get_nowait()
                                    self.audio['viewer_drops'] += 1
                                queue.put_nowait(packet)
                            self.audio_event.set()
                    if self.audio_required:
                        raise RuntimeError('Audio connection ended')
            except asyncio.CancelledError:
                break
            except (ClientError, OSError, RuntimeError, asyncio.TimeoutError) as error:
                self.audio.update(connected=False, last_error=str(error) or type(error).__name__)
                self.voice.input_failed()
                if self.recording:
                    self.finish_recording('interrupted', self.audio['last_error'])
                await asyncio.sleep(1)
        self.audio['connected'] = False

    def status(self):
        age = time.monotonic() - self.board_seen if self.board_seen else None
        audio_age = time.monotonic() - self.last_audio if self.last_audio else None
        return {'board_url': self.board_url, 'board': self.board,
                'board_connected': age is not None and age < 4,
                'board_last_seen_seconds': round(age, 2) if age is not None else None,
                'board_error': self.board_error,
                'audio': {**self.audio, 'connected': self.audio['connected'] and audio_age is not None and audio_age < 2,
                          'last_frame_age_seconds': round(audio_age, 3) if audio_age is not None else None},
                'recording': self.recording.meta.copy() if self.recording else None,
                'simulator': self.fleet.status() if self.fleet else {'running': False},
                'commands': list(self.commands), 'voice': self.voice.status(), 'camera': self.camera.status()}


def console(request):
    return request.app['console']


@web.middleware
async def handle_errors(request, handler):
    if request.method not in {'GET', 'HEAD'}:
        origin = request.headers.get('Origin')
        if origin and urllib.parse.urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text='Cross-origin write requests are not allowed')
    try:
        return await handler(request)
    except (ValueError, KeyError, json.JSONDecodeError, VoiceError) as error:
        return web.json_response({'error': str(error)}, status=400)


async def get_status(request):
    return web.json_response(console(request).status())


async def set_config(request):
    state = console(request)
    if state.recording or state.record_pending or state.voice.active:
        return web.json_response({'error': '请先停止录音和语音通话，再更换开发板'}, status=409)
    payload = await request.json()
    address = valid_board_url(payload['board_url'])
    if state.fleet:
        await asyncio.to_thread(state.fleet.stop)
        state.fleet = None
    if state.audio_task and not state.audio_task.done():
        state.audio_task.cancel()
        await asyncio.gather(state.audio_task, return_exceptions=True)
    await state.camera.reset()
    state.board_url, state.board, state.board_seen = address, {}, 0
    state.audio_event.clear()
    (ROOT / 'local' / 'pc.json').write_text(json.dumps({'board_url': address}), encoding='utf-8')
    if state.viewers:
        state.ensure_audio()
    return web.json_response({'board_url': address})


async def start_recording(request):
    state = console(request)
    async with state.record_lock:
        if state.voice.active:
            return web.json_response({'error': '请先结束语音通话，避免喇叭影响原始收音'}, status=409)
        if state.recording or state.record_pending:
            return web.json_response({'error': '已有录音正在进行'}, status=409)
        if not state.status()['board_connected'] or not state.board.get('audio', {}).get('ready'):
            return web.json_response({'error': '开发板或麦克风尚未就绪'}, status=503)
        payload = await request.json()
        state.record_pending = True
        try:
            state.audio_event.clear()
            state.ensure_audio()
            await asyncio.wait_for(state.audio_event.wait(), 5)
            state.recording = Recording(state.board.copy(), str(payload.get('label', '现场录音')))
            state.log_event({'kind': 'recording_started', 'session_id': state.recording.session_id})
            return web.json_response(state.recording.meta)
        except asyncio.TimeoutError:
            return web.json_response({'error': '未收到真实音频数据，请检查开发板连接'}, status=503)
        finally:
            state.record_pending = False


async def stop_recording(request):
    state = console(request)
    async with state.record_lock:
        result = state.finish_recording()
    return web.json_response(result or {'status': 'idle'})


async def list_recordings(request):
    items = []
    for path in sorted((DATA / 'sessions').glob('*/metadata.json'), reverse=True)[:100]:
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
            item.pop('board', None)
            items.append(item)
        except (OSError, ValueError):
            continue
    return web.json_response({'recordings': items})


async def recording_file(request):
    session, name = request.match_info['session'], request.match_info['name']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', session) or name not in {'audio.wav', 'metadata.json', 'events.jsonl'}:
        raise web.HTTPNotFound()
    path = DATA / 'sessions' / session / name
    if not path.is_file():
        raise web.HTTPNotFound()
    if console(request).recording and console(request).recording.session_id == session and name == 'audio.wav':
        return web.json_response({'error': '请先停止录音以完成 WAV 文件'}, status=409)
    response = web.FileResponse(path)
    if request.query.get('download') == '1':
        response.headers['Content-Disposition'] = f'attachment; filename="{session}-{name}"'
    return response


async def audio_websocket(request):
    state = console(request)
    if not state.board.get('audio', {}).get('ready'):
        return web.json_response({'error': '当前开发板程序未开启麦克风'}, status=503)
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=1024)
    await ws.prepare(request)
    queue = asyncio.Queue(maxsize=10)
    state.viewers[ws] = queue
    state.ensure_audio()
    await ws.send_json({'sample_rate': 48000, 'bits_per_sample': 16, 'channels': 1,
                        'gain_db': 12, 'source': 'real_microphone'})
    async def send():
        while True:
            packet = await queue.get()
            await asyncio.wait_for(ws.send_bytes(packet), 1)
    sender = asyncio.create_task(send())
    try:
        async for message in ws:
            if message.type in {WSMsgType.ERROR, WSMsgType.CLOSE}:
                break
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        state.viewers.pop(ws, None)
    return ws


async def simulator_start(request):
    state = console(request)
    if state.board.get('mode') == 'camera_only':
        return web.json_response({'error': '当前是摄像头程序，仪器网关暂未启用'}, status=503)
    payload = await request.json()
    if state.fleet and state.fleet.status()['running']:
        return web.json_response(state.fleet.status())
    count = int(payload.get('count', 4))
    state.fleet = SimulatorFleet(state.board_url, count).start()
    state.log_event({'kind': 'simulator_started', 'source': 'simulated', 'count': count})
    return web.json_response(state.fleet.status())


async def simulator_stop(request):
    state = console(request)
    if state.fleet:
        await asyncio.to_thread(state.fleet.stop)
    return web.json_response({'running': False, 'source': 'simulated'})


async def submit_command(request):
    state = console(request)
    payload = await request.json()
    command = {'request_id': payload.get('request_id') or uuid.uuid4().hex,
               'device_id': payload['device_id'], 'method': payload['method'],
               'params': payload.get('params', {}), 'ttl_ms': int(payload.get('ttl_ms', 5000))}
    state.log_event({'kind': 'command_submitted', 'source': 'simulated', **command})
    result = await state.execute_command(command)
    state.log_event({'kind': 'command_finished', **result})
    state.commands.appendleft(result)
    return web.json_response(result)


async def command_status(request):
    result = await console(request).request('GET', '/api/commands/' + urllib.parse.quote(request.match_info['id'], safe=''))
    return web.json_response(result)


async def index(request):
    return web.FileResponse(UI / 'index.html')


async def get_ai_status(request):
    return web.json_response(ai_status())


async def get_camera_status(request):
    return web.json_response(console(request).camera.status())


async def camera_websocket(request):
    return await console(request).camera.websocket(request)


async def save_camera_photo(request):
    return web.json_response(console(request).camera.save_photo(DATA/'camera-photos'))


async def configure_camera(request):
    payload = await request.json()
    return web.json_response(await console(request).camera.set_quality(payload.get('quality')))


async def camera_photo(request):
    name = request.match_info['name']
    if not valid_photo_name(name):
        raise web.HTTPNotFound()
    path = DATA/'camera-photos'/name
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={'Cache-Control': 'no-store'})


async def get_voice_status(request):
    state = console(request)
    return web.json_response({'config': voice_config(), 'session': state.voice.status(),
                              'automatic_interrupt_ready': bool(state.board.get('speech_frontend', {}).get('ready')),
                              'recording_active': bool(state.recording or state.record_pending),
                              'board_ready': state.status()['board_connected'] and bool(state.board.get('speaker', {}).get('ready'))})


async def configure_voice(request):
    state = console(request)
    if state.voice.active:
        return web.json_response({'error': '请先结束通话，再修改配置'}, status=409)
    payload = await request.json()
    return web.json_response(save_voice_config(payload))


async def start_voice(request):
    return web.json_response(await console(request).voice.start())


async def selftest_voice(request):
    return web.json_response(await console(request).voice.start(protocol_test=True))


async def stop_voice(request):
    return web.json_response(await console(request).voice.stop())


async def interrupt_voice(request):
    return web.json_response(await console(request).voice.interrupt())


async def stop_console(request):
    def exit_loop():
        raise web.GracefulExit()
    asyncio.get_running_loop().call_later(.3, exit_loop)
    return web.json_response({'stopping': True})


async def ai_chat(request):
    state = console(request)
    if state.ai_lock.locked():
        return web.json_response({'error': '已有 AI 回答正在生成'}, status=409)
    payload = await request.json()
    text = payload.get('text', '')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        return web.json_response({'error': '请输入 1～2000 个字符'}, status=400)
    if not ai_status().get('configured'):
        return web.json_response({'error': '请先将 DeepSeek 密钥填入 api_key/.env'}, status=503)
    async with state.ai_lock:
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream; charset=utf-8',
                                              'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        await response.prepare(request)
        async def event(data):
            await response.write(('data: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode())
        try:
            async with aclosing(stream_reply(text, payload.get('history'))) as answer:
                async for part in answer:
                    await event({'delta': part})
            await event({'done': True})
        except AIError as error:
            await event({'error': str(error)})
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response


async def lifecycle(app):
    state = app['console']
    state.http = ClientSession(trust_env=False, connector=TCPConnector(limit=16, limit_per_host=7))
    state.poll_task = asyncio.create_task(state.poll())
    yield
    await state.voice.stop('console_stopped')
    await state.camera.reset()
    if state.recording:
        state.finish_recording('interrupted', 'console_stopped')
    if state.fleet:
        await asyncio.to_thread(state.fleet.stop)
    for ws in tuple(state.viewers):
        await ws.close()
    tasks = [task for task in (state.poll_task, state.audio_task) if task]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await state.http.close()


def create_app(board_url):
    app = web.Application(middlewares=[handle_errors], client_max_size=64 * 1024)
    app['console'] = Console(board_url)
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.get('/', index), web.get('/api/status', get_status),
        web.post('/api/config', set_config), web.get('/ws/audio', audio_websocket),
        web.post('/api/recordings/start', start_recording), web.post('/api/recordings/stop', stop_recording),
        web.get('/api/recordings', list_recordings),
        web.get('/api/recordings/{session}/{name}', recording_file),
        web.post('/api/simulators/start', simulator_start), web.post('/api/simulators/stop', simulator_stop),
        web.post('/api/commands', submit_command), web.get('/api/commands/{id}', command_status),
        web.get('/api/ai/status', get_ai_status), web.post('/api/ai/chat', ai_chat),
        web.get('/api/camera/status', get_camera_status), web.get('/ws/camera', camera_websocket),
        web.post('/api/camera/capture', save_camera_photo), web.get('/api/camera/photos/{name}', camera_photo),
        web.post('/api/camera/config', configure_camera),
        web.get('/api/voice/status', get_voice_status), web.post('/api/voice/config', configure_voice),
        web.post('/api/voice/start', start_voice), web.post('/api/voice/self-test', selftest_voice),
        web.post('/api/voice/stop', stop_voice), web.post('/api/voice/interrupt', interrupt_voice),
        web.post('/api/console/stop', stop_console),
    ])
    app.router.add_static('/assets/', UI / 'assets')
    return app


def main():
    config_path = ROOT / 'local' / 'pc.json'
    defaults = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', default=defaults.get('board_url', 'http://192.168.2.38'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    web.run_app(create_app(args.board), host='127.0.0.1', port=args.port, print=print)


if __name__ == '__main__':
    main()
