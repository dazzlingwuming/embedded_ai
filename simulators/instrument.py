"""Network instrument simulator. Runs on this PC or a separate laptop, stdlib only.

All generated readings and parameter writes are explicitly simulated. Workers
poll the ESP32 gateway, so the complete request/response path traverses the board.
"""
import argparse
from collections import OrderedDict
from datetime import datetime, timezone
import json
import http.client
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class SimulatedInstrument:
    def __init__(self, device_id):
        self.device_id = device_id
        self.parameters = {'amplitude': 1.0, 'frequency_hz': 50.0, 'offset': 0.0}
        self.parameter_writes = 0
        self.deliveries = 0
        self.fault = 'normal'
        self.offline_until = 0.0
        self.cache = OrderedDict()

    def remember(self, request_id, response):
        self.cache[request_id] = response
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return response

    def handle(self, command):
        self.deliveries += 1
        request_id = command['request_id']
        if request_id in self.cache:
            return self.cache[request_id]
        method = command.get('method')
        params = command.get('params', {})
        result = {'source': 'simulated', 'device_id': self.device_id,
                  'device_generated_utc': utc_now()}
        state = 'completed'
        if command.get('remaining_ms', 1) <= 0:
            state, result['error'] = 'rejected', 'expired_before_execution'
        elif method == 'set_fault':
            mode = params.get('mode')
            duration_ms = params.get('duration_ms', 8000)
            if mode not in {'normal', 'reject', 'timeout', 'lost_ack', 'offline'}:
                state, result['error'] = 'rejected', 'invalid_fault_mode'
            elif mode == 'offline' and (isinstance(duration_ms, bool) or not isinstance(duration_ms, int)
                                       or not 1000 <= duration_ms <= 60000):
                state, result['error'] = 'rejected', 'invalid_offline_duration'
            else:
                self.fault = mode
                if mode == 'offline':
                    self.offline_until = time.monotonic() + duration_ms/1000
                    result['duration_ms'] = duration_ms
                state, result['mode'] = 'applied', mode
        elif self.fault == 'reject':
            state, result['error'] = 'rejected', 'simulated_device_rejection'
        elif self.fault == 'timeout':
            return self.remember(request_id, None)
        elif method == 'identify':
            result.update(model='BenchLink virtual instrument', protocol='network-simulator-v1',
                          physical_device=False)
        elif method == 'read_status':
            result.update(parameters=self.parameters.copy(), parameter_writes=self.parameter_writes,
                          deliveries=self.deliveries, fault=self.fault)
        elif method == 'set_parameter':
            name, value = params.get('name'), params.get('value')
            limits = {'amplitude': (0, 5), 'frequency_hz': (1, 2000), 'offset': (-2, 2)}
            if name not in limits or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                state, result['error'] = 'rejected', 'invalid_parameter'
            elif not limits[name][0] <= value <= limits[name][1]:
                state, result['error'] = 'rejected', 'parameter_out_of_range'
            else:
                previous = self.parameters[name]
                self.parameters[name] = float(value)
                self.parameter_writes += 1
                state = 'applied'
                result.update(name=name, previous=previous, value=value,
                              parameter_writes=self.parameter_writes)
                if self.fault == 'lost_ack':
                    return self.remember(request_id, None)
        elif method == 'read_waveform':
            rate, count = 10000, 128
            a, f, offset = (self.parameters[k] for k in ('amplitude', 'frequency_hz', 'offset'))
            samples = [round(offset + a * (math.sin(2 * math.pi * f * n / rate) +
                        0.12 * math.sin(6 * math.pi * f * n / rate)), 5) for n in range(count)]
            result.update(sample_rate=rate, unit='V (simulated)', samples=samples,
                          parameters=self.parameters.copy())
        else:
            state, result['error'] = 'rejected', 'unsupported_method'
        response = (state, result)
        return self.remember(request_id, response)


