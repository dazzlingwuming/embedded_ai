import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pc_backend.ai_client import settings
from pc_backend.voice_config import load_config


ROOT = Path(__file__).resolve().parent.parent


class DirectNetworkConfigurationTests(unittest.TestCase):
    def test_deepseek_missing_proxy_stays_direct_with_proxy_environment_present(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache/tmp') as folder:
            with patch('pc_backend.ai_client.ROOT',Path(folder)), \
                 patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:7900',
                                        'HTTPS_PROXY':'http://127.0.0.1:7900'}):
                self.assertIsNone(settings()['proxy'])

    def test_speech_missing_proxy_stays_direct_with_proxy_environment_present(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache/tmp') as folder:
            with patch('pc_backend.voice_config.CONFIG_PATH',Path(folder)/'absent.json'), \
                 patch('pc_backend.voice_config.read_key',return_value=''), \
                 patch.dict(os.environ,{'HTTPS_PROXY':'http://127.0.0.1:7900'}):
                self.assertEqual(load_config()['proxy'],'')

    def test_configuration_templates_default_to_direct(self):
        for name in ('ai.example.json','realtime.example.json'):
            with self.subTest(template=name):
                config=json.loads((ROOT/'config'/name).read_text(encoding='utf-8'))
                self.assertFalse(config['proxy'])


if __name__=='__main__':unittest.main()
