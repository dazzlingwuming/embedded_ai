"""One real MJPEG upstream shared by browser viewers and local vision consumers."""
import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
import re
import math
import time
from urllib.parse import urlsplit
import uuid

from aiohttp import ClientError, ClientTimeout, web

MAX_FRAME = 2 * 1024 * 1024


def frame_timing(times):
    gaps = sorted((right-left)*1000 for left, right in zip(times, times[1:]))
    if not gaps:
        return {'interval_p95_ms': None, 'max_interval_ms': None, 'low_1pct_fps': None, 'interval_samples': 0}
    tail_count = max(1, math.ceil(len(gaps)*.01))
    tail_mean = sum(gaps[-tail_count:])/tail_count
    return {'interval_p95_ms': round(gaps[math.ceil(len(gaps)*.95)-1], 1),
            'max_interval_ms': round(gaps[-1], 1), 'interval_samples': len(gaps),
            'low_1pct_fps': round(1000/tail_mean, 1) if len(gaps)>=100 and tail_mean>0 else None}


class JPEGStream:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b'\xff\xd8')
            if start < 0:
                if len(self.buffer) > MAX_FRAME:
                    raise ValueError('视频数据中没有 JPEG 图像')
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b'\xff\xd9', 2)
            if end < 0:
                if len(self.buffer) > MAX_FRAME:
                    raise ValueError('视频单帧过大或不完整')
                break
            if end + 2 > MAX_FRAME:
                raise ValueError('视频单帧过大')
            frames.append(bytes(self.buffer[:end+2]))
            del self.buffer[:end+2]
        return frames


