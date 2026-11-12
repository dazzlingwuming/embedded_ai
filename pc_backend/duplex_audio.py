"""Board AEC/VAD wire format and conservative interruption confirmation."""
from dataclasses import dataclass
import asyncio
import struct
import time

from pc_backend.voice_protocol import VoiceError

SPEECH_HEADER = struct.Struct('<4sIQIHHHHHH')
SPEECH_SAMPLES = 320
SPEECH_PACKET_BYTES = SPEECH_HEADER.size + SPEECH_SAMPLES*2


@dataclass(frozen=True)
class SpeechFrame:
    sequence: int
    capture_us: int
    flags: int
    mic_rms: int
    reference_rms: int
    clean_rms: int
    speech_ms: int
    pcm: bytes


def unpack_speech(packet):
    if len(packet) != SPEECH_PACKET_BYTES:
        raise VoiceError('回声消除音频包长度不正确')
    magic, sequence, stamp, rate, samples, flags, mic, ref, clean, speech = SPEECH_HEADER.unpack_from(packet)
    if magic != b'BLV1' or rate != 16000 or samples != SPEECH_SAMPLES or flags & ~15 or not flags & 4:
        raise VoiceError('回声消除音频格式不匹配，请检查固件')
    if flags & 8:
        raise VoiceError('板上语音处理输入出现缺口，请检查负载后重新开始通话')
    return SpeechFrame(sequence, stamp, flags, mic, ref, clean, speech, packet[SPEECH_HEADER.size:])


class InterruptionGate:
    def __init__(self, minimum_ms=200):
        self.minimum_ms = minimum_ms
        self.confirmed_at = self.voice_at = 0.0

    def feed(self, frame, arrival=None):
        arrival = time.monotonic() if arrival is None else arrival
        if frame.flags & 1 and frame.clean_rms >= 8:
            self.voice_at = arrival
            if frame.speech_ms >= self.minimum_ms:
                self.confirmed_at = arrival

    def accepts(self, text, arrival=None):
        arrival = time.monotonic() if arrival is None else arrival
        normalized = ''.join(ch for ch in text if ch.isalnum())
        explicit_stop = normalized in ('停', '等等', '等一下', '停止', '别说了', '打住')
        if explicit_stop:
            return self.voice_at > 0 and arrival-self.voice_at <= 1.2
        return (len(normalized) >= 2 and self.confirmed_at > 0 and
                arrival-self.confirmed_at <= 1.2)


def take_sentence(pending, force=False, first=False):
    """Bound synthesis by short text units that can be committed after playback."""
    maximum = 18 if first else 80
    for index, char in enumerate(pending):
        punctuation = char in '。！？!?；;\n' or (char in '，,、：:' and (first or index >= 29))
        boundary = index >= maximum-1
        # Avoid clipping an English word merely because the first phrase is short.
        if boundary and char.isascii() and char.isalnum() and index < maximum+23:
            following = pending[index+1:index+2]
            if not following or (following.isascii() and following.isalnum()):
                boundary = False
        if punctuation or boundary:
            return pending[:index+1], pending[index+1:]
    return (pending, '') if force else ('', pending)


async def timed_speech_events(events, inactivity_seconds):
    """Limit waiting for the next provider event, excluding consumer backpressure."""
    iterator = events.__aiter__()
    try:
        while True:
            try:
                async with asyncio.timeout(inactivity_seconds):
                    event = await anext(iterator)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise VoiceError(f'云语音服务连续 {inactivity_seconds} 秒未返回数据，请检查网络') from None
            yield event
    finally:
        close = getattr(iterator, 'aclose', None)
        if close:
            await close()
