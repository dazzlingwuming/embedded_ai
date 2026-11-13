"""Continuous board AEC audio, ASR-confirmed barge-in, and heard-only history.

Cloud recognition, DeepSeek and synthesis remain separate services. Text/audio
alignment is conservative at fully drained short TTS segment boundaries.
"""
import asyncio
from dataclasses import dataclass, field
from contextlib import aclosing
import time
from urllib.parse import urlsplit
import uuid

from aiohttp import ClientTimeout

from pc_backend.ai_client import stream_reply
from pc_backend.chained_voice import ChainedVoiceBridge
from pc_backend.cloud_speech import SpeechTask
from pc_backend.duplex_audio import (InterruptionGate, SPEECH_PACKET_BYTES,
    take_sentence, timed_speech_events, unpack_speech)
from pc_backend.voice_config import load_config
from pc_backend.vision_context import prepare_for_voice
from pc_backend.voice_protocol import PCMConverter, PlaybackInterrupted, VoiceError


@dataclass
class SpokenTurn:
    identity: str
    generation: int
    question: str
    generated: str = ''
    heard: str = ''
    trailing_formatting: str = ''
    synthesized_bytes: int = 0
    history_entry: dict | None = None
    interrupted: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Segment:
    turn: SpokenTurn
    text: str


