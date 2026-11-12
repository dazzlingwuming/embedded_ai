"""Bounded speaker flow control, and cancellation independent of the audio socket."""
import asyncio
from contextlib import suppress
from urllib.parse import urlsplit

from aiohttp import ClientTimeout, ClientWSTimeout, WSMsgType
from pc_backend.voice_protocol import VoiceError, PlaybackInterrupted, speaker_frame


class BoardSpeaker:
    def __init__(self, http, board_url):
        self.http, self.board_url = http, board_url
        self.ws = self.reader = self.begin_waiter = None
        self.epoch = self.sent = self.consumed = self.total_sent = 0
        self.condition = asyncio.Condition()
        self.drained = asyncio.Event()
        self.error = None
        self.closed = False
        # A failed/busy handshake does not grant ownership of the shared board
        # speaker. Such a client must never send the global HTTP cancel.
        self.acquired = False

    async def connect(self):
        if self.closed or self.ws is not None:
            raise VoiceError('播放连接已打开或已经关闭，请建立新的连接')
        host = urlsplit(self.board_url).hostname
        self.ws = await self.http.ws_connect(f'http://{host}:82/speaker', heartbeat=2,
                                             timeout=ClientWSTimeout(ws_close=1), max_msg_size=8192)
        hello = await self.ws.receive_json(timeout=4)
        self.acquired = isinstance(hello, dict) and hello.get('type') == 'hello'
        if not self.acquired or hello.get('sample_rate') != 48000:
            raise VoiceError('板上扬声器正忙，或固件版本不匹配')
        self.reader = asyncio.create_task(self.receive())

    async def receive(self):
        try:
            async for message in self.ws:
                if message.type != WSMsgType.TEXT:
                    if message.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue
                event = message.json()
                kind = event.get('type')
                if kind == 'begun':
                    if self.begin_waiter and not self.begin_waiter.done():
                        self.epoch = int(event['epoch'])
                        self.sent = self.consumed = 0
                        self.drained.clear()
                        self.begin_waiter.set_result(self.epoch)
                elif event.get('epoch') == self.epoch and kind in ('consumed', 'drained'):
                    async with self.condition:
                        self.consumed = max(self.consumed, int(event.get('consumed', 0)))
                        self.condition.notify_all()
                    if kind == 'drained':
                        self.drained.set()
                elif kind in ('play_error', 'stream_timeout', 'overflow', 'sequence_gap',
                               'invalid_frame', 'codec_error', 'invalid_volume', 'invalid_control'):
                    raise VoiceError('板上播放流异常：' + kind)
            if not self.closed:
                raise VoiceError('板上播放连接已断开')
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.error = error if isinstance(error, VoiceError) else VoiceError('板上播放协议异常')
            if self.begin_waiter and not self.begin_waiter.done():
                self.begin_waiter.set_exception(self.error)
            async with self.condition:
                self.condition.notify_all()
            self.drained.set()
        finally:
            if self.ws is None or self.ws.closed:
                self.acquired = False

    async def begin(self, volume):
        if self.error or self.closed or not self.acquired:
            raise self.error or VoiceError('播放连接已关闭或尚未取得所有权')
        self.begin_waiter = asyncio.get_running_loop().create_future()
        await self.ws.send_json({'type': 'begin', 'volume': volume})
        return await asyncio.wait_for(self.begin_waiter, 4)

    async def send(self, pcm, epoch):
        async with self.condition:
            await asyncio.wait_for(self.condition.wait_for(lambda:
                self.error or self.closed or self.epoch != epoch or self.sent - self.consumed < 16), 4)
            if self.error or self.closed:
                raise self.error or VoiceError('播放连接已关闭')
            if self.epoch != epoch:
                raise PlaybackInterrupted()
        sequence = self.sent
        self.sent += 1
        await self.ws.send_bytes(speaker_frame(epoch, sequence, pcm))
        self.total_sent += 1

    async def finish(self, epoch):
        if epoch != self.epoch:
            raise PlaybackInterrupted()
        await self.ws.send_json({'type': 'end', 'epoch': epoch})
        await asyncio.wait_for(self.drained.wait(), 5)
        if self.error:
            raise self.error
        if epoch != self.epoch:
            raise PlaybackInterrupted()

    async def cancel(self):
        self.epoch = 0
        if self.begin_waiter and not self.begin_waiter.done():
            self.begin_waiter.set_exception(PlaybackInterrupted())
        self.drained.set()
        async with self.condition:
            self.condition.notify_all()
        if not self.acquired:
            return
        if not self.ws or self.ws.closed:
            self.acquired = False
            return
        if self.ws and not self.ws.closed:
            # Ordered WS cancellation also covers an old begin frame arriving
            # after the independent HTTP cancellation request.
            with suppress(Exception):
                await asyncio.wait_for(self.ws.send_json({'type': 'cancel'}), 1)
        if not self.acquired or self.ws.closed:
            self.acquired = False
            return
        # Out-of-band cancellation invalidates queued audio immediately, even if
        # the WebSocket has buffered old frames awaiting delivery.
        try:
            async with self.http.post(self.board_url + '/api/speaker/cancel', json={},
                                      timeout=ClientTimeout(total=3)) as response:
                await response.read()
                if response.status != 200:
                    raise VoiceError('板上停止播放失败')
        except Exception:
            if self.ws:
                with suppress(Exception):
                    await self.ws.close()
                if self.ws.closed:
                    self.acquired = False

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.cancel()
        finally:
            if self.ws:
                with suppress(Exception):
                    await self.ws.close()
            self.acquired = False
            if self.reader:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
