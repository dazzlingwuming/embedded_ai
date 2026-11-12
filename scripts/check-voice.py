"""Actual board + local protocol fixture acceptance; no paid API or speech model."""
import asyncio
import json
from pathlib import Path
import sys
import time

from aiohttp import ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parent.parent


async def main():
    report = {'test_type': 'real_board_with_local_cloud_protocol_fixture', 'cloud_api_used': False,
              'paid_api_used': False, 'recognition_or_synthesis_tested': False, 'passed': False}
    async with ClientSession(trust_env=False, timeout=ClientTimeout(total=12)) as client:
        async def api(path, payload=None, expected=200):
            async with client.request('GET' if payload is None else 'POST', 'http://127.0.0.1:8765'+path, json=payload) as response:
                value = await response.json()
                assert response.status == expected, (path, response.status, value)
                return value
        try:
            before = await api('/api/status')
            assert before['board_connected'] and before['board']['speaker']['ready']
            report['initial_board'] = before['board']
            config = (await api('/api/voice/status'))['config']
            if not config['configured']:
                denied = await api('/api/voice/start', {}, expected=400)
                assert 'Key' in denied['error']
                report['missing_key_refused_without_call'] = True
            await api('/api/voice/self-test', {})
            await api('/api/recordings/start', {'label': 'must-not-start-during-voice'}, expected=409)
            report['raw_recording_excluded_during_voice'] = True
            deadline = time.monotonic() + 20
            session = None
            while time.monotonic() < deadline:
                session = (await api('/api/voice/status'))['session']
                if not session['active']:
                    break
                await asyncio.sleep(.2)
            report['session'] = session
            assert session and not session['active'] and not session['error'], session
            assert session['stop_reason'] == 'self_test_complete', session
            assert session['source'] == 'protocol_test' and not session['cloud_verified']
            assert session['metrics']['uplink_bytes'] >= 6400
            assert session['metrics']['downlink_bytes'] == 24000
            assert session['metrics']['speaker_frames'] > 0
            await asyncio.sleep(.3)
            after = await api('/api/status')
            report['final_board'] = after['board']
            assert not after['board']['speaker']['amplifier_enabled']
            assert after['board']['speaker']['write_errors'] == 0
            assert after['board']['audio']['read_errors'] == before['board']['audio']['read_errors']
            assert after['board']['audio']['alc_register'] == 0
            report['passed'] = True
        except Exception as error:
            report['error'] = repr(error)
        finally:
            await api('/api/voice/stop', {})
    (ROOT/'data'/'voice-transport-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'], 'error':report.get('error'),
                      'session':report.get('session')},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(main()))
