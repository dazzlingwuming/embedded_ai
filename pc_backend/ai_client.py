"""Cloud text-model adapter; credentials remain on the PC, never in responses."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import re
import time

from aiohttp import ClientSession, ClientTimeout, ClientError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_TOKENS = 8192
MAX_OUTPUT_TOKENS = 393216  # Current official deepseek-flash Chat Completions limit.
_STREAM_EVENTS = ContextVar('deepseek_stream_events', default=None)


class AIError(Exception):
    pass


@contextmanager
def capture_stream_events():
    """Opt-in per-context transport metrics; never stores prompts, deltas or keys."""
    events = []
    token = _STREAM_EVENTS.set(events)
    try:
        yield events
    finally:
        _STREAM_EVENTS.reset(token)


def _record(event, started, **fields):
    events = _STREAM_EVENTS.get()
    if events is not None:
        events.append({'event': event, 'elapsed_ms': round((time.monotonic()-started)*1000, 2), **fields})


def _config_integer(config, name, default, low, high):
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AIError(f'AI 配置 {name} 需要是 {low}～{high} 的整数')
    return value


def parse_key(text):
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^(?:export\s+)?(?:DEEPSEEK_API_KEY|API_KEY|api_key)\s*[:=]\s*(.+)$', line)
        value = match.group(1).strip().strip('\"\'') if match else line
        if re.fullmatch(r'sk-[A-Za-z0-9_-]+', value):
            return value
    return ''


def settings():
    config_path = ROOT / 'local' / 'ai.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    key_path = (ROOT / config.get('api_key_file', 'api_key/.env')).resolve()
    if not key_path.is_relative_to(ROOT.resolve()):
        raise AIError('密钥文件必须位于本项目目录内')
    key = ''
    if key_path.is_file():
        raw = key_path.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('utf-16')
        key = parse_key(text)
    key = key or config.get('api_key', '')
    return {'model': config.get('model', 'deepseek-flash'), 'key': key,
            'proxy': config.get('proxy') or None,
            'max_tokens': _config_integer(config, 'max_tokens', DEFAULT_OUTPUT_TOKENS, 1, MAX_OUTPUT_TOKENS),
            'max_continuations': _config_integer(config, 'max_continuations', 3, 0, 8),
            'read_timeout_seconds': _config_integer(config, 'read_timeout_seconds', 30, 5, 300)}


def public_status():
    try:
        config = settings()
        return {'provider': 'DeepSeek', 'model': config['model'], 'configured': bool(config['key']),
                'speech_mode': 'cloud_asr_deepseek_tts'}
    except (ValueError, OSError, AIError):
        return {'provider': 'DeepSeek', 'configured': False, 'error': '请检查项目内 AI 配置文件格式'}


async def _sse_payloads(content, read_timeout):
    data_lines = []
    while True:
        # Only time the active network read. A consumer may pause between yields
        # for minutes while bounded TTS/speaker queues drain normally.
        async with asyncio.timeout(read_timeout):
            raw = await content.readline()
        if not raw:
            if data_lines:
                yield b'\n'.join(data_lines)
            return
        line = raw.rstrip(b'\r\n')
        if not line:
            if data_lines:
                yield b'\n'.join(data_lines)
                data_lines.clear()
        elif line.startswith(b'data:'):
            data_lines.append(line[5:].lstrip(b' '))


async def stream_reply(text, history=None, system_prompt=None):
    config = settings()
    if not config['key']:
        raise AIError('未读取到 DeepSeek 密钥，请检查 api_key/.env')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        raise AIError('请输入 1～2000 个字符')
    messages = [{'role': 'system', 'content': '你是开发板测试助手。用简洁自然的中文回答。只根据用户实际提供的信息回答，不编造测量或设备连接结果。'}]
    if isinstance(system_prompt, str) and system_prompt.strip():
        messages[0]['content'] = system_prompt
    if isinstance(history, list):
        for item in history[-12:]:
            if isinstance(item, dict) and item.get('role') in ('user', 'assistant') and isinstance(item.get('content'), str):
                messages.append({'role': item['role'], 'content': item['content']})
    messages.append({'role': 'user', 'content': text.strip()})
    payload = {'model': config['model'], 'messages': messages, 'stream': True,
               'thinking': {'type': 'disabled'}, 'max_tokens': config['max_tokens']}
    started = time.monotonic()
    output = []
    continuations = config.get('max_continuations', 3)
    read_timeout = config.get('read_timeout_seconds', 30)
    # Always send this credential to the official DeepSeek host only.
    try:
        timeout = ClientTimeout(total=None, connect=10, sock_read=None)
        async with ClientSession(trust_env=False, timeout=timeout) as client:
            for request_index in range(continuations+1):
                # Official prefix completion returns the continuation only. It
                # preserves the original answer without adding a new user turn.
                request_messages = messages
                url = 'https://api.deepseek.com/chat/completions'
                if request_index:
                    url = 'https://api.deepseek.com/beta/chat/completions'
                    request_messages = [*messages, {'role': 'assistant', 'content': ''.join(output), 'prefix': True}]
                _record('request_started', started, request_index=request_index, model=config['model'],
                        max_tokens=config['max_tokens'], prefix_continuation=bool(request_index))
                async with asyncio.timeout(read_timeout):
                    response = await client.post(url, json={**payload, 'messages': request_messages},
                        headers={'Authorization': 'Bearer ' + config['key']}, proxy=config['proxy'],
                        allow_redirects=False)
                finish_reason, got_text = None, False
                async with response:
                    if response.status != 200:
                        hints = {400: '输入或参数不符合接口要求', 401: '密钥认证失败',
                                 402: '账户额度不足', 429: '请求频率受限'}
                        raise AIError(f"DeepSeek HTTP {response.status}：{hints.get(response.status, '请求未成功')}")
                    async for data in _sse_payloads(response.content, read_timeout):
                        if data == b'[DONE]':
                            break
                        try:
                            event = json.loads(data)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            raise AIError('DeepSeek 返回了无效的流式数据') from None
                        if not isinstance(event, dict) or event.get('error'):
                            raise AIError('DeepSeek 返回了流式错误，回答未完成')
                        choices = event.get('choices') or []
                        if not choices:
                            continue
                        if not isinstance(choices, list) or not isinstance(choices[0], dict):
                            raise AIError('DeepSeek 返回了无效的流式选择数据')
                        choice = choices[0]
                        delta_data = choice.get('delta') or {}
                        if not isinstance(delta_data, dict):
                            raise AIError('DeepSeek 返回了无效的文字增量')
                        delta = delta_data.get('content')
                        finish_reason = choice.get('finish_reason') or finish_reason
                        if finish_reason:
                            _record('request_finished', started, request_index=request_index,
                                    finish_reason=finish_reason, usage=event.get('usage'))
                        if delta:
                            if not isinstance(delta, str):
                                raise AIError('DeepSeek 返回了不支持的文字格式')
                            got_text = True
                            output.append(delta)
                            _record('delta_received', started, request_index=request_index, characters=len(delta))
                            yield delta
                        if finish_reason:
                            break
                if finish_reason == 'stop':
                    if not output:
                        raise AIError('DeepSeek 未返回可显示的回答')
                    return
                if finish_reason == 'length':
                    if not got_text or request_index == continuations:
                        raise AIError('DeepSeek 达到输出或上下文长度上限，回答尚未完整结束；请提高输出预算或缩小范围')
                    continue
                if finish_reason:
                    known = ('content_filter', 'tool_calls', 'insufficient_system_resource', 'aborted')
                    reason = finish_reason if finish_reason in known else 'unknown_finish_reason'
                    raise AIError('DeepSeek 未完整生成回答（'+reason+'）')
                raise AIError('DeepSeek 流式连接在完成标记之前结束，回答可能不完整')
    except (ClientError, asyncio.TimeoutError, OSError) as error:
        raise AIError('DeepSeek 连接失败或读取超时，请检查网络') from error
