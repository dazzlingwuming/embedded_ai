"""Direct TLS/authentication checks. No generation, ASR task, or TTS task."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
import sys
import time

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pc_backend.ai_client import settings as ai_settings
from pc_backend.voice_config import load_config


async def main():
    ai = ai_settings()
    speech = load_config()
    report = {'checked_utc': datetime.now(timezone.utc).isoformat(),
        'application_proxy': None, 'trust_env': False,
        'tls_certificate_verification': True, 'cloud_generation_used': False,
        'speech_tasks_started': False, 'audio_sent': False, 'passed': False,
        'configuration': {'deepseek_proxy_empty': ai['proxy'] is None,
                          'speech_proxy_empty': not speech['proxy']}}
    context = ssl.create_default_context()
    timeout = aiohttp.ClientTimeout(total=25, connect=12)
    async with aiohttp.ClientSession(trust_env=False, timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=context)) as client:
        async def deepseek():
            result = {'host': 'api.deepseek.com', 'endpoint': '/models',
                      'key_present': bool(ai['key']), 'passed': False}
            started = time.monotonic()
            try:
                headers = {'Authorization': 'Bearer '+ai['key']} if ai['key'] else {}
                async with client.get('https://api.deepseek.com/models',
                        headers=headers, proxy=None) as response:
                    result['http_status'] = response.status
                    result['network_reachable'] = True
                    if response.status == 200:
                        payload = await response.json()
                        models = [item.get('id') for item in payload.get('data', [])
                                  if isinstance(item, dict)]
                        result.update(passed=True, authentication_verified=True,
                                      project_model_listed=ai['model'] in models)
                    else:
                        await response.read()
            except Exception as error:
                # Vendor errors and request objects can contain supplied values.
                result['error_type'] = type(error).__name__
            result['elapsed_seconds'] = round(time.monotonic()-started, 2)
            return result

        async def aliyun():
            result = {'host': speech['api_host'], 'endpoint': '/api-ws/v1/inference',
                      'key_present': bool(speech['api_key']), 'passed': False}
            started = time.monotonic()
            try:
                url = 'wss://'+speech['api_host']+'/api-ws/v1/inference'
                headers = {'Authorization': 'Bearer '+speech['api_key']}
                async with client.ws_connect(url, headers=headers, proxy=None,
                        heartbeat=None, timeout=aiohttp.ClientWSTimeout(ws_close=1)) as ws:
                    result.update(passed=True, network_reachable=True,
                                  websocket_upgrade_status=101, authentication_verified=True)
                    connection = ws.get_extra_info('ssl_object')
                    if connection:
                        result['tls_version'] = connection.version()
                    # Deliberately send no run-task, text or audio message.
            except Exception as error:
                result['error_type'] = type(error).__name__
                if isinstance(error, aiohttp.WSServerHandshakeError):
                    result['http_status'] = error.status
            result['elapsed_seconds'] = round(time.monotonic()-started, 2)
            return result

        report['deepseek'], report['aliyun_speech'] = await asyncio.gather(deepseek(), aliyun())
    report['passed'] = (all(report['configuration'].values()) and
                        report['deepseek']['passed'] and report['aliyun_speech']['passed'])
    destination = ROOT/'data/direct-network-check.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(main()))
