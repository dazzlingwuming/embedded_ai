"""Shared playback/cancellation mechanics and an API-free wire self-test.

Default turn-taking mode suppresses mic uplink during playback plus an echo tail.
Manual interruption is supported; this module does not claim acoustic full duplex.
"""
import asyncio
import base64
from collections import deque
from contextlib import AsyncExitStack, suppress
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urlencode
import uuid

from aiohttp import ClientSession, ClientTimeout, ClientWSTimeout, WSMsgType, ClientError

from pc_backend.speaker_client import BoardSpeaker
from pc_backend.voice_config import load_config, public_config, session_update
from pc_backend.voice_protocol import PCMConverter, VoiceError, PlaybackInterrupted, decode_cloud_audio, unpack_microphone
from simulators.realtime_protocol import ProtocolTestCloud

ROOT = Path(__file__).resolve().parent.parent


def now():
    return datetime.now(timezone.utc).isoformat()


def cloud_failure(event):
    code = str((event.get('error') or {}).get('code', 'unknown'))
    allowed = ''.join(c for c in code if c.isalnum() or c in '._-')[:80]
    # Raw vendor messages can include supplied values; never echo credentials.
    return VoiceError('云端语音拒绝请求：' + (allowed or 'unknown') + '。请核对语音 Key、API Host、模型权限和额度。')


