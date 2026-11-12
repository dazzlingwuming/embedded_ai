"""Real ASR → production DeepSeek/TTS/playback pipeline, with labeled test input."""
import asyncio
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
from pc_backend.cloud_speech import SpeechTask, command
from pc_backend.chained_voice import ChainedVoiceBridge
from pc_backend.speaker_client import BoardSpeaker
from pc_backend.voice_config import load_config
from pc_backend.voice_protocol import PCMConverter, VoiceError
from pc_backend.ai_client import AIError


async def main():
    report = {'checked_utc': datetime.now(timezone.utc).isoformat(), 'passed': False,
        'input_origin': 'previous_cloud_tts_test_fixture', 'microphone_speech_recognition_tested': False,
        'cloud_api_used': True, 'audibility_confirmed_by_person': False, 'stage': 'configuration'}
    playback = session = None
    try:
        config = load_config()
        with wave.open(str(ROOT/'data/cloud-speech-check.wav'), 'rb') as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1,2,24000)
            pcm = PCMConverter(24000,16000).convert(wav.readframes(wav.getnframes()))
        async with ClientSession(trust_env=False, timeout=ClientTimeout(total=None, connect=8)) as http:
            async with http.get('http://127.0.0.1:8765/api/status') as response:
                status = await response.json()
            assert not status['recording'] and not status['voice']['active'], 'End recording and voice calls before testing'
            report['stage'] = 'ASR'
            sentences = []
            async with SpeechTask(http, config, 'ASR') as asr:
                async def receive_asr():
                    async for event in asr.events():
                        sentence = event.get('payload', {}).get('output', {}).get('sentence') or {}
                        if sentence.get('sentence_end') and sentence.get('text') and not sentence.get('heartbeat'):
                            sentences.append(sentence)
                listener = asyncio.create_task(receive_asr())
                try:
                    for offset in range(0,len(pcm),3200):
                        await asr.send_pcm(pcm[offset:offset+3200])
                        await asyncio.sleep(.1)
                    await asr.send(command('finish-task',asr.identity,{'input':{}}))
                    async with asyncio.timeout(15):
                        await listener
                finally:
                    listener.cancel(); await asyncio.gather(listener,return_exceptions=True)
            if not sentences:
                raise VoiceError('ASR 没有识别测试语音')
            report['recognized_test_text'] = ''.join(s['text'] for s in sentences)
            report['asr_recognition_verified'] = True
            report['stage'] = 'DeepSeek_TTS_board_speaker'
            session = ChainedVoiceBridge(SimpleNamespace(http=http, board_url=status['board_url']))
            session.config = config
            session.config['instructions'] = '你是中文语音测试助手。请只用一句不超过十五个汉字的中文回复。'
            session.active, session.phase, session.source = True, 'listening', 'cloud_chain_with_synthesized_test_input'
            session.started = time.monotonic()
            session.metrics = {'interruptions':0,'late_audio_discarded':0,'downlink_bytes':0,'speaker_frames':0}
            session.speaker = BoardSpeaker(http,status['board_url'])
            await session.speaker.connect()
            playback = asyncio.create_task(session.playback())
            await session.accept_sentence(sentences[0])
            text = await asyncio.wait_for(session.turns.get(),2)
            await asyncio.wait_for(session.reply_turn(http,text),60)
            deadline = time.monotonic()+15
            while session.phase!='listening' and time.monotonic()<deadline:
                if playback.done():
                    await playback
                    raise VoiceError('板上播放提前结束')
                await asyncio.sleep(.05)
            if session.phase!='listening':
                raise VoiceError('板上播放没有正常结束')
            report.update(llm_provider='DeepSeek',deepseek_reply=session.history[-1]['content'],
                          tts_verified=session.tts_verified,tts_pcm_bytes=session.metrics['downlink_bytes'],
                          speaker_frames=session.speaker.total_sent,passed=True,stage='complete')
            playback.cancel(); await asyncio.gather(playback,return_exceptions=True); playback=None
            await session.speaker.close()
            async with http.get(status['board_url']+'/api/status') as response:
                board=await response.json()
            report['final_speaker']=board['speaker']
            report['passed'] &= not board['speaker']['amplifier_enabled'] and board['speaker']['write_errors']==0
    except Exception as error:
        report['error']=str(error) if isinstance(error,(VoiceError,AIError)) else type(error).__name__
    finally:
        if playback:
            playback.cancel();await asyncio.gather(playback,return_exceptions=True)
        if session:
            session.active=False
            if session.speaker and not session.speaker.closed:
                await session.speaker.close()
    (ROOT/'data/speech-pipeline-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(main()))
