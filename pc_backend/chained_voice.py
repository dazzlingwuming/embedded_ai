"""Cloud ASR → the user's DeepSeek → cloud TTS → the actual board speaker."""
import asyncio
import base64
from collections import deque
from contextlib import AsyncExitStack, aclosing
import json
import time
import uuid

from aiohttp import ClientSession, ClientTimeout
from pc_backend.ai_client import AIError, public_status as ai_status, stream_reply
from pc_backend.cloud_speech import SpeechTask
from pc_backend.duplex_audio import take_sentence, timed_speech_events
from pc_backend.speaker_client import BoardSpeaker
from pc_backend.voice_bridge import VoiceBridge, ROOT, now
from pc_backend.voice_protocol import PCMConverter, VoiceError


class ChainedVoiceBridge(VoiceBridge):
    def __init__(self, console):
        super().__init__(console)
        self.asr = self.turn_task = None
        self.turns = asyncio.Queue(maxsize=1)
        self.history = deque(maxlen=12)
        self.sentences = set()
        self.asr_floor_ms = 0
        self.asr_verified = self.llm_verified = self.tts_verified = False

    def status(self):
        return {**super().status(), 'pipeline': ['cloud_asr', 'DeepSeek', 'cloud_tts'],
                'llm_provider': 'DeepSeek', 'asr_verified': self.asr_verified,
                'llm_verified': self.llm_verified, 'tts_verified': self.tts_verified}

    async def start(self, protocol_test=False):
        if self.active:
            raise VoiceError('已有语音会话正在运行')
        if not protocol_test and not ai_status().get('configured'):
            raise VoiceError('请先配置现有 DeepSeek Key')
        self.asr_verified = self.llm_verified = self.tts_verified = False
        self.asr_floor_ms = 0
        self.history.clear(); self.sentences.clear(); self.clear_queue(self.turns)
        await super().start(protocol_test)
        if not protocol_test:
            self.source = 'cloud_asr_deepseek_tts'
        return self.status()

    def resume_listening(self):
        if self.asr:
            self.asr_floor_ms = self.asr.samples_sent//16
        super().resume_listening()

    async def interrupt(self, send_to_cloud=True):
        if self.source == 'protocol_test':
            return await super().interrupt(send_to_cloud)
        async with self.control_lock:
            if not self.active:
                return self.status()
            self.phase = 'interrupting'
            self.metrics['interruptions'] += 1
            self.generation += 1
            if self.current_response:
                self.ignored_responses.add(self.current_response)
            self.clear_queue(self.play_queue); self.clear_queue(self.mic_queue); self.clear_queue(self.turns)
            try:
                if self.turn_task and self.turn_task is not asyncio.current_task() and not self.turn_task.done():
                    self.turn_task.cancel()
                    await asyncio.gather(self.turn_task, return_exceptions=True)
            finally:
                if self.speaker:
                    await self.speaker.cancel()
                self.current_response, self.response_open = None, False
                self.mute_until = time.monotonic()+self.config['echo_tail_ms']/1000
                self.resume_listening()
            return self.status()

    async def run(self, protocol_test):
        if protocol_test:
            return await super().run(True)
        children = []
        try:
            async with AsyncExitStack() as stack:
                self.speaker = BoardSpeaker(self.console.http, self.console.board_url)
                await self.speaker.connect()
                client = await stack.enter_async_context(ClientSession(trust_env=False,
                    timeout=ClientTimeout(total=None, connect=8)))
                self.asr = await stack.enter_async_context(SpeechTask(client, self.config, 'ASR'))
                self.resume_listening()
                self.clear_queue(self.mic_queue)
                children = [asyncio.create_task(self.asr_uplink()), asyncio.create_task(self.receive_asr()),
                    asyncio.create_task(self.process_turns(client)), asyncio.create_task(self.playback()),
                    asyncio.create_task(self.watchdog(False)), asyncio.create_task(self.stop_event.wait())]
                async def stop_children():
                    for child in children:
                        child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
                stack.push_async_callback(stop_children)
                done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and task.exception():
                        raise task.exception()
                if not self.stop_event.is_set():
                    raise VoiceError('语音服务连接已结束，请手动重新开始')
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.error = str(error) if isinstance(error, (VoiceError, AIError)) else 'ASR → DeepSeek → TTS 链路中断，请检查网络和配置'
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
                    self.error = self.error or '开发板播放连接关闭异常'
            self.asr = None
            self.active = False
            self.phase = 'error' if self.error else 'idle'
            self.reason = self.reason or 'stopped'
            directory = ROOT/'data'/'voice-checks'
            directory.mkdir(exist_ok=True, parents=True)
            evidence = {'session_id': self.session_id, 'source': self.source, 'finished_utc': now(),
                'reason': self.reason, 'error': self.error, 'metrics': self.metrics,
                'pipeline': ['cloud_asr', 'DeepSeek', 'cloud_tts'], 'asr_verified': self.asr_verified,
                'llm_verified': self.llm_verified, 'tts_verified': self.tts_verified,
                'acoustic_echo_cancellation': bool(getattr(self, 'duplex_enabled', False)),
                'audibility_confirmed_by_person': False}
            (directory/(self.session_id+'.json')).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    async def asr_uplink(self):
        converter, previous = PCMConverter(48000, 16000), None
        while True:
            try:
                async with asyncio.timeout(.1):
                    sequence, pcm, arrival = await self.mic_queue.get()
            except TimeoutError:
                # Cloud heartbeat while playing: do not upload microphone echo.
                # Silence is still ASR input and may incur provider charges.
                if self.phase != 'listening' or time.monotonic() < self.mute_until:
                    previous, converter = None, PCMConverter(48000,16000)
                await self.asr.send_pcm(b'\0\0'*1600)
                self.metrics['asr_silence_bytes'] = self.metrics.get('asr_silence_bytes', 0)+3200
                continue
            if self.phase != 'listening' or time.monotonic() < self.mute_until:
                previous, converter = None, PCMConverter(48000,16000)
                await self.asr.send_pcm(b'\0\0'*320)
                self.metrics['asr_silence_bytes'] = self.metrics.get('asr_silence_bytes', 0)+640
                continue
            if time.monotonic()-arrival > 1:
                self.metrics['mic_drops'] += 1
                continue
            if previous is not None:
                missing = (sequence-previous-1) & 0xffffffff
                if missing > 5:
                    raise VoiceError('收音出现超过 100 毫秒的缺口，本次语音已结束，请检查网络')
                if missing:
                    self.metrics['input_discontinuities'] += 1
                    await self.asr.send_pcm(b'\0\0'*(missing*320))
                    converter = PCMConverter(48000, 16000)
            previous = sequence
            converted = converter.convert(pcm)
            await self.asr.send_pcm(converted)
            self.metrics['uplink_bytes'] += len(converted)

    async def receive_asr(self):
        async for event in self.asr.events():
            if isinstance(event, dict) and event.get('header', {}).get('event') == 'result-generated':
                await self.accept_sentence(event.get('payload', {}).get('output', {}).get('sentence') or {})

    async def accept_sentence(self, sentence):
        if sentence.get('heartbeat') or self.phase != 'listening' or time.monotonic() < self.mute_until:
            return
        if self.finish_session_if_due():
            return
        text = sentence.get('text', '')
        if not isinstance(text, str) or not text.strip():
            return
        identity = str(sentence.get('sentence_id', sentence.get('begin_time', 'current')))
        final = sentence.get('sentence_end') is True
        if final:
            end = sentence.get('end_time')
            if not isinstance(end, (int, float)) or end <= self.asr_floor_ms or identity in self.sentences:
                return
            self.sentences.add(identity)
        self.add_message('user', text, 'asr-'+identity, partial=not final)
        if final:
            self.asr_verified = True
            self.phase = 'thinking'
            self.clear_queue(self.mic_queue)
            await self.turns.put(text)

    async def process_turns(self, client):
        while True:
            text = await self.turns.get()
            task = asyncio.create_task(self.reply_turn(client, text))
            self.turn_task = task
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                if self.turn_task is task:
                    self.turn_task = None

    async def reply_turn(self, client, text):
        generation, identity = self.generation, uuid.uuid4().hex
        await self.handle_event({'type': 'response.created', 'response': {'id': identity}})
        full_reply, pending = '', ''
        first_piece = True
        self.mark_response_timing(identity,'tts_preheat_started_ms')
        async with SpeechTask(client, self.config, 'TTS') as tts:
            self.mark_response_timing(identity,'tts_preheat_ready_ms')
            async def receive_audio():
                received = False
                async with aclosing(timed_speech_events(tts.events(),self.config.get('tts_inactivity_seconds',30))) as events:
                    async for event in events:
                        if generation != self.generation:
                            return
                        if isinstance(event, bytes):
                            if not event:
                                continue
                            received = self.tts_verified = True
                            await self.handle_event({'type': 'response.audio.delta', 'response_id': identity,
                                                     'delta': base64.b64encode(event).decode()})
                        elif event.get('header', {}).get('event') == 'task-finished':
                            if not received:
                                raise VoiceError('TTS 任务结束但没有返回声音')
                            await self.handle_event({'type': 'response.done', 'response': {
                                'id': identity, 'status': 'completed', 'usage': event.get('payload', {}).get('usage')}})
                            return

            audio = asyncio.create_task(receive_audio())
            instructions = self.config['instructions']+' 使用适合朗读的纯文本，不使用 Markdown。普通闲聊可简短；用户明确要求全文或逐字朗读时完整提供原文，不自行省略、改写或总结。分段只是播放方式，不限制回答长度。输入是 ASR 转写文字；不要声称已检查音质、摄像头、仪器读数或执行硬件操作。'
            reply = stream_reply(text, list(self.history), system_prompt=instructions)
            try:
                async for delta in reply:
                    if generation != self.generation:
                        return
                    if audio.done():
                        await audio
                        raise VoiceError('TTS 在文字发送完成前提前结束')
                    if not delta:
                        continue
                    self.mark_response_timing(identity,'llm_first_text_received_ms')
                    self.mark_response_timing(identity,'llm_last_text_received_ms',replace=True)
                    full_reply += delta; pending += delta
                    self.llm_verified = True
                    self.add_message('assistant', full_reply, identity, partial=True)
                    while True:
                        sentence,rest = take_sentence(pending,first=first_piece)
                        if rest==pending:
                            break
                        pending=rest
                        if sentence:
                            if any(char.isalnum() for char in sentence):
                                self.mark_response_timing(identity,'first_segment_ready_ms')
                                first_piece=False
                            await tts.send_text(sentence)
                self.mark_response_timing(identity,'llm_stream_drained_ms')
                await tts.send_text(pending)
                await tts.finish_text()
                await audio
                if generation == self.generation:
                    self.history.extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': full_reply}])
            finally:
                await reply.aclose()
                audio.cancel()
                await asyncio.gather(audio, return_exceptions=True)
