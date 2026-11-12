"""Short real ASR handshake + DeepSeek → TTS check. Never print credentials."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from aiohttp import ClientSession, ClientTimeout
from pc_backend.voice_config import load_config
from pc_backend.cloud_speech import SpeechTask, command
from pc_backend.ai_client import stream_reply
from pc_backend.voice_protocol import VoiceError


async def main():
    report = {'checked_utc': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'asr_recognition_tested': False, 'microphone_audio_sent': False,
              'speaker_playback_tested': False, 'stage': 'configuration'}
    config = load_config()
    try:
        if not config['api_key'] or not config['api_host']:
            raise VoiceError('ASR/TTS Key 或 API Host 未配置')
        async with ClientSession(trust_env=False, timeout=ClientTimeout(total=None, connect=8)) as client:
            report['stage'] = 'ASR'
            async with SpeechTask(client, config, 'ASR') as asr:
                report['asr_task_started'] = True
                await asr.send_pcm(b'\0\0'*1600)  # 0.1 seconds of silence, not recognition evidence.
                await asr.send(command('finish-task', asr.identity, {'input': {}}))
                async with asyncio.timeout(12):
                    async for event in asr.events():
                        pass
                report['asr_task_finished'] = True
            report['stage'] = 'DeepSeek'
            reply = ''.join([part async for part in stream_reply('请只回复：你好，现在可以测试语音了。')])
            report['deepseek_reply'] = reply
            report['stage'] = 'TTS'
            audio = bytearray()
            async with SpeechTask(client, config, 'TTS') as tts:
                report['tts_task_started'] = True
                async def receive():
                    async for event in tts.events():
                        if isinstance(event, bytes):
                            audio.extend(event)
                            if len(audio)>24000*2*20:
                                raise VoiceError('语音测试音频超过 20 秒限制')
                receiver = asyncio.create_task(receive())
                try:
                    await tts.send_text(reply)
                    await tts.finish_text()
                    async with asyncio.timeout(30):
                        await receiver
                finally:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
            if not audio or len(audio)%2:
                raise VoiceError('TTS 未返回完整的 PCM 音频')
            output = ROOT/'data'/'cloud-speech-check.wav'
            with wave.open(str(output), 'wb') as wav:
                wav.setparams((1,2,24000,0,'NONE','not compressed')); wav.writeframes(audio)
            report.update(passed=True, tts_pcm_bytes=len(audio), tts_seconds=round(len(audio)/48000,3),
                          tts_sample_rate=24000, stage='complete', wav=str(output))
    except Exception as error:
        report['error'] = str(error) if isinstance(error, VoiceError) else type(error).__name__
    (ROOT/'data'/'cloud-speech-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(main()))