class CameraRelay:
    def __init__(self, console):
        self.console = console
        self.info = None
        self.info_seen = 0
        self.viewers = set()
        self.consumers = {}
        self.consumer_errors = 0
        self.task = None
        self.latest = None
        self.last_frame = 0
        self.frames = 0
        self.bytes_received = 0
        self.times = deque(maxlen=2400)
        self.started = 0
        self.error = None
        self.lock = asyncio.Lock()

    @property
    def ready(self):
        return bool(self.info and time.monotonic() - self.info_seen < 8)

    async def probe(self):
        async with self.console.http.get(self.console.board_url+'/api/get_camera_info',
                allow_redirects=False, timeout=ClientTimeout(total=4)) as response:
            if response.status != 200:
                raise ValueError('当前开发板程序没有开启摄像头')
            info = await response.json(content_type=None)
        cameras = info.get('cameras') if isinstance(info, dict) else None
        if not isinstance(cameras, list) or not cameras or cameras[0].get('index') != 0:
            raise ValueError('开发板没有报告可用摄像头')
        self.info, self.info_seen = cameras[0], time.monotonic()
        return self.info

    def status(self):
        now = time.monotonic()
        recent = [stamp for stamp in self.times if now-stamp < 5]
        fps = len(recent)/min(5, max(.001, now-self.started)) if self.started else 0
        timing = frame_timing([stamp for stamp in self.times if now-stamp<60])
        return {'ready': self.ready, 'running': bool(self.task and not self.task.done()),
                'connected': bool(self.latest and now-self.last_frame < 4),
                'source': 'real_camera', 'resolution': (self.info or {}).get('currentResolution'),
                'frames': self.frames, 'fps': round(fps, 1), 'viewers': len(self.viewers),
                'consumers': len(self.consumers), 'consumer_errors': self.consumer_errors,
                'quality': (self.info or {}).get('currentQuality'),
                'frame_bytes': len(self.latest) if self.latest else None, 'bytes_received': self.bytes_received,
                'last_frame_age_seconds': round(now-self.last_frame, 2) if self.last_frame else None,
                'error': self.error, **timing}

    def broadcast(self, item):
        for queue in tuple(self.viewers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

    async def acquire(self, name, callback):
        """Keep the shared upstream alive without a browser websocket."""
        if not self.ready:
            raise ValueError('摄像头尚未就绪')
        async with self.lock:
            self.consumers[name] = callback
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self.run())

    async def release(self, name):
        async with self.lock:
            self.consumers.pop(name, None)
            if not self.viewers and not self.consumers:
                await self.stop()

    async def run(self):
        self.error = None
        self.latest, self.last_frame, self.frames = None, 0, 0
        self.bytes_received = 0
        self.times.clear()
        self.started = time.monotonic()
        host = urlsplit(self.console.board_url).hostname
        source = str((self.info or {}).get('src', ':81/stream'))
        match = re.fullmatch(r':([1-9]\d{0,4})/stream', source)
        port = int(match.group(1)) if match else 0
        if not 1 <= port <= 65535:
            self.error = '摄像头报告的视频地址格式不匹配'
            self.broadcast({'error': self.error})
            return
        parser = JPEGStream()
        try:
            async with self.console.http.get(f'http://{host}:{port}/stream', allow_redirects=False,
                    timeout=ClientTimeout(total=None, connect=4, sock_read=8)) as response:
                if response.status != 200 or 'multipart/x-mixed-replace' not in response.headers.get('Content-Type', ''):
                    raise ValueError('摄像头没有返回视频流')
                async for chunk in response.content.iter_chunked(65536):
                    for frame in parser.feed(chunk):
                        self.latest, self.last_frame = frame, time.monotonic()
                        self.frames += 1
                        self.bytes_received += len(frame)
                        self.times.append(self.last_frame)
                        self.broadcast(frame)
                        for callback in tuple(self.consumers.values()):
                            try:
                                # Consumers only enqueue bytes here; JPEG decoding
                                # and scoring must run outside the audio event loop.
                                callback(frame, self.last_frame)
                            except Exception:
                                self.consumer_errors += 1
            if self.viewers or self.consumers:
                raise ValueError('摄像头视频连接已结束，请重新开始预览')
        except asyncio.CancelledError:
            raise
        except (ClientError, ValueError, OSError, asyncio.TimeoutError) as error:
            self.error = str(error) if isinstance(error, ValueError) else '摄像头连接中断，请检查开发板和局域网后重新开始预览'
            self.broadcast({'error': self.error})

    async def stop(self):
        try:
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        finally:
            # A disconnected request can itself be cancelled during cleanup.
            # Clear cached images even when cancellation interrupts that await.
            self.task = None
            self.latest = None
            self.last_frame = 0

    async def reset(self):
        async with self.lock:
            self.broadcast({'error': '开发板连接已切换，请重新开始预览'})
            self.consumers.clear()
            await self.stop()
            self.info, self.info_seen, self.error = None, 0, None

    async def websocket(self, request):
        if not self.ready:
            return web.json_response({'error': '摄像头尚未就绪，请检查开发板连接和当前程序'}, status=503)
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1024)
        await ws.prepare(request)
        queue = asyncio.Queue(maxsize=1)
        async with self.lock:
            self.viewers.add(queue)
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self.run())
        await ws.send_json({'source': 'real_camera', 'resolution': self.status()['resolution']})
        finished = asyncio.Event()

        async def send():
            while not finished.is_set() and not ws.closed:
                async with asyncio.timeout(10):
                    item = await queue.get()
                if isinstance(item, dict):
                    await ws.send_json(item)
                    break
                async with asyncio.timeout(3):
                    await ws.send_bytes(item)

        async def receive():
            await ws.receive()

        sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            finished.set()
            sender.cancel(); receiver.cancel()
            async with self.lock:
                self.viewers.discard(queue)
                if not self.viewers and not self.consumers:
                    await self.stop()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            with suppress(Exception):
                await ws.close()
        return ws

    def save_photo(self, directory: Path):
        if not self.latest or time.monotonic()-self.last_frame > 3:
            raise ValueError('请先开始预览，收到实时画面后再保存照片')
        directory.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:6] + '.jpg'
        (directory/name).write_bytes(self.latest)
        return {'name': name, 'path': str(directory/name), 'source': 'real_camera',
                'url': '/api/camera/photos/'+name, 'bytes': len(self.latest)}

    async def set_quality(self, quality):
        if isinstance(quality, bool) or not isinstance(quality, int) or not 20 <= quality <= 90:
            raise ValueError('预览画质必须是 20～90 的整数')
        if not self.ready:
            raise ValueError('请先连接摄像头')
        try:
            async with self.console.http.post(self.console.board_url+'/api/set_camera_config',
                    json={'index': 0, 'image_format': 0, 'jpeg_quality': quality},
                    allow_redirects=False, timeout=ClientTimeout(total=4)) as response:
                await response.read()
                if response.status != 200:
                    raise ValueError('开发板未接受画质设置')
            await self.probe()
        except (ClientError, OSError, asyncio.TimeoutError):
            raise ValueError('画质设置暂未确认，请检查连接后读取当前值') from None
        return self.status()


def valid_photo_name(name):
    return bool(re.fullmatch(r'\d{8}T\d{6}Z_[a-f0-9]{6}\.jpg', name))
