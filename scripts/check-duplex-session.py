"""Production cloud/control-flow + real speaker diagnostic with synthetic input.

Only GET board:84/speech is replaced. ASR, DeepSeek, TTS and speaker acknowledgments
are real in --run-cloud mode. This does NOT test microphone double-talk or AEC.
"""
import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import ClientSession, ClientTimeout
from pc_backend.ai_client import settings as ai_settings
from pc_backend.duplex_audio import SPEECH_HEADER, unpack_speech
from pc_backend.duplex_voice import DuplexVoiceBridge
from pc_backend.voice_config import load_config
from pc_backend.voice_protocol import PCMConverter, VoiceError

INPUT_ORIGIN = 'synthesized_clean_pcm_with_synthetic_vad'
DIAGNOSTIC_SOURCE = 'real_cloud_duplex_session_with_synthetic_input'
FRAME_BYTES = 640
PROMPT = ('你正在进行语音对话控制流测试。无论用户如何打招呼，请用五个独立中文短句回复，'
          '每句十二到十八个汉字，总计不超过九十个汉字，介绍语音助手可以提供的一般帮助。'
          '不用序号、Markdown或表情，不要声称已观察到设备、音质或现场情况。'
          '收到插话后的新问题时，同样用五个短句回答。')


def fixture_frames(path):
    with wave.open(str(path), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise VoiceError('诊断样本必须是单声道 PCM16 WAV')
        converted = PCMConverter(wav.getframerate(),16000).convert(wav.readframes(wav.getnframes()))
    if not converted:
        raise VoiceError('诊断语音样本为空')
    return [converted[index:index+FRAME_BYTES].ljust(FRAME_BYTES,b'\0')
            for index in range(0,len(converted),FRAME_BYTES)]


def check_preflight(console, board):
    if console.get('recording') or console.get('record_pending'):
        raise VoiceError('用户录音正在运行，诊断拒绝启动')
    if console.get('voice',{}).get('active'):
        raise VoiceError('用户通话正在运行，诊断拒绝启动')
    if not console.get('board_connected'):
        raise VoiceError('控制台尚未连接开发板')
    speaker = board.get('speaker',{})
    if speaker.get('connected') or speaker.get('active') or speaker.get('amplifier_enabled'):
        raise VoiceError('板上喇叭被占用，诊断拒绝启动')
    if not speaker.get('ready'):
        raise VoiceError('板上喇叭尚未就绪')
    frontend = board.get('speech_frontend',{})
    if not frontend.get('ready'):
        raise VoiceError('需要 0.6.0 或更新固件及就绪的语音前端')
    if frontend.get('streaming'):
        raise VoiceError('实板语音前端正在被其他测试使用，诊断拒绝启动')


class SyntheticSpeechInput:
    """A clocked 20 ms input replacement, explicitly carrying synthetic VAD."""
    def __init__(self, frames):
        self.frames = frames
        self.pending = deque()
        self.sequence = 0
        self.speech_ms = 0
        self.closed = False
        self.interceptions = 0
        self.utterances = []
        self.completed_utterances = []
        self.origin = None

    def inject(self, label):
        if self.pending:
            raise VoiceError('上一段合成输入尚未送完')
        self.utterances.append({'label':label,'queued_monotonic':time.monotonic(),
                                'frames':len(self.frames)})
        for index, pcm in enumerate(self.frames):
            self.pending.append((pcm,label,index==len(self.frames)-1))

    async def iter_chunked(self, size):
        self.origin = time.monotonic()
        while not self.closed:
            await asyncio.sleep(max(0,self.origin+self.sequence*.02-time.monotonic()))
            if self.pending:
                pcm,label,last = self.pending.popleft()
                self.speech_ms = min(65000,self.speech_ms+20)
                flags = 5  # Synthetic VAD and an already-clean fixture; no real AEC.
                if last:
                    self.completed_utterances.append(label)
            else:
                pcm = b'\0'*FRAME_BYTES
                self.speech_ms,flags = 0,4
            values = memoryview(pcm).cast('h')
            rms = int(math.sqrt(sum(value*value for value in values)/320))
            stamp = int((self.origin+self.sequence*.02)*1e6)
            packet = SPEECH_HEADER.pack(b'BLV1',self.sequence,stamp,16000,320,
                                       flags,rms,0,rms,self.speech_ms)+pcm
            self.sequence += 1
            yield packet

    def response(self):
        source = self
        class Response:
            status = 200
            content = source
            async def __aenter__(self):
                source.interceptions += 1
                if source.interceptions != 1:
                    raise VoiceError('诊断意外重新打开了合成输入连接')
                return self
            async def __aexit__(self,*args):
                source.closed = True
        return Response()


class SpeechInputRouter:
    def __init__(self, http, board_url, source):
        self.http,self.board_url,self.source = http,board_url,source

    def get(self, url, *args, **kwargs):
        target = urlsplit(str(url))
        board = urlsplit(self.board_url)
        if (target.scheme == 'http' and target.hostname == board.hostname and
                target.port == 84 and target.path == '/speech' and not target.query):
            return self.source.response()
        return self.http.get(url,*args,**kwargs)

    def __getattr__(self,name):
        # Speaker WS, cancellation HTTP, board status and all other I/O stay real.
        return getattr(self.http,name)


async def wait_until(voice, condition, timeout, message):
    deadline = time.monotonic()+timeout
    while not condition():
        if not voice.active or voice.error:
            raise VoiceError(voice.error or '诊断会话提前结束')
        if time.monotonic() >= deadline:
            raise VoiceError(message)
        await asyncio.sleep(.025)


async def get_json(http,url):
    async with http.get(url,timeout=ClientTimeout(total=5),proxy=None) as response:
        if response.status != 200:
            raise VoiceError('诊断状态查询失败，HTTP '+str(response.status))
        return await response.json()


def annotate_session(session):
    return {**session,'input_origin':INPUT_ORIGIN,
        'real_microphone_double_talk_tested':False,
        'acoustic_echo_cancellation_tested':False}


async def run_cloud():
    started = time.monotonic()
    report = {'checked_utc':datetime.now(timezone.utc).isoformat(),'passed':False,
        'input_origin':INPUT_ORIGIN,'real_microphone_double_talk_tested':False,
        'acoustic_echo_cancellation_tested':False,'human_audibility_confirmed':False,
        'application_proxy':None,'cloud_api_used':False,'cloud_api_calls_attempted':False,
        'responses_and_speaker_acknowledgments':'real','stage':'preflight',
        'diagnostic_deadline_seconds':90,'instruction_override_persisted':False}
    voice = source = old_turn = new_turn = None
    scenario_passed = False
    board_url = None
    async with ClientSession(trust_env=False,timeout=ClientTimeout(total=None,connect=8)) as http:
        try:
            # Reserve 20 seconds of the 90-second total for owned connection cleanup.
            async with asyncio.timeout_at(started+70):
                status = await get_json(http,'http://127.0.0.1:8765/api/status')
                board_url = status['board_url']
                board = await get_json(http,board_url+'/api/status')
                check_preflight(status,board)
                report['initial_board'] = {'firmware':board.get('firmware'),
                    'boot_id':board.get('boot_id'),'speaker':board['speaker']}
                config = load_config()
                if config['proxy'] or ai_settings()['proxy']:
                    raise VoiceError('诊断要求项目使用 Direct 直连')
                if not config['api_key'] or not config['api_host']:
                    raise VoiceError('云语音凭据未配置')
                config.update(interaction_mode='automatic_interrupt',instructions=PROMPT,
                              max_session_seconds=90)
                source = SyntheticSpeechInput(fixture_frames(ROOT/'data/cloud-speech-check.wav'))
                console = SimpleNamespace(board=board,board_url=board_url,
                    http=SpeechInputRouter(http,board_url,source),recording=None,record_pending=False,
                    status=lambda:{'board_connected':True},ensure_audio=lambda:None)
                voice = DuplexVoiceBridge(console)
                report['stage'] = 'start_production_session'
                # Only runtime configuration and the board speech input are replaced.
                # ASR/LLM/TTS implementations and all speaker operations stay production.
                with patch('pc_backend.duplex_voice.load_config',side_effect=lambda:config.copy()), \
                     patch('pc_backend.voice_bridge.load_config',side_effect=lambda:config.copy()):
                    report['cloud_api_calls_attempted'] = True
                    await voice.start()
                    voice.source = DIAGNOSTIC_SOURCE
                    await wait_until(voice,lambda:voice.phase=='listening',18,'ASR 没有进入监听状态')
                    report['stage'] = 'first_input_and_reply'
                    source.inject('first_question')
                    await wait_until(voice,lambda:voice.current_turn is not None and
                        voice.phase=='speaking',25,'第一轮没有开始实板播放')
                    old_turn = voice.current_turn
                    # Let one real segment drain so history retention can be tested,
                    # then interrupt a later still-playing segment of the same turn.
                    await wait_until(voice,lambda:bool(old_turn.heard) and voice.phase=='speaking',
                                     15,'没有出现可验证已播短句的插话窗口')
                    if old_turn.done.is_set():
                        raise VoiceError('第一轮过短，未获得自动打断测试窗口')
                    report['stage'] = 'automatic_interrupt'
                    source.inject('interruption_question')
                    await wait_until(voice,lambda:old_turn.interrupted and
                        voice.metrics.get('auto_interruptions',0)>=1,12,'合成插话没有触发自动打断')
                    first_history = [entry.copy() for entry in voice.history]
                    if old_turn.history_entry is None or old_turn.history_entry['content']!=old_turn.heard:
                        raise VoiceError('被打断回答的历史与已播短句不一致')
                    if not old_turn.heard or old_turn.heard==old_turn.generated:
                        raise VoiceError('未验证到完整已播短句与未播完内容的区别')
                    report['interrupted_turn'] = {'question':old_turn.question,
                        'generated':old_turn.generated,'fully_played_text':old_turn.heard,
                        'interrupted':old_turn.interrupted,'history_at_interrupt':first_history,
                        'history_matches_fully_played_text':True}
                    report['stage'] = 'second_reply'
                    await wait_until(voice,lambda:voice.current_turn is not None and
                        voice.current_turn is not old_turn,15,'插话后的最终识别没有启动新回答')
                    new_turn = voice.current_turn
                    await wait_until(voice,lambda:new_turn.done.is_set() and
                        voice.phase=='listening' and not source.pending,35,'第二轮没有正常播放结束')
                    if new_turn.interrupted or not new_turn.heard:
                        raise VoiceError('第二轮回答没有完整播出')
                    if not all((voice.asr_verified,voice.llm_verified,voice.tts_verified)):
                        raise VoiceError('三个真实云端环节未全部通过')
                    if old_turn.history_entry['content']!=old_turn.heard:
                        raise VoiceError('第二轮执行后旧回答历史被未播内容改写')
                    report.update(second_turn={'question':new_turn.question,
                        'fully_played_text':new_turn.heard,'completed':True},
                        final_history=[entry.copy() for entry in voice.history],
                        session=annotate_session(voice.status()),stage='scenario_complete')
                    scenario_passed = True
        except Exception as error:
            report['error'] = str(error) if isinstance(error,VoiceError) else type(error).__name__
            if voice:
                report['session'] = annotate_session(voice.status())
        finally:
            if voice:
                try:
                    await voice.stop('duplex_diagnostic_complete')
                except Exception as error:
                    report['cleanup_error_type'] = type(error).__name__
                    if voice.speaker:
                        try:
                            await asyncio.wait_for(voice.speaker.close(),3)
                        except Exception:
                            report['speaker_cleanup_failed'] = True
                report['final_session_active'] = voice.active
                report['final_stop_reason'] = voice.reason
                report['cloud_api_used'] = bool(voice.asr_verified or voice.tts_verified or
                                                voice.metrics.get('uplink_bytes',0))
                report['session_id'] = voice.session_id
                if voice.session_id:
                    evidence_path = ROOT/'data/voice-checks'/(voice.session_id+'.json')
                    if evidence_path.is_file():
                        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
                        evidence.update(input_origin=INPUT_ORIGIN,
                            real_microphone_double_talk_tested=False,
                            acoustic_echo_cancellation_tested=False)
                        evidence_path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
                        report['production_session_evidence'] = evidence_path.relative_to(ROOT).as_posix()
            if source:
                source.closed = True
                report['synthetic_input'] = {'intercepted_speech_connections':source.interceptions,
                    'frames_sent':source.sequence,'utterances_injected':len(source.utterances),
                    'utterances_fully_sent':source.completed_utterances}
            if voice and board_url:
                try:
                    final_board = await get_json(http,board_url+'/api/status')
                    report['final_speaker'] = final_board['speaker']
                    report['board_boot_unchanged'] = (final_board.get('boot_id')==
                                                     report.get('initial_board',{}).get('boot_id'))
                except Exception as error:
                    report['final_board_error_type'] = type(error).__name__
    speaker = report.get('final_speaker',{})
    report['passed'] = bool(scenario_passed and not report.get('final_session_active',True) and
        speaker.get('ready') and not speaker.get('active') and not speaker.get('connected') and
        not speaker.get('amplifier_enabled') and report.get('board_boot_unchanged') and
        speaker.get('write_errors')==report['initial_board']['speaker'].get('write_errors'))
    report['elapsed_seconds'] = round(time.monotonic()-started,2)
    if report['passed']:
        report['stage']='complete'
    destination = ROOT/'data/duplex-session-check.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report.get(key) for key in ('passed','stage','error','elapsed_seconds',
        'session_id','final_session_active','production_session_evidence')},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


async def check_offline():
    source = SyntheticSpeechInput(fixture_frames(ROOT/'data/cloud-speech-check.wav'))
    source.inject('offline_fixture')
    stream = source.iter_chunked(4096)
    try:
        first = await anext(stream)
        frame = unpack_speech(first)
    finally:
        source.closed = True
        await stream.aclose()
    result = {'passed':True,'cloud_api_used':False,'board_used':False,
              'input_origin':INPUT_ORIGIN,'fixture_frame_count':len(source.frames),
              'packet_bytes':len(first),'frame_sample_count':len(frame.pcm)//2}
    print(json.dumps(result),flush=True)
    return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--offline',action='store_true',help='validate the local fixture only; no network')
    mode.add_argument('--run-cloud',action='store_true',help='run real, potentially billable cloud calls and board playback')
    args=parser.parse_args()
    sys.exit(asyncio.run(check_offline() if args.offline else run_cloud()))
