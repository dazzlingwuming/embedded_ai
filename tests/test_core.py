import json
from pathlib import Path
import tempfile
import unittest
import wave
from array import array
from unittest.mock import AsyncMock, patch

from pc_backend.server import Console, Recording, valid_board_url
from simulators.instrument import SimulatedInstrument, SimulatorFleet


class InstrumentTests(unittest.TestCase):
    def setUp(self):
        self.device = SimulatedInstrument('sim-01')

    def command(self, request_id, method, **params):
        return self.device.handle({'request_id': request_id, 'method': method, 'params': params})

    def test_duplicate_write_does_not_apply_twice(self):
        first = self.command('same-request', 'set_parameter', name='amplitude', value=2)
        second = self.command('same-request', 'set_parameter', name='amplitude', value=2)
        self.assertEqual(first, second)
        self.assertEqual(self.device.parameter_writes, 1)

    def test_invalid_value_is_rejected_without_mutation(self):
        for value in (-1, 6, float('nan'), True):
            result = self.command(str(value), 'set_parameter', name='amplitude', value=value)
            self.assertEqual(result[0], 'rejected')
        self.assertEqual(self.device.parameters['amplitude'], 1)
        self.assertEqual(self.device.parameter_writes, 0)

    def test_lost_ack_can_mean_applied(self):
        self.command('fault', 'set_fault', mode='lost_ack')
        self.assertIsNone(self.command('write', 'set_parameter', name='amplitude', value=2))
        self.assertEqual(self.device.parameters['amplitude'], 2)
        self.assertEqual(self.device.parameter_writes, 1)
        self.assertIsNone(self.command('write', 'set_parameter', name='amplitude', value=2))
        self.assertEqual(self.device.parameter_writes, 1)

    def test_expired_command_is_not_executed(self):
        result = self.device.handle({'request_id': 'expired', 'method': 'set_parameter',
                                    'remaining_ms': 0, 'params': {'name': 'amplitude', 'value': 2}})
        self.assertEqual(result[0], 'rejected')
        self.assertEqual(self.device.parameter_writes, 0)

    def test_waveform_is_explicitly_simulated_and_responds_to_parameter(self):
        before = self.command('wave1', 'read_waveform')[1]
        self.command('change', 'set_parameter', name='amplitude', value=2)
        after = self.command('wave2', 'read_waveform')[1]
        self.assertEqual(before['source'], 'simulated')
        self.assertAlmostEqual(max(after['samples']), 2 * max(before['samples']), places=4)

    def test_http_retry_preserves_instrument_session(self):
        fleet = SimulatorFleet('http://192.168.2.38', 1)
        registrations, polls = [], []

        def request(method, path, payload=None):
            if path == '/api/sim/register':
                registrations.append(payload['session_id'])
                return {'registered': True}
            polls.append(path)
            if len(polls) == 1:
                raise TimeoutError('temporary network interruption')
            fleet.stop_event.set()
            return {'command': None}

        fleet.request = request
        with patch.object(fleet.stop_event, 'wait', return_value=False):
            fleet.worker(fleet.instruments[0])
        self.assertEqual(len(registrations), 2)
        self.assertEqual(registrations[0], registrations[1])

    def test_result_retry_does_not_execute_the_write_again(self):
        fleet = SimulatorFleet('http://192.168.2.38', 1)
        polls, acknowledgements = [], []

        def request(method, path, payload=None):
            if path == '/api/sim/register':
                return {'registered': True}
            if path.startswith('/api/sim/poll'):
                polls.append(path)
                return {'command': {'request_id': 'write-once', 'method': 'set_parameter',
                                    'params': {'name': 'amplitude', 'value': 2}}}
            acknowledgements.append(payload.copy())
            if len(acknowledgements) == 1:
                raise TimeoutError('acknowledgement response was lost')
            fleet.stop_event.set()
            return {'state': 'applied'}

        fleet.request = request
        with patch.object(fleet.stop_event, 'wait', return_value=False):
            fleet.worker(fleet.instruments[0])
        self.assertEqual(fleet.instruments[0].parameter_writes, 1)
        self.assertEqual(len(polls), 1)
        self.assertEqual(acknowledgements[0], acknowledgements[1])


class CommandRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_submission_response_only_queries_existing_id(self):
        state = Console('http://192.168.2.38')
        command = {'request_id': 'write-once', 'device_id': 'sim-01', 'method': 'set_parameter',
                   'params': {'name': 'amplitude', 'value': 2}, 'ttl_ms': 3000}
        state.request = AsyncMock(side_effect=[TimeoutError(), {'state': 'applied', 'request_id': 'write-once'}])
        with patch.object(state, 'log_event'), patch('pc_backend.server.asyncio.sleep', new_callable=AsyncMock):
            result = await state.execute_command(command)
        self.assertEqual(result['state'], 'applied')
        self.assertTrue(result['recovered_response'])
        self.assertEqual([call.args[0] for call in state.request.await_args_list], ['POST', 'GET'])
        self.assertEqual(state.request.await_args_list[1].args[1], '/api/commands/write-once')

    async def test_transient_status_read_failure_does_not_resubmit(self):
        state = Console('http://192.168.2.38')
        state.request = AsyncMock(side_effect=[{'state': 'queued'}, TimeoutError(), {'state': 'completed'}])
        with patch.object(state, 'log_event'), patch('pc_backend.server.asyncio.sleep', new_callable=AsyncMock):
            result = await state.execute_command({'request_id': 'read-once', 'ttl_ms': 3000})
        self.assertEqual(result['state'], 'completed')
        self.assertEqual([call.args[0] for call in state.request.await_args_list], ['POST', 'GET', 'GET'])


class RecordingTests(unittest.TestCase):
    def test_missing_frame_is_silence_with_gap_metadata(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(dir=root / '.cache' / 'tmp') as temporary, patch('pc_backend.server.DATA', Path(temporary)):
            record = Recording({'audio': {'gain_db': 12}}, 'unit fixture, not real hardware')
            pcm = array('h', [100] * 960).tobytes()
            record.add(pcm, 10, 20000, 960)
            record.add(pcm, 12, 60000, 960)
            result = record.finish()
            self.assertEqual(result['missing_frames'], 1)
            self.assertEqual(result['silence_samples_inserted'], 960)
            with wave.open(str(record.directory / 'audio.wav')) as wav:
                self.assertEqual(wav.getnframes(), 2880)
                self.assertEqual(wav.getframerate(), 48000)
                data = array('h', wav.readframes(2880))
            self.assertTrue(all(value == 0 for value in data[960:1920]))
            metadata = json.loads((record.directory / 'metadata.json').read_text())
            self.assertEqual(metadata['status'], 'completed')

    def test_board_address_is_lan_only(self):
        self.assertEqual(valid_board_url('http://192.168.2.38/'), 'http://192.168.2.38')
        for invalid in ('https://192.168.2.38', 'http://example.com', 'http://8.8.8.8', 'http://user:pass@192.168.2.38', 'http://192.168.2.38/other'):
            with self.assertRaises(ValueError):
                valid_board_url(invalid)


if __name__ == '__main__':
    unittest.main()
