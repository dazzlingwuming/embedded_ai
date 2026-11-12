"""Two-turn real cloud session, using labeled synthesized PCM in place of live speech."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import wave

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from aiohttp import ClientSession, ClientTimeout
from pc_backend.chained_voice import ChainedVoiceBridge
from pc_backend.voice_config import load_config
from pc_backend.voice_protocol import MIC_HEADER, PCMConverter, VoiceError


async def main():
    report = {'checked_utc':datetime.now(timezone.utc).isoformat(),'passed':False,
              'input_origin':'synthesized_test_pcm','cloud_api_used':True,
              'live_microphone_recognition_tested':False,'requested_turns':2}
    voice = pump = None
    async with ClientSession(trust_env=False,timeout=ClientTimeout(total=None,connect=8)) as http:
        try:
            async with http.get('http://127.0.0.1:8765/api/status') as response:
                status = await response.json()
            assert not status['recording'] and not status['voice']['active']
            with wave.open(str(ROOT/'data/cloud-speech-check.wav'),'rb') as wav:
                raw = PCMConverter(24000,48000).convert(wav.readframes(wav.getnframes()))
            chunks = [raw[i:i+1920].ljust(1920,b'\0') for i in range(0,len(raw),1920)]
            pending = deque()
            console = SimpleNamespace(board=status['board'],board_url=status['board_url'],http=http,
                                      recording=None,record_pending=False,status=lambda:{'board_connected':True})
            voice = ChainedVoiceBridge(console)
            async def feed():
                sequence = 0
                while voice.active:
                    pcm = pending.popleft() if pending else b'\0'*1920
                    packet = MIC_HEADER.pack(b'BLA1',sequence,int(time.monotonic()*1e6),48000,960,0)+pcm
                    voice.offer_audio(packet)
                    sequence += 1
                    await asyncio.sleep(.02)
            def ensure_audio():
                nonlocal pump
                pump = asyncio.create_task(feed())
            console.ensure_audio = ensure_audio
            await voice.start()
            voice.source = 'real_cloud_session_with_synthesized_input'
            deadline = time.monotonic()+15
            while voice.phase!='listening' and voice.active and time.monotonic()<deadline:
                await asyncio.sleep(.05)
            if voice.phase!='listening':
                raise VoiceError(voice.error or 'ASR 会话未就绪')
            for number in range(1,3):
                pending.extend(chunks)
                deadline = time.monotonic()+35
                while time.monotonic()<deadline:
                    if not voice.active or voice.error:
                        raise VoiceError(voice.error or '语音会话提前结束')
                    if len(voice.history)>=number*2 and voice.phase=='listening' and not pending:
                        break
                    await asyncio.sleep(.05)
                else:
                    raise VoiceError(f'第 {number} 轮未完成')
            report.update(passed=True,completed_turns=len(voice.history)//2,
                          replies=[entry['content'] for entry in voice.history if entry['role']=='assistant'],
                          session=voice.status())
        except Exception as error:
            report['error']=str(error) if isinstance(error,VoiceError) else type(error).__name__
            if voice: report['session']=voice.status()
        finally:
            if voice:
                await voice.stop('diagnostic_complete')
                report['final_session_active'] = voice.active
                report['final_stop_reason'] = voice.reason
            if pump:
                pump.cancel();await asyncio.gather(pump,return_exceptions=True)
    (ROOT/'data/chained-session-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(main()))
