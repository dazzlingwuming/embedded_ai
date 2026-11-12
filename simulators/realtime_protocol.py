"""API-free wire fixture. It counts PCM and returns a short tone, never speech/ASR."""
from array import array
import asyncio
import base64
import math

from aiohttp import web, WSMsgType


class ProtocolTestCloud:
    def __init__(self):
        self.runner = None
        self.url = None
        self.input_bytes = 0
        self.cancel_count = 0

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get('/realtime', self.connection)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.url = f'http://127.0.0.1:{port}/realtime'
        return self

    async def __aexit__(self, *args):
        if self.runner:
            await self.runner.cleanup()

    async def connection(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'session.created', 'session': {'id': 'protocol-test'}})
        responded = False
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            event = message.json()
            if event['type'] == 'session.update':
                await ws.send_json({'type': 'session.updated', 'session': event['session']})
            elif event['type'] == 'response.cancel':
                self.cancel_count += 1
            elif event['type'] == 'input_audio_buffer.append':
                self.input_bytes += len(base64.b64decode(event['audio'], validate=True))
                if self.input_bytes >= 6400 and not responded:
                    responded = True
                    rid = 'protocol-tone-1'
                    await ws.send_json({'type': 'conversation.item.input_audio_transcription.completed',
                        'item_id': 'fixture-input', 'transcript': '协议自检：已收到板子音频；没有执行语音识别。'})
                    await ws.send_json({'type': 'response.created', 'response': {'id': rid}})
                    await ws.send_json({'type': 'response.audio_transcript.delta', 'response_id': rid,
                                       'delta': '协议自检提示音，不是 AI 语音。'})
                    values = array('h', (int(8192 * min(n/240, (12000-n)/240, 1) * math.sin(2*math.pi*440*n/24000))
                                         for n in range(12000)))
                    pcm = values.tobytes()
                    # Deliberately odd split sizes exercise byte-boundary handling.
                    cursor, sizes, index = 0, (625, 1777, 2048), 0
                    while cursor < len(pcm):
                        chunk = pcm[cursor:cursor+sizes[index % 3]]
                        cursor += len(chunk); index += 1
                        await ws.send_json({'type': 'response.audio.delta', 'response_id': rid,
                                            'delta': base64.b64encode(chunk).decode()})
                        await asyncio.sleep(.01)
                    await ws.send_json({'type': 'response.audio.done', 'response_id': rid})
                    await ws.send_json({'type': 'response.done', 'response': {'id': rid, 'status': 'completed',
                        'usage': {'source': 'protocol_test', 'input_tokens': 0, 'output_tokens': 0}}})
        return ws