class VoiceBridge:
    def __init__(self, console):
        self.console = console
        self.active = False
        self.phase = 'idle'
        self.source = 'cloud_realtime'
        self.error = None
        self.reason = None
        self.task = self.cloud = self.speaker = None
        self.messages = deque(maxlen=24)
        self.usage = deque(maxlen=12)
        self.metrics = {}
        self.started = 0
        self.session_id = None
        self.lifecycle_lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.cloud_write_lock = asyncio.Lock()
        self.generation = 0
        self.mic_floor_us = 0
        self.capture_offset = None
        self.current_response = None
        self.response_open = False
        self.ignored_responses = set()
        self.output_started = set()
        self.mic_queue = asyncio.Queue(maxsize=25)
        self.play_queue = asyncio.Queue(maxsize=100)
        self.stop_event = asyncio.Event()
        self.config = None
        self.last_input = 0
        self.mute_until = 0
        self.response_bytes = 0
        self.cloud_verified = False
        self.awaiting_cancel = False
        self.session_limit_pending = False
        self.timing_records = deque(maxlen=8)
        self.timing_starts = {}

    def status(self):
        metrics = self.metrics.copy()
        if self.active and self.speaker:
            metrics['speaker_frames'] = self.speaker.total_sent
        return {'active': self.active, 'phase': self.phase, 'source': self.source,
                'session_id': self.session_id, 'elapsed_seconds': round(time.monotonic()-self.started, 1) if self.active else 0,
                'error': self.error, 'stop_reason': self.reason, 'metrics': metrics,
                'messages': list(self.messages), 'usage': list(self.usage),
                'mode': 'turn_taking_manual_interrupt', 'acoustic_echo_cancellation': False,
                'cloud_verified': self.cloud_verified, 'speaker_queue': self.play_queue.qsize(),
                'session_limit_pending': self.session_limit_pending}

    def add_message(self, role, text, identity=None, partial=False):
        identity = identity or uuid.uuid4().hex
        for item in self.messages:
            if item['id'] == identity:
                item.update(text=text, partial=partial)
                return
        self.messages.append({'id': identity, 'role': role, 'text': text,
                              'partial': partial, 'source': self.source, 'time': now()})

    def begin_response_timing(self, identity):
        existing = next((record for record in self.timing_records if record['turn_id']==identity),None)
        if existing is not None:
            return existing
        record = {'turn_id':identity,'request_started_utc':now(),
            'measurement':'application_receive_and_send_boundaries_include_backpressure',
            'server_generation_end_measured':False}
        for name in ('llm_first_text_received_ms','llm_last_text_received_ms','llm_stream_drained_ms',
                     'tts_preheat_started_ms','tts_preheat_ready_ms','first_segment_ready_ms',
                     'first_tts_audio_received_ms','first_speaker_send_ms','playback_completed_ms',
                     'interrupted_ms'):
            record[name] = None
        self.timing_records.append(record)
        self.timing_starts[identity] = time.monotonic()
        retained = {item['turn_id'] for item in self.timing_records}
        self.timing_starts = {key:value for key,value in self.timing_starts.items() if key in retained}
        self.metrics['current_turn_timing'] = record
        self.metrics['turn_timing_history'] = list(self.timing_records)
        return record

    def mark_response_timing(self, identity, name, replace=False):
        record = next((item for item in self.timing_records if item['turn_id']==identity),None)
        if record is not None and (replace or record.get(name) is None):
            record[name] = round((time.monotonic()-self.timing_starts[identity])*1000,1)

    def session_limit_due(self):
        limit = (self.config or {}).get('max_session_seconds',0)
        return bool(self.source!='protocol_test' and self.started and limit and
                    time.monotonic()-self.started >= limit)

    def response_in_progress(self):
        task = getattr(self,'turn_task',None)
        turns = getattr(self,'turns',None)
        return bool(self.phase in ('thinking','speaking','interrupting') or self.response_open or
                    not self.play_queue.empty() or (turns is not None and not turns.empty()) or
                    (task is not None and not task.done()))

    def finish_session_if_due(self):
        if self.source!='protocol_test' and (self.session_limit_pending or self.session_limit_due()):
            self.session_limit_pending = False
            self.metrics['session_limit_pending'] = False
            self.reason = 'session_time_limit'
            self.stop_event.set()
            return True
        return False

    @staticmethod
    def clear_queue(queue):
        while not queue.empty():
            queue.get_nowait()

    async def start(self, protocol_test=False):
        async with self.lifecycle_lock:
            if self.active:
                raise VoiceError('已有语音会话正在运行')
            state = self.console.status()
            if not state['board_connected'] or not self.console.board.get('speaker', {}).get('ready'):
                raise VoiceError('开发板或扬声器尚未就绪，需要 0.4.0 或更新固件')
            if self.console.recording or self.console.record_pending:
                raise VoiceError('请先停止原始录音，再开始语音通话或声音自检')
            config = load_config()
            if not protocol_test and not (config.get('api_key') and config.get('api_host')):
                raise VoiceError('还未配置云语音 Key 和 API Host；可先运行无需 API 的连接自检')
            self.config = config
            self.active, self.phase = True, 'connecting'
            self.source = 'protocol_test' if protocol_test else 'cloud_realtime'
            self.error = self.reason = None
            self.messages.clear(); self.usage.clear()
            self.metrics = {'mic_frames': 0, 'uplink_bytes': 0, 'mic_drops': 0, 'suppressed_mic_frames': 0,
                            'downlink_bytes': 0, 'speaker_frames': 0, 'interruptions': 0,
                            'late_audio_discarded': 0, 'input_discontinuities': 0}
            self.session_limit_pending = False
            self.timing_records.clear(); self.timing_starts.clear()
            self.started, self.last_input = time.monotonic(), time.monotonic()
            self.session_id = uuid.uuid4().hex
            self.generation += 1
            self.mic_floor_us, self.capture_offset, self.mute_until = 0, None, 0
            self.current_response, self.response_open = None, False
            self.cloud_verified = self.awaiting_cancel = False
            self.ignored_responses.clear(); self.output_started.clear()
            self.clear_queue(self.mic_queue); self.clear_queue(self.play_queue)
            self.stop_event = asyncio.Event()
            self.task = asyncio.create_task(self.run(protocol_test))
            self.console.ensure_audio()
            return self.status()

    def offer_audio(self, packet):
        if not self.active:
            return
        sequence, stamp, pcm = unpack_microphone(packet)
        arrival = time.monotonic()
        offset = arrival - stamp / 1e6
        self.capture_offset = offset if self.capture_offset is None else min(self.capture_offset, offset)
        self.last_input = arrival
        self.metrics['mic_frames'] += 1
        if (self.phase != 'listening' and self.phase != 'thinking') or arrival < self.mute_until:
            self.metrics['suppressed_mic_frames'] += 1
            return
        if stamp < self.mic_floor_us or arrival - (stamp/1e6+self.capture_offset) > 1:
            self.metrics['mic_drops'] += 1
            return
        if self.mic_queue.full():
            self.mic_queue.get_nowait()
            self.metrics['mic_drops'] += 1
        self.mic_queue.put_nowait((sequence, pcm, arrival))

    async def send_cloud(self, event):
        if not self.cloud or self.cloud.closed:
            raise VoiceError('云端语音连接已关闭')
        event = {'event_id': 'evt_' + uuid.uuid4().hex, **event}
        async with self.cloud_write_lock:
            await asyncio.wait_for(self.cloud.send_json(event), 5)

    async def interrupt(self, send_to_cloud=True):
        async with self.control_lock:
            if not self.active:
                return self.status()
            self.metrics['interruptions'] += 1
            self.generation += 1
            if self.current_response:
                self.ignored_responses.add(self.current_response)
            self.clear_queue(self.play_queue); self.clear_queue(self.mic_queue)
            self.mute_until = time.monotonic() + self.config['echo_tail_ms']/1000
            self.phase = 'interrupting'
            try:
                if send_to_cloud and self.response_open:
                    self.awaiting_cancel = True
                    await self.send_cloud({'type': 'response.cancel'})
            except Exception:
                self.error = '云端停止请求未能送达，已关闭本次通话'
                self.reason = 'connection_error'
                self.stop_event.set()
                raise VoiceError(self.error) from None
            finally:
                if self.speaker:
                    await self.speaker.cancel()
                self.current_response = None
                self.response_open = False
                self.phase = 'error' if self.error else 'listening'
            return self.status()

    async def stop(self, reason='user_stop'):
        if self.active:
            self.reason = reason
        self.stop_event.set()
        if self.task and self.task is not asyncio.current_task() and not self.task.done():
            if not self.task.cancelling():
                self.task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self.task), 7)
        return self.status()

    def input_failed(self):
        if not self.active:
            return
        self.error = '开发板收音连接已中断，本次通话已结束；请检查局域网后手动重新开始'
        self.reason = 'connection_error'
        self.stop_event.set()
        if self.task and not self.task.done() and not self.task.cancelling():
            self.task.cancel()

    async def run(self, protocol_test):
        children = []
        try:
            if not protocol_test:
                raise VoiceError('请通过 ASR → DeepSeek → TTS 管线启动语音')
            async with AsyncExitStack() as stack:
                self.speaker = BoardSpeaker(self.console.http, self.console.board_url)
                await self.speaker.connect()
                fixture = await stack.enter_async_context(ProtocolTestCloud())
                url, headers = fixture.url, {}
                client = await stack.enter_async_context(ClientSession(trust_env=False,
                    timeout=ClientTimeout(total=None, connect=8)))
                try:
                    self.cloud = await client.ws_connect(url, headers=headers,
                        proxy=None,
                        heartbeat=15, timeout=ClientWSTimeout(ws_close=1), max_msg_size=2*1024*1024)
                except ClientError as error:
                    status = getattr(error, 'status', None)
                    raise VoiceError('云语音 WebSocket 连接失败' + (f'（HTTP {status}）' if status else '') + '，请核对地址、Key 和网络') from None
                stack.push_async_callback(self.cloud.close)
                created = await self.cloud.receive_json(timeout=12)
                if created.get('type') == 'error':
                    raise cloud_failure(created)
                if created.get('type') != 'session.created':
                    raise VoiceError('云端语音握手格式不匹配')
                await self.send_cloud(session_update(self.config))
                updated = await self.cloud.receive_json(timeout=12)
                if updated.get('type') == 'error':
                    raise cloud_failure(updated)
                if updated.get('type') != 'session.updated':
                    raise VoiceError('云端没有确认语音配置')
                output = updated.get('session', {}).get('audio', {}).get('output', {}).get('format', {})
                if output.get('type') != 'pcm' or output.get('sample_rate') != 24000:
                    raise VoiceError('云端输出必须确认是 24 kHz、16 bit 单声道 PCM')
                self.phase = 'listening'
                self.clear_queue(self.mic_queue)
                children = [asyncio.create_task(self.uplink()), asyncio.create_task(self.downlink()),
                            asyncio.create_task(self.playback()), asyncio.create_task(self.watchdog(protocol_test)),
                            asyncio.create_task(self.stop_event.wait())]
                async def stop_children():
                    for child in children:
                        child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
                stack.push_async_callback(stop_children)
                finished, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    if not task.cancelled() and task.exception():
                        raise task.exception()
                if not self.stop_event.is_set():
                    raise VoiceError('语音连接已结束，请手动重新开始')
                if fixture:
                    self.metrics['fixture_received_pcm_bytes'] = fixture.input_bytes
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.error = str(error) if isinstance(error, VoiceError) else '语音链路中断，请检查网络或配置'
            self.reason = 'connection_error'
        finally:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if self.speaker:
                self.metrics['speaker_frames'] = self.speaker.total_sent
                try:
                    await self.speaker.close()
                except Exception:
                    self.error = self.error or '板上播放连接关闭异常，请检查开发板状态'
            self.cloud = None
            self.active = False
            self.phase = 'error' if self.error else 'idle'
            if not self.reason:
                self.reason = 'stopped'
            directory = ROOT / 'data' / 'voice-checks'
            directory.mkdir(parents=True, exist_ok=True)
            # Only diagnostics and aggregate counts; no raw audio, key or transcript.
            evidence = {'session_id': self.session_id, 'source': self.source, 'finished_utc': now(),
                        'reason': self.reason, 'error': self.error, 'metrics': self.metrics,
                        'cloud_service_exercised': False,
                        'cloud_audio_received': self.cloud_verified,
                        'audibility_confirmed_by_person': False, 'acoustic_echo_cancellation': False}
            (directory / (self.session_id + '.json')).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    async def uplink(self):
        converter = PCMConverter(48000, 16000)
        previous = None
        while True:
            sequence, pcm, arrival = await self.mic_queue.get()
            if self.phase not in ('listening', 'thinking') or time.monotonic() < self.mute_until:
                continue
            if time.monotonic()-arrival > 1:
                self.metrics['mic_drops'] += 1
                continue
            if previous is not None and ((sequence - previous) & 0xffffffff) != 1:
                converter = PCMConverter(48000, 16000)
                self.metrics['input_discontinuities'] += 1
                await self.send_cloud({'type': 'input_audio_buffer.clear'})
            previous = sequence
            converted = converter.convert(pcm)
            await self.send_cloud({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(converted).decode()})
            self.metrics['uplink_bytes'] += len(converted)

    async def downlink(self):
        async for message in self.cloud:
            if message.type != WSMsgType.TEXT:
                if message.type == WSMsgType.ERROR:
                    raise VoiceError('云端语音连接出错')
                continue
            await self.handle_event(message.json())
        if not self.stop_event.is_set():
            raise VoiceError('云端语音连接已断开')

    async def handle_event(self, event):
        kind = event.get('type', '')
        rid = event.get('response_id') or (event.get('response') or {}).get('id')
        if kind == 'error':
            detail = event.get('error') or {}
            message = str(detail.get('message', '')).lower()
            if self.awaiting_cancel and (detail.get('code') in ('response_cancel_not_active', 'no_active_response') or
                any(phrase in message for phrase in ('no active response', 'no in-progress response', 'no response to cancel'))):
                self.awaiting_cancel = False
                return
            raise cloud_failure(event)
        if kind == 'response.created':
            if self.current_response and self.current_response != rid and self.phase == 'speaking':
                await self.interrupt(send_to_cloud=False)
            self.current_response, self.response_open = rid, True
            self.response_bytes = 0
            if rid:
                self.begin_response_timing(rid)
            self.phase = 'thinking'
        elif kind == 'input_audio_buffer.speech_stopped':
            if self.phase == 'listening':
                self.phase = 'thinking'
        elif kind == 'conversation.item.input_audio_transcription.completed':
            self.add_message('user', str(event.get('transcript', '')), event.get('item_id'))
        elif kind in ('response.audio_transcript.delta', 'response.output_audio_transcript.delta', 'response.text.delta'):
            if rid in self.ignored_responses:
                return
            identity = rid or 'assistant-current'
            before = next((m['text'] for m in self.messages if m['id'] == identity), '')
            self.add_message('assistant', before + str(event.get('delta', '')), identity, partial=True)
        elif kind in ('response.audio.delta', 'response.output_audio.delta'):
            if rid in self.ignored_responses:
                self.metrics['late_audio_discarded'] += 1
                return
            pcm = decode_cloud_audio(event)
            if self.source != 'protocol_test':
                self.cloud_verified = True
            self.response_bytes += len(pcm)
            self.mark_response_timing(rid, 'first_tts_audio_received_ms')
            generation = self.generation
            if rid not in self.output_started:
                self.output_started.add(rid)
                self.phase = 'speaking'
                self.clear_queue(self.mic_queue)
                await self.play_queue.put(('begin', generation, rid, b''))
            for offset in range(0, len(pcm), 960):
                if generation != self.generation or rid in self.ignored_responses:
                    self.metrics['late_audio_discarded'] += 1
                    break
                await self.play_queue.put(('audio', generation, rid, pcm[offset:offset+960]))
                self.metrics['downlink_bytes'] += len(pcm[offset:offset+960])
        elif kind == 'response.done':
            response = event.get('response') or {}
            if rid == self.current_response:
                self.response_open = False
            if response.get('usage'):
                self.usage.append(response['usage'])
            for item in self.messages:
                if item['id'] == rid:
                    item['partial'] = False
            if rid in self.ignored_responses:
                return
            if response.get('status') not in (None, 'completed'):
                await self.interrupt(send_to_cloud=False)
                self.add_message('system', '云端停止了本次回答：' + str(response.get('status'))[:30])
            elif rid in self.output_started:
                await self.play_queue.put(('end', self.generation, rid, b''))
            else:
                self.phase = 'listening'

    async def playback(self):
        converter, pending, epoch = None, b'', 0
        while True:
            kind, generation, rid, data = await self.play_queue.get()
            if generation != self.generation:
                continue
            try:
                if kind == 'begin':
                    volume = min(self.config['volume'], 75) if self.source == 'protocol_test' else self.config['volume']
                    epoch = await self.speaker.begin(volume)
                    converter, pending = PCMConverter(24000, 48000), b''
                elif kind == 'audio':
                    pending += converter.convert(data)
                    while len(pending) >= 1920:
                        if generation != self.generation:
                            break
                        await self.speaker.send(pending[:1920], epoch)
                        self.mark_response_timing(rid,'first_speaker_send_ms')
                        pending = pending[1920:]
                elif kind == 'end':
                    if converter.pending:
                        raise VoiceError('云端 PCM 以不完整的 16 bit 采样结束')
                    if pending:
                        await self.speaker.send(pending, epoch)
                        self.mark_response_timing(rid,'first_speaker_send_ms')
                    await self.speaker.finish(epoch)
                    if generation != self.generation:
                        continue
                    self.mute_until = time.monotonic() + self.config['echo_tail_ms']/1000
                    await asyncio.sleep(self.config['echo_tail_ms']/1000)
                    if generation != self.generation:
                        continue
                    if self.capture_offset is not None:
                        self.mic_floor_us = int((time.monotonic()-self.capture_offset)*1e6)
                    self.resume_listening()
                    self.mark_response_timing(rid,'playback_completed_ms')
                    if self.source == 'protocol_test':
                        self.reason = 'self_test_complete'
                        self.stop_event.set()
            except PlaybackInterrupted:
                converter, pending, epoch = None, b'', 0

    async def watchdog(self, protocol_test):
        while True:
            await asyncio.sleep(.25)
            if self.speaker and self.speaker.error:
                raise self.speaker.error
            if time.monotonic()-self.last_input > 6:
                raise VoiceError('超过六秒未收到开发板音频，通话已结束')
            if protocol_test and time.monotonic()-self.started >= 12:
                self.reason = 'self_test_timeout'
                raise VoiceError('连接自检超时')
            if not protocol_test and self.session_limit_due():
                self.session_limit_pending = True
                self.metrics['session_limit_pending'] = True
                if not self.response_in_progress():
                    self.finish_session_if_due()
                    return

    def resume_listening(self):
        self.phase = 'listening'
        self.finish_session_if_due()
