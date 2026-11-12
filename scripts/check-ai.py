"""One real DeepSeek greeting; never print or persist credentials."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pc_backend.ai_client import AIError, public_status, stream_reply


async def main():
    started = time.monotonic()
    evidence = {'checked_utc': datetime.now(timezone.utc).isoformat(), 'provider': public_status(),
                'test_input': '你好。请只用一句简短中文回复，确认文字对话已连通。', 'passed': False,
                'audio_sent': False, 'device_data_sent': False}
    try:
        pieces = [piece async for piece in stream_reply(evidence['test_input'])]
        evidence.update(passed=True, reply=''.join(pieces))
    except (AIError, ValueError, OSError) as error:
        evidence['error'] = str(error) if isinstance(error, AIError) else '本地配置读取失败'
    evidence['elapsed_seconds'] = round(time.monotonic()-started, 3)
    (ROOT / 'data').mkdir(exist_ok=True)
    (ROOT / 'data' / 'ai-connection-check.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(asyncio.run(main()))