class SimulatorFleet:
    def __init__(self, board_url, count=4, prefix='sim'):
        if not 1 <= count <= 4:
            raise ValueError('Use 1 to 4 simulated instruments')
        self.board_url = board_url.rstrip('/')
        self.stop_event = threading.Event()
        self.instruments = [SimulatedInstrument(f'{prefix}-{i + 1:02d}') for i in range(count)]
        self.threads = []
        self.worker_status = {}
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.connections = threading.local()

    def request(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        connection = getattr(self.connections, 'http', None)
        if connection is None:
            address = urllib.parse.urlsplit(self.board_url)
            connection = http.client.HTTPConnection(address.hostname, address.port or 80, timeout=3)
            self.connections.http = connection
        try:
            connection.request(method, path, body=data, headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            result = json.loads(response.read())
            if response.status >= 400:
                raise ValueError(result.get('error', f'HTTP {response.status}'))
            return result
        except (OSError, ValueError, http.client.HTTPException):
            connection.close()
            self.connections.http = None
            raise

    def update(self, device_id, **values):
        with self.lock:
            self.worker_status.setdefault(device_id, {}).update(values)

    def worker(self, instrument):
        session = uuid.uuid4().hex
        registered = False
        pending_result = None
        while not self.stop_event.is_set():
            if instrument.offline_until > time.monotonic():
                self.update(instrument.device_id, connected=False, error='simulated_disconnect')
                registered = False
                self.stop_event.wait(0.25)
                continue
            try:
                if not registered:
                    # An HTTP retry is still the same instrument process and
                    # deduplication cache. Preserve identity across that retry.
                    self.request('POST', '/api/sim/register', {
                        'device_id': instrument.device_id, 'session_id': session,
                        'name': f'模拟仪器 {instrument.device_id}', 'source': 'simulated'})
                    registered = True
                if pending_result is None:
                    query = urllib.parse.urlencode({'device_id': instrument.device_id, 'session_id': session})
                    payload = self.request('GET', '/api/sim/poll?' + query)
                    command = payload.get('command')
                    if command:
                        answer = instrument.handle(command)
                        if answer:
                            state, result = answer
                            pending_result = {
                                'device_id': instrument.device_id, 'session_id': session,
                                'request_id': command['request_id'], 'source': 'simulated',
                                'state': state, 'result': result}
                if pending_result is not None:
                    self.request('POST', '/api/sim/result', pending_result)
                    pending_result = None
                self.update(instrument.device_id, connected=True, error=None,
                            last_seen_utc=utc_now(), deliveries=instrument.deliveries)
            except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
                if isinstance(error, ValueError):
                    pending_result = None  # Gateway no longer accepts that result.
                registered = False
                self.update(instrument.device_id, connected=False, error=type(error).__name__)
                self.stop_event.wait(0.75)
            self.stop_event.wait(0.3)
        connection = getattr(self.connections, 'http', None)
        if connection:
            connection.close()
        self.update(instrument.device_id, connected=False, error='simulator_stopped')

    def start(self):
        with self.start_lock:
            # Calling start twice on one fleet must not create a second session
            # polling the same device ID and hiding a simulated disconnection.
            if self.threads or self.stop_event.is_set():
                return self
            for instrument in self.instruments:
                thread = threading.Thread(target=self.worker, args=(instrument,), daemon=True,
                                          name=f'simulator-{instrument.device_id}')
                thread.start()
                self.threads.append(thread)
        return self

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=4)

    def status(self):
        with self.lock:
            return {'running': bool(self.threads) and not self.stop_event.is_set(),
                    'board_url': self.board_url, 'source': 'simulated',
                    'devices': {k: v.copy() for k, v in self.worker_status.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', required=True, help='Example: http://192.168.2.38')
    parser.add_argument('--devices', type=int, default=4)
    parser.add_argument('--prefix', default='sim')
    args = parser.parse_args()
    fleet = SimulatorFleet(args.board, args.devices, args.prefix).start()
    print(f'网络模拟仪器运行中：{args.devices} 台 → {args.board}；没有连接真实仪器。', flush=True)
    try:
        while True:
            time.sleep(5)
            print(json.dumps(fleet.status(), ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        fleet.stop()


if __name__ == '__main__':
    main()
