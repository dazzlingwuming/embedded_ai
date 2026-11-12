"""Check the actual camera HTTP page, JPEG capture, and advancing MJPEG stream."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('url', help='Camera address printed in the serial log')
args = parser.parse_args()
base = args.url.rstrip('/')
root = Path(__file__).resolve().parent.parent
data_dir = root / 'data'
data_dir.mkdir(exist_ok=True)
# Camera traffic is local and must not be sent through the internet proxy.
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def read(path):
    with opener.open(base + path, timeout=12) as response:
        return response.status, dict(response.headers), response.read()

status, headers, page = read('/')
if status != 200 or not page:
    raise RuntimeError('The camera web page is unavailable')
print(f'Web page: HTTP {status}, {len(page)} bytes', flush=True)
_, _, raw_info = read('/api/get_camera_info')
info = json.loads(raw_info)
(data_dir / 'camera-info.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
print('Camera info:', json.dumps(info, ensure_ascii=False), flush=True)
_, _, frame = read('/api/capture_image?source=0')
if not (frame.startswith(b'\xff\xd8') and frame.endswith(b'\xff\xd9')):
    raise RuntimeError('Capture endpoint did not return a complete JPEG')
(data_dir / 'camera-capture.jpg').write_bytes(frame)
print(f'JPEG capture: complete, {len(frame)} bytes', flush=True)

host = urllib.parse.urlsplit(base).hostname
stream_url = f'http://{host}:81/stream'
buffer = bytearray()
frame_hashes = []
started = time.monotonic()
with opener.open(stream_url, timeout=12) as response:
    while len(frame_hashes) < 5 and time.monotonic() - started < 20:
        chunk = response.read(16384)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            first = buffer.find(b'\xff\xd8')
            last = buffer.find(b'\xff\xd9', max(0, first))
            if first < 0 or last < 0:
                break
            image = bytes(buffer[first:last + 2])
            frame_hashes.append(hashlib.sha256(image).hexdigest())
            del buffer[:last + 2]
        if len(buffer) > 8 * 1024 * 1024:
            raise RuntimeError('MJPEG buffer contains no complete frame')
elapsed = time.monotonic() - started
if len(frame_hashes) < 3:
    raise RuntimeError('MJPEG stream did not provide at least three complete frames')
result = {'url': base, 'frames_received': len(frame_hashes),
          'distinct_frames': len(set(frame_hashes)), 'sample_seconds': round(elapsed, 3),
          'note': 'Short functional sample; this is not a sustained FPS benchmark.'}
(data_dir / 'camera-verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print('MJPEG stream:', json.dumps(result), flush=True)
