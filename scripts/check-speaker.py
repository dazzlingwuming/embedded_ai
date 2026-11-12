"""Real-board playback failure checks. Only zero-valued PCM is sent; no API."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from aiohttp import ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pc_backend.voice_protocol import speaker_frame


async def main():
    report = {'test_type': 'real_board_speaker_protocol_failures', 'paid_api_used': False,
              'started_utc': datetime.now(timezone.utc).isoformat(), 'checks': [], 'passed': False}
    async with ClientSession(trust_env=False, timeout=ClientTimeout(total=8)) as client:
        async with client.get('http://127.0.0.1:8765/api/status') as response:
            console = await response.json()
        assert not console['recording'] and not console['voice']['active'], 'Stop recording/voice before running'
        base = console['board_url']
        host = base.split('://', 1)[1].split(':', 1)[0]

        async def status():
            async with client.get(base+'/api/status') as response:
                return await response.json()

        async def receive(ws, kind):
            async with asyncio.timeout(4):
                while True:
                    event = await ws.receive_json()
                    if event['type'] == kind:
                        return event

        async def begin(ws, volume=0):
            await ws.send_json({'type': 'begin', 'volume': volume})
            return (await receive(ws, 'begun'))['epoch']

        async def wait_amplifier():
            for _ in range(20):
                current = await status()
                if current['speaker']['amplifier_enabled']:
                    return current
                await asyncio.sleep(.05)
            raise AssertionError('Silent PCM did not reach enabled amplifier')

        def check(name, condition, **evidence):
            report['checks'].append({'check': name, 'passed': bool(condition), **evidence})
            assert condition, (name, evidence)

        before = await status()
        report['initial_board'] = before
        ws = None
        try:
            ws = await client.ws_connect(f'http://{host}:82/speaker')
            await receive(ws, 'hello')
            epoch = await begin(ws, volume=1)
            frame = speaker_frame(epoch, 0, b'\0\0'*960)
            await ws.send_bytes(frame)
            await ws.send_bytes(frame)
            await receive(ws, 'duplicate')
            current = await wait_amplifier()
            check('duplicate_frame_not_enqueued_twice', current['speaker']['received_frames'] == 1)

            async with client.post(base+'/api/speaker/cancel', json={}) as response:
                assert response.status == 200
            current = await status()
            check('http_cancel_mutes_and_invalidates_queue', not current['speaker']['active'] and
                  not current['speaker']['amplifier_enabled'] and current['speaker']['queued_frames'] == 0)
            new_epoch = await begin(ws)
            await ws.send_bytes(frame)
            await receive(ws, 'stale')
            await ws.send_json({'type': 'end', 'epoch': epoch})
            await receive(ws, 'stale')
            current = await status()
            check('late_cancelled_audio_and_end_cannot_stop_new_turn',
                  current['speaker']['active'] and current['speaker']['epoch'] == new_epoch and
                  current['speaker']['received_frames'] == 0)
            await ws.send_bytes(speaker_frame(new_epoch, 0, b'\0\0'*960))
            await ws.send_json({'type': 'end', 'epoch': new_epoch})
            await receive(ws, 'drained')
            current = await status()
            check('new_turn_drains_once_then_mutes', current['speaker']['consumed_frames'] == 1 and
                  not current['speaker']['active'] and not current['speaker']['amplifier_enabled'])

            epoch = await begin(ws)
            await ws.send_bytes(speaker_frame(epoch, 1, b'\0\0'*960))
            await receive(ws, 'sequence_gap')
            current = await status()
            check('out_of_order_stream_is_cancelled', not current['speaker']['active'] and
                  not current['speaker']['amplifier_enabled'])
            await begin(ws)
            await ws.send_bytes(b'invalid-PCM')
            await receive(ws, 'invalid_frame')
            current = await status()
            check('malformed_audio_is_cancelled', not current['speaker']['active'] and
                  not current['speaker']['amplifier_enabled'])

            epoch = await begin(ws, volume=1)
            await ws.send_bytes(speaker_frame(epoch, 0, b'\0\0'*960))
            await wait_amplifier()
            await ws.close()
            ws = None
            for _ in range(30):
                current = await status()
                if not current['speaker']['connected']:
                    break
                await asyncio.sleep(.1)
            check('websocket_disconnect_cancels_playback', not current['speaker']['connected'] and
                  not current['speaker']['active'] and not current['speaker']['amplifier_enabled'])
            check('microphone_and_i2s_remain_healthy', current['boot_id'] == before['boot_id'] and
                  current['audio']['read_errors'] == before['audio']['read_errors'] and
                  current['speaker']['write_errors'] == before['speaker']['write_errors'])
            report['final_board'] = current
            report['passed'] = True
        except Exception as error:
            report['error'] = repr(error)
        finally:
            if ws:
                await ws.close()
            async with client.post(base+'/api/speaker/cancel', json={}) as response:
                await response.read()
    (ROOT/'data'/'speaker-protocol-verification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'error': report.get('error')},
                     ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(main()))
