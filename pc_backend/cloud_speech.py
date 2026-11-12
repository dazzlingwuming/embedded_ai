"""Official DashScope duplex protocols. No speech LLM, local ASR or local TTS."""
import asyncio
from contextlib import suppress
import uuid

from aiohttp import ClientError, ClientWSTimeout, WSMsgType
from pc_backend.voice_protocol import VoiceError


def command(action, identity, payload):
    return {'header': {'action': action, 'task_id': identity, 'streaming': 'duplex'}, 'payload': payload}


def task_error(event, service):
    raw = str(event.get('header', {}).get('error_code', 'unknown'))
    code = ''.join(ch for ch in raw if ch.isalnum() or ch in '._-')[:80]
    return VoiceError(f'{service} 服务拒绝请求：{code}。请检查模型权限、地域、Key 和额度。')


def asr_request(config, identity):
    return command('run-task', identity, {'task_group': 'audio', 'task': 'asr',
        'function': 'recognition', 'model': config['asr_model'], 'input': {},
        'parameters': {'format': 'pcm', 'sample_rate': 16000,
                       'max_sentence_silence': config['silence_duration_ms'], 'heartbeat': True}})


def tts_request(config, identity):
    return command('run-task', identity, {'task_group': 'audio', 'task': 'tts',
        'function': 'SpeechSynthesizer', 'model': config['tts_model'], 'input': {},
        'parameters': {'text_type': 'PlainText', 'voice': config['voice'],
                       'format': 'pcm', 'sample_rate': 24000, 'enable_ssml': False}})


class SpeechTask:
    def __init__(self, client, config, service):
        self.client, self.config, self.service = client, config, service
        self.identity = str(uuid.uuid4())
        self.ws = None
        self.samples_sent = 0

    async def __aenter__(self):
        url = 'wss://'+self.config['api_host']+'/api-ws/v1/inference'
        try:
            self.ws = await self.client.ws_connect(url,
                headers={'Authorization': 'Bearer '+self.config['api_key']},
                proxy=self.config['proxy'] or None, heartbeat=10,
                timeout=ClientWSTimeout(ws_close=1), max_msg_size=2*1024*1024)
            request = asr_request(self.config, self.identity) if self.service == 'ASR' else tts_request(self.config, self.identity)
            await self.send(request)
            event = await self.ws.receive_json(timeout=12)
            self.check(event)
            if event.get('header', {}).get('event') != 'task-started':
                raise VoiceError(self.service+' 服务没有确认任务开始')
            return self
        except BaseException as error:
            await self.close()
            if isinstance(error, (ClientError, OSError, asyncio.TimeoutError)):
                status = getattr(error, 'status', None)
                raise VoiceError(self.service+' 连接失败'+(f'（HTTP {status}）' if status else '')+'，请检查地址、Key 和网络') from None
            raise

    async def __aexit__(self, *args):
        await self.close()

    def check(self, event):
        header = event.get('header', {})
        if header.get('task_id') != self.identity:
            raise VoiceError(self.service+' 返回了不匹配的任务编号')
        if header.get('event') == 'task-failed':
            raise task_error(event, self.service)

    async def send(self, event):
        async with asyncio.timeout(5):
            await self.ws.send_json(event)

    async def send_pcm(self, pcm):
        async with asyncio.timeout(5):
            await self.ws.send_bytes(pcm)
        self.samples_sent += len(pcm)//2

    async def send_text(self, text):
        if text:
            await self.send(command('continue-task', self.identity, {'input': {'text': text}}))

    async def finish_text(self):
        await self.send(command('finish-task', self.identity, {'input': {}}))

    async def events(self):
        async for message in self.ws:
            if message.type == WSMsgType.BINARY:
                if self.service != 'TTS':
                    raise VoiceError('ASR 服务返回了意外的二进制数据')
                yield message.data
            elif message.type == WSMsgType.TEXT:
                event = message.json(); self.check(event)
                yield event
                if event.get('header', {}).get('event') == 'task-finished':
                    return
            elif message.type == WSMsgType.ERROR:
                raise VoiceError(self.service+' 服务连接出错')
        raise VoiceError(self.service+' 服务连接已断开')

    async def close(self):
        if self.ws and not self.ws.closed:
            with suppress(Exception):
                await self.ws.close()