class DuplexVoiceBridge(ChainedVoiceBridge):
    def __init__(self, console):
        super().__init__(console)
        self.duplex_enabled = False
        self.current_turn = None
        self.gate = InterruptionGate()
        self.previous_interrupted = False

    @property
    def uses_separate_audio(self):
        return self.duplex_enabled and self.source != 'protocol_test'

    def status(self):
        status = super().status()
        if self.uses_separate_audio:
            status.update(mode='aec_asr_confirmed_interrupt', acoustic_echo_cancellation=True,
                cloud_verified=bool(self.asr_verified and self.llm_verified and self.tts_verified),
                interruption_detection='board_webrtc_vad_and_cloud_asr',
                history_policy='fully_drained_tts_segments',
                history_alignment='sentence_boundaries_not_word_timestamps')
        return status

    async def start(self, protocol_test=False):
        if self.active:
            raise VoiceError('已有语音会话正在运行')
        config = load_config()
        enabled = not protocol_test and config['interaction_mode'] == 'automatic_interrupt'
        if enabled and not self.console.board.get('speech_frontend', {}).get('ready'):
            raise VoiceError('自动打断需要板上 AEC/VAD（固件 0.6.0）；也可先选择轮流说话模式')
        self.duplex_enabled = enabled
        self.current_turn = None
        self.previous_interrupted = False
        self.gate = InterruptionGate(config['interruption_min_speech_ms'])
        try:
            await super().start(protocol_test)
        except BaseException:
            self.duplex_enabled = False
            raise
        if enabled:
            self.metrics.update(auto_interruptions=0, played_segments=0, synthesized_segments=0,
                speaker_stop_ms=None, mic_rms=0, reference_rms=0, clean_rms=0)
        return self.status()

    def offer_audio(self, packet):
        if not self.uses_separate_audio:
            super().offer_audio(packet)

    def resume_listening(self):
        if not self.uses_separate_audio:
            return super().resume_listening()
        # The ASR stream never stops during playback: do not discard the beginning
        # of an interruption by moving its audio floor or adding an echo mute.
        self.mute_until = 0
        if self.phase == 'connecting':
            # Raw capture is intentionally absent in this mode. Give the speech
            # endpoint its full input grace period after cloud startup completes.
            self.last_input = time.monotonic()
        self.phase = 'listening'
        self.finish_session_if_due()

    async def asr_uplink(self):
        if not self.uses_separate_audio:
            return await super().asr_uplink()
        host = urlsplit(self.console.board_url).hostname
        previous, clock_offset = None, None
        timeout = ClientTimeout(total=None, connect=4, sock_read=6)
        async with self.console.http.get(f'http://{host}:84/speech', timeout=timeout) as response:
            if response.status != 200:
                raise VoiceError('板上回声消除音频未就绪，或被其他测试占用')
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                buffer.extend(chunk)
                while len(buffer) >= SPEECH_PACKET_BYTES:
                    packet = bytes(buffer[:SPEECH_PACKET_BYTES]); del buffer[:SPEECH_PACKET_BYTES]
                    frame = unpack_speech(packet)
                    arrival = time.monotonic()
                    offset = arrival-frame.capture_us/1e6
                    clock_offset = offset if clock_offset is None else min(clock_offset, offset)
                    if offset-clock_offset > 1:
                        raise VoiceError('语音音频积压超过一秒，请检查网络后重新开始')
                    if previous is not None:
                        missing = (frame.sequence-previous-1) & 0xffffffff
                        if missing > 5:
                            raise VoiceError('回声消除音频出现超过 100 毫秒的缺口')
                        if missing:
                            self.metrics['input_discontinuities'] += 1
                            self.metrics['mic_drops'] += missing
                            await self.asr.send_pcm(b'\0\0'*(missing*320))
                    previous = frame.sequence
                    self.last_input = arrival
                    self.metrics['mic_frames'] += 1
                    self.metrics.update(mic_rms=frame.mic_rms, reference_rms=frame.reference_rms,
                                        clean_rms=frame.clean_rms)
                    self.gate.feed(frame, arrival)
                    await self.asr.send_pcm(frame.pcm)
                    self.metrics['uplink_bytes'] += len(frame.pcm)
            raise VoiceError('板上回声消除音频连接已结束')

    async def accept_sentence(self, sentence):
        if not self.uses_separate_audio:
            return await super().accept_sentence(sentence)
        if sentence.get('heartbeat'):
            return
        if self.phase=='listening' and self.finish_session_if_due():
            return
        text = sentence.get('text', '')
        if not isinstance(text, str) or not text.strip():
            return
        identity = str(sentence.get('sentence_id', sentence.get('begin_time', 'current')))
        if identity in self.sentences:
            return
        final = sentence.get('sentence_end') is True
        if final:
            end = sentence.get('end_time')
            if not isinstance(end, (int, float)) or end <= self.asr_floor_ms:
                return
        if self.phase in ('thinking', 'speaking'):
            if not self.gate.accepts(text):
                if final:
                    self.sentences.add(identity)
                    self.metrics['unconfirmed_interruptions'] = self.metrics.get('unconfirmed_interruptions', 0)+1
                return
            await self.interrupt(automatic=True)
        if self.phase != 'listening':
            return
        if self.stop_event.is_set():
            return
        if final:
            self.sentences.add(identity)
        self.add_message('user', text, 'asr-'+identity, partial=not final)
        if final:
            self.asr_verified = True
            self.phase = 'thinking'
            await self.turns.put(text)

    async def interrupt(self, send_to_cloud=True, automatic=False):
        if not self.uses_separate_audio:
            return await super().interrupt(send_to_cloud)
        async with self.control_lock:
            if not self.active:
                return self.status()
            self.phase = 'interrupting'
            self.metrics['interruptions'] += 1
            if automatic:
                self.metrics['auto_interruptions'] = self.metrics.get('auto_interruptions', 0)+1
            self.generation += 1
            if self.current_response:
                self.ignored_responses.add(self.current_response)
            self.clear_queue(self.play_queue); self.clear_queue(self.turns)
            turn = self.current_turn
            if turn and not turn.done.is_set():
                self.mark_response_timing(turn.identity,'interrupted_ms')
                turn.interrupted = self.previous_interrupted = True
                for message in self.messages:
                    if message['id'] == turn.identity:
                        message.update(interrupted=True, partial=False, spoken_text=turn.heard)
            started = time.monotonic()
            try:
                # Stop audible output before waiting for remote LLM/TTS shutdown.
                if self.speaker:
                    await self.speaker.cancel()
                self.metrics['speaker_stop_ms'] = round((time.monotonic()-started)*1000, 1)
            finally:
                if self.turn_task and self.turn_task is not asyncio.current_task() and not self.turn_task.done():
                    self.turn_task.cancel()
                    await asyncio.gather(self.turn_task, return_exceptions=True)
                self.current_response, self.response_open = None, False
                self.resume_listening()
            return self.status()

    def commit_heard_segment(self, segment):
        turn = segment.turn
        if turn.generation != self.generation or turn.interrupted:
            return
        turn.heard += segment.text
        if turn.history_entry is None:
            turn.history_entry = {'role': 'assistant', 'content': turn.heard}
            self.history.append(turn.history_entry)
        else:
            turn.history_entry['content'] = turn.heard
        self.metrics['played_segments'] = self.metrics.get('played_segments', 0)+1
        for message in self.messages:
            if message['id'] == turn.identity:
                message['spoken_text'] = turn.heard

    def commit_completed_formatting(self, turn):
        if (turn.generation != self.generation or turn.interrupted or
                turn.history_entry is None or not turn.trailing_formatting):
            return
        # Preserve final line breaks/punctuation in text history after all audible
        # units drain; a formatting-only fragment is not a separate spoken unit.
        turn.heard += turn.trailing_formatting
        turn.history_entry['content'] = turn.heard
        for message in self.messages:
            if message['id'] == turn.identity:
                message['spoken_text'] = turn.heard

    async def reply_turn(self, client, text):
        if not self.uses_separate_audio:
            return await super().reply_turn(client, text)
        turn = SpokenTurn(uuid.uuid4().hex, self.generation, text)
        self.begin_response_timing(turn.identity)
        self.current_turn = turn
        self.current_response, self.response_open = turn.identity, True
        self.response_bytes = 0
        self.phase = 'thinking'
        self.gate = InterruptionGate(self.config['interruption_min_speech_ms'])
        history = [entry.copy() for entry in self.history]
        self.history.append({'role': 'user', 'content': text})
        instructions = self.config['instructions']+' 使用适合朗读的纯文本和自然标点，不使用 Markdown。普通闲聊可简短；用户明确要求全文或逐字朗读时完整提供原文，不自行省略、改写或总结。分段只是播放方式，不限制回答长度。输入是 ASR 转写文字；不要声称已检查音质或执行未实际执行的设备操作。只有本轮实际附图时才描述摄像头画面；图中可见信息与设备直接测量应区分。'
        if self.previous_interrupted:
            instructions += ' 上一轮回答被用户打断；历史只保留已完整播出的短句，可能有一句只播放了部分。不要默认其他内容已经被听到，优先回答用户最新问题。'
            self.previous_interrupted = False
        segments = asyncio.Queue(maxsize=2)

        async def produce():
            pending = ''
            formatting = ''
            first_piece = True
            prepared = await prepare_for_voice(self, text, turn.identity)
            if turn.generation != self.generation:
                if prepared:
                    prepared.mark('cancelled')
                raise PlaybackInterrupted()
            options = {}
            response_instructions = instructions
            if prepared:
                response_instructions += prepared.instructions
                if prepared.images:
                    options['images'] = prepared.images
                prepared.mark('requesting' if prepared.images else 'no_image')
            reply = stream_reply(text, history, system_prompt=response_instructions, **options)
            completed = False
            try:
                async for delta in reply:
                    if turn.generation != self.generation:
                        raise PlaybackInterrupted()
                    if not delta:
                        continue
                    if prepared:
                        prepared.mark('response_received' if prepared.images else 'no_image')
                    self.mark_response_timing(turn.identity,'llm_first_text_received_ms')
                    self.mark_response_timing(turn.identity,'llm_last_text_received_ms',replace=True)
                    turn.generated += delta; pending += delta
                    self.llm_verified = True
                    self.add_message('assistant', turn.generated, turn.identity, partial=True)
                    while True:
                        sentence, rest = take_sentence(pending,first=first_piece)
                        if rest == pending:
                            break
                        pending = rest
                        if any(char.isalnum() for char in sentence):
                            self.mark_response_timing(turn.identity,'first_segment_ready_ms')
                            await segments.put(Segment(turn, formatting+sentence))
                            formatting = ''
                            first_piece = False
                        else:
                            formatting += sentence
                self.mark_response_timing(turn.identity,'llm_stream_drained_ms')
                sentence, _ = take_sentence(pending, force=True)
                if any(char.isalnum() for char in sentence):
                    self.mark_response_timing(turn.identity,'first_segment_ready_ms')
                    await segments.put(Segment(turn, formatting+sentence))
                    formatting = ''
                else:
                    formatting += sentence
                turn.trailing_formatting = formatting
                await segments.put(None)
                completed = True
                if prepared:
                    prepared.mark('completed' if prepared.images else 'no_image')
            finally:
                await reply.aclose()
                if prepared and not completed:
                    prepared.mark('cancelled' if turn.generation != self.generation or self.stop_event.is_set() else 'error')

        async def synthesize_segment(tts, segment):
            received, finished = False, False
            await tts.send_text(segment.text)
            await tts.finish_text()
            events = timed_speech_events(tts.events(),self.config.get('tts_inactivity_seconds',30))
            async with aclosing(events) as incoming:
                async for event in incoming:
                    if turn.generation != self.generation:
                        raise PlaybackInterrupted()
                    if isinstance(event, bytes):
                        if not event:
                            continue
                        self.mark_response_timing(turn.identity,'first_tts_audio_received_ms')
                        turn.synthesized_bytes += len(event)
                        self.response_bytes += len(event)
                        if not received:
                            await self.play_queue.put(('segment_begin', turn.generation, segment, b''))
                        received = self.tts_verified = self.cloud_verified = True
                        # Bound queued audio by bytes, independently of vendor WS chunk size.
                        for offset in range(0,len(event),960):
                            if turn.generation != self.generation:
                                raise PlaybackInterrupted()
                            chunk = event[offset:offset+960]
                            await self.play_queue.put(('segment_audio', turn.generation, segment, chunk))
                            self.metrics['downlink_bytes'] += len(chunk)
                    elif event.get('header', {}).get('event') == 'task-finished':
                        finished = True
                        if event.get('payload', {}).get('usage'):
                            self.usage.append(event['payload']['usage'])
                        break
            if not received or not finished:
                raise VoiceError('TTS 未完整返回这一段声音')
            self.metrics['synthesized_segments'] = self.metrics.get('synthesized_segments', 0)+1
            await self.play_queue.put(('segment_end', turn.generation, segment, b''))

        async def synthesize():
            # Start the first provider task concurrently with LLM streaming, before
            # waiting for the first short phrase. Later tasks remain demand-driven.
            self.mark_response_timing(turn.identity,'tts_preheat_started_ms')
            async with SpeechTask(client, self.config, 'TTS') as tts:
                self.mark_response_timing(turn.identity,'tts_preheat_ready_ms')
                segment = await segments.get()
                if segment is None:
                    return
                await synthesize_segment(tts,segment)
            while True:
                segment = await segments.get()
                if segment is None:
                    return
                async with SpeechTask(client,self.config,'TTS') as tts:
                    await synthesize_segment(tts,segment)

        tasks = [asyncio.create_task(produce()), asyncio.create_task(synthesize())]
        try:
            pending_tasks = set(tasks)
            while pending_tasks:
                done, pending_tasks = await asyncio.wait(pending_tasks,
                    return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        if turn.generation != self.generation:
                            raise PlaybackInterrupted()
                        raise VoiceError('语音生成任务意外取消，请重新开始通话')
                    if task.exception():
                        raise task.exception()
            if not turn.synthesized_bytes:
                raise VoiceError('本次回答没有可播放的语音内容')
            await self.play_queue.put(('turn_done', turn.generation, turn, b''))
            await turn.done.wait()
        except PlaybackInterrupted:
            if turn.generation == self.generation:
                raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def playback(self):
        if not self.uses_separate_audio:
            return await super().playback()
        converter, pending, epoch = None, b'', 0
        while True:
            kind, generation, item, data = await self.play_queue.get()
            if generation != self.generation:
                continue
            try:
                if kind == 'segment_begin':
                    epoch = await self.speaker.begin(self.config['volume'])
                    if generation != self.generation:
                        continue
                    converter, pending = PCMConverter(24000, 48000), b''
                    self.phase = 'speaking'
                elif kind == 'segment_audio':
                    pending += converter.convert(data)
                    while len(pending) >= 1920:
                        if generation != self.generation:
                            break
                        await self.speaker.send(pending[:1920], epoch)
                        self.mark_response_timing(item.turn.identity,'first_speaker_send_ms')
                        pending = pending[1920:]
                elif kind == 'segment_end':
                    if converter.pending:
                        raise VoiceError('TTS 返回了不完整的 PCM 采样')
                    if pending:
                        await self.speaker.send(pending, epoch)
                        self.mark_response_timing(item.turn.identity,'first_speaker_send_ms')
                    await self.speaker.finish(epoch)
                    # Board drained includes DMA tail and PA mute. Partial
                    # segments are intentionally excluded from model history.
                    if generation == self.generation:
                        self.commit_heard_segment(item)
                        self.phase = 'thinking'
                    converter, pending = None, b''
                elif kind == 'turn_done':
                    self.commit_completed_formatting(item)
                    for message in self.messages:
                        if message['id'] == item.identity:
                            message.update(partial=False, delivered=True, spoken_text=item.heard)
                    self.current_response, self.response_open = None, False
                    self.mark_response_timing(item.identity,'playback_completed_ms')
                    self.resume_listening()
                    item.done.set()
            except PlaybackInterrupted:
                converter, pending, epoch = None, b'', 0
