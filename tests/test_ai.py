import unittest
from unittest.mock import patch

from pc_backend.ai_client import parse_key, public_status


class CredentialTests(unittest.TestCase):
    def test_user_colon_format_and_dotenv_are_supported(self):
        fixture = 'sk-test-fixture-not-a-real-secret'
        for text in (f'api_key:{fixture}', f'DEEPSEEK_API_KEY="{fixture}"',
                     f'# example\nexport API_KEY={fixture}', fixture):
            self.assertEqual(parse_key(text), fixture)

    def test_unrecognized_configuration_does_not_become_a_key(self):
        self.assertEqual(parse_key('example instructions\napi_key=\n# sk-comment'), '')

    def test_public_status_never_contains_credential(self):
        fixture = 'sk-test-fixture-not-a-real-secret'
        with patch('pc_backend.ai_client.settings', return_value={'key': fixture, 'model': 'deepseek-flash'}):
            status = public_status()
        self.assertTrue(status['configured'])
        self.assertNotIn(fixture, repr(status))
        self.assertNotIn('key', status)


if __name__ == '__main__':
    unittest.main()
