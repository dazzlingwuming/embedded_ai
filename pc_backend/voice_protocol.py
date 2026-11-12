"""PCM transport only: no local recognition, synthesis, VAD or voice model."""
import base64
import binascii
import struct
import warnings

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop  # Project runtime is CPython 3.12.

MIC_HEADER = struct.Struct('<4sIQIHH')
SPEAKER_HEADER = struct.Struct('<4sIIHH')
FRAME_SAMPLES = 960


class VoiceError(Exception):
    pass


class PlaybackInterrupted(Exception):
    pass


class PCMConverter:
    def __init__(self, input_rate, output_rate):
        self.input_rate, self.output_rate = input_rate, output_rate
        self.state = None
        self.pending = b''

    def convert(self, pcm):
        pcm = self.pending + pcm
        even = len(pcm) & ~1
        self.pending = pcm[even:]
        if not even:
            return b''
        result, self.state = audioop.ratecv(pcm[:even], 2, 1, self.input_rate, self.output_rate, self.state)
        return result


def unpack_microphone(packet):
    if len(packet) < MIC_HEADER.size:
        raise VoiceError('麦克风音频包不完整')
    magic, sequence, stamp, rate, count, flags = MIC_HEADER.unpack_from(packet)
    if magic != b'BLA1' or rate != 48000 or count != 960 or len(packet) != MIC_HEADER.size + count * 2:
        raise VoiceError('麦克风音频格式不匹配')
    return sequence, stamp, packet[MIC_HEADER.size:]


def speaker_frame(epoch, sequence, pcm):
    if not 0 < epoch <= 0xffffffff or not 0 <= sequence <= 0xffffffff or not 0 < len(pcm) <= 1920 or len(pcm) % 2:
        raise VoiceError('扬声器音频包格式不匹配')
    return SPEAKER_HEADER.pack(b'BLP1', epoch, sequence, len(pcm)//2, 0) + pcm


def decode_cloud_audio(event):
    encoded = event.get('delta', '')
    if not isinstance(encoded, str) or len(encoded) > 1024 * 1024:
        raise VoiceError('云端音频数据超出单帧限制')
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise VoiceError('云端音频编码无效') from None
