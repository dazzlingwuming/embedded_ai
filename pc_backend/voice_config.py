"""Cloud ASR → DeepSeek → cloud TTS; separate speech key, project-local only."""
import json
from pathlib import Path
import re

from pc_backend.voice_protocol import VoiceError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / 'local' / 'realtime.json'
KEY_PATH = ROOT / 'api_key' / 'realtime.env'
SHARED_KEY_PATH = ROOT / 'api_key' / '.env'
DEFAULTS = {'provider': 'asr_deepseek_tts', 'api_host': '', 'asr_model': 'fun-asr-realtime',
            'tts_model': 'cosyvoice-v3-flash', 'voice': 'longanyang', 'proxy': '', 'volume': 75,
            'max_session_seconds': 600, 'silence_duration_ms': 800,
            'instructions': '你是友好的中文语音助手。普通闲聊可简短；用户明确要求全文、逐字朗读或详细内容时，完整完成，不自行省略或改成摘要。',
            'echo_tail_ms': 500, 'tts_inactivity_seconds': 30}
DEFAULTS.update(interaction_mode='automatic_interrupt', interruption_min_speech_ms=200)


def validate_host(host):
    host = host.strip().lower()
    # The cloud credential is never sent to an arbitrary user-supplied host.
    if host and not (re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.maas\.aliyuncs\.com', host)
                     or host in ('dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com')):
        raise VoiceError('API Host 请填写阿里控制台提供的主机名，不含 https:// 或路径')
    return host


def read_key():
    for path in (KEY_PATH, SHARED_KEY_PATH):
        if not path.is_file():
            continue
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            match = re.match(r'^\s*(?:export\s+)?(?:DASHSCOPE_API_KEY|QWEN_API_KEY|ALIYUN_API_KEY|ALIYUN_KEY|阿里云[-_]?Key)\s*[:=]\s*[\"\']?(sk-[A-Za-z0-9_.-]+)[\"\']?\s*$', line, re.IGNORECASE)
            if match:
                return match.group(1)
    return ''


def load_config():
    config = DEFAULTS.copy()
    if CONFIG_PATH.is_file():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
            if not isinstance(saved, dict):
                raise ValueError()
        except (OSError, ValueError):
            raise VoiceError('实时语音配置文件格式不正确') from None
        if saved.get('provider') == 'qwen':
            saved.pop('provider'); saved.pop('model', None); saved.pop('voice', None)
        config.update({name: saved[name] for name in DEFAULTS if name in saved})
    config = validate_config(config)
    config['api_key'] = read_key()
    return config


def public_config():
    try:
        config = load_config()
    except (OSError, ValueError, VoiceError):
        return {**DEFAULTS, 'configured': False, 'key_present': False, 'error': '请检查实时语音配置格式'}
    key = config.pop('api_key')
    config.update(key_present=bool(key), configured=bool(key and config['api_host']))
    return config


def validate_config(config):
    for name in ('api_host', 'asr_model', 'tts_model', 'voice', 'proxy', 'instructions'):
        if not isinstance(config[name], str):
            raise VoiceError('配置字段必须是文字')
        config[name] = config[name].strip()
    if config['provider'] != 'asr_deepseek_tts':
        raise VoiceError('语音管线固定为云 ASR → DeepSeek → 云 TTS')
    if config['interaction_mode'] not in ('automatic_interrupt', 'turn_taking'):
        raise VoiceError('对话模式需要选择自动打断或轮流说话')
    config['api_host'] = validate_host(config['api_host'])
    if not re.fullmatch(r'fun-asr-[a-z0-9-]+', config['asr_model']):
        raise VoiceError('ASR 适配器需要 Fun-ASR 实时识别模型')
    if not re.fullmatch(r'cosyvoice-[a-z0-9.\-]+', config['tts_model']):
        raise VoiceError('TTS 适配器需要 CosyVoice 语音合成模型')
    if not 1 <= len(config['voice']) <= 50 or not 1 <= len(config['instructions']) <= 2000:
        raise VoiceError('音色或提示词长度不正确')
    if config['proxy'] and not re.fullmatch(r'https?://(?:127\.0\.0\.1|localhost):\d{1,5}', config['proxy']):
        raise VoiceError('代理可留空，或填写本机 HTTP 代理地址')
    for name, low, high in (('volume', 0, 100), ('max_session_seconds', 0, 3600),
                            ('silence_duration_ms', 200, 6000), ('echo_tail_ms', 100, 2000),
                            ('interruption_min_speech_ms', 100, 1000), ('tts_inactivity_seconds', 5, 300)):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise VoiceError(f'{name} 超出允许范围')
        config[name] = value
    if 0 < config['max_session_seconds'] < 30:
        raise VoiceError('通话上限应为 0（不限时）或至少 30 秒')
    return config


def save_config(payload):
    # A complete UI save can repair a malformed local file.
    try:
        config = load_config()
    except VoiceError:
        config = DEFAULTS.copy()
    config.pop('api_key', None)
    config.update({name: payload[name] for name in DEFAULTS if name in payload})
    config = validate_config(config)
    key = payload.get('api_key', '')
    if key and (not isinstance(key, str) or not re.fullmatch(r'sk-[A-Za-z0-9_.-]{10,512}', key.strip())):
        raise VoiceError('语音 Key 格式不正确')
    CONFIG_PATH.parent.mkdir(exist_ok=True)
    temporary = CONFIG_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(CONFIG_PATH)
    if key:
        KEY_PATH.parent.mkdir(exist_ok=True)
        temporary_key = KEY_PATH.with_suffix('.tmp')
        temporary_key.write_text('DASHSCOPE_API_KEY=' + key.strip() + '\n', encoding='utf-8')
        temporary_key.replace(KEY_PATH)
    return public_config()


def session_update(config):
    # Only the API-free wire fixture uses this synthetic event format.
    return {'type': 'session.update', 'session': {
        'modalities': ['text', 'audio'], 'instructions': config['instructions'],
        'audio': {'input': {'format': {'type': 'pcm', 'sample_rate': 16000}},
                  'output': {'format': {'type': 'pcm', 'sample_rate': 24000}, 'voice': 'protocol-test-tone'}},
        'input_audio_transcription': {'model': 'qwen3-asr-flash-realtime'},
        'turn_detection': {'type': 'server_vad', 'threshold': .5,
                           'silence_duration_ms': config['silence_duration_ms']},
        'enable_search': False}}
