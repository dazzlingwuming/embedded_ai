import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from simulators.instrument import SimulatedInstrument, SimulatorFleet

SCRIPT=Path(__file__).with_name('verify_hardware.py')
SPEC=importlib.util.spec_from_file_location('hardware_acceptance_fixture',SCRIPT)
VERIFIER=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class FaultDurationTests(unittest.TestCase):
    def command(self,identity,duration=None):
        params={'mode':'offline'}
        if duration is not None:params['duration_ms']=duration
        return {'request_id':identity,'method':'set_fault','params':params}

    def test_default_pause_remains_eight_seconds_and_explicit_duration_is_echoed(self):
        instrument=SimulatedInstrument('sim-04')
        with patch('simulators.instrument.time.monotonic',return_value=100):
            result=instrument.handle(self.command('default'))
            self.assertEqual(instrument.offline_until,108)
            self.assertEqual(result[1]['duration_ms'],8000)
            result=instrument.handle(self.command('longer',16000))
            self.assertEqual(instrument.offline_until,116)
            self.assertEqual(result[1]['duration_ms'],16000)

    def test_retrying_same_fault_does_not_extend_its_silence(self):
        instrument=SimulatedInstrument('sim-04');command=self.command('same',16000)
        with patch('simulators.instrument.time.monotonic',return_value=100):first=instrument.handle(command)
        with patch('simulators.instrument.time.monotonic',return_value=200):second=instrument.handle(command)
        self.assertEqual(first,second)
        self.assertEqual(instrument.offline_until,116)

    def test_invalid_pause_is_rejected_without_mutating_fault_state(self):
        instrument=SimulatedInstrument('sim-04')
        for index,duration in enumerate((False,0,500,60001,1500.5,'16000')):
            result=instrument.handle(self.command(str(index),duration))
            self.assertEqual(result[0],'rejected')
            self.assertEqual(instrument.fault,'normal')
            self.assertEqual(instrument.offline_until,0)

    def test_worker_really_stops_all_network_requests_for_the_requested_interval(self):
        fleet=SimulatorFleet('http://offline.invalid',1)
        now=[0.0];calls=[];polls=0
        def request(method,path,payload=None):
            nonlocal polls
            calls.append((now[0],path))
            if path=='/api/sim/register':return {'registered':True}
            if path.startswith('/api/sim/poll'):
                polls+=1
                if polls==1:return {'command':self.command('offline',16000)}
                fleet.stop_event.set();return {'command':None}
            return {'state':'applied'}
        def wait(seconds):now[0]+=seconds;return fleet.stop_event.is_set()
        fleet.request=request
        with patch('simulators.instrument.time.monotonic',side_effect=lambda:now[0]), \
             patch.object(fleet.stop_event,'wait',side_effect=wait):
            fleet.worker(fleet.instruments[0])
        registrations=[at for at,path in calls if path=='/api/sim/register']
        self.assertEqual(len(registrations),2)
        self.assertGreaterEqual(registrations[1]-registrations[0],16)
        self.assertFalse(any(0<at<16 for at,_ in calls))

    def test_starting_one_fleet_twice_cannot_create_competing_sessions(self):
        fleet=SimulatorFleet('http://offline.invalid',4)
        with patch('simulators.instrument.threading.Thread') as factory:
            fleet.start();fleet.start()
            self.assertEqual(factory.call_count,4)
            self.assertEqual(len(fleet.threads),4)
            fleet.stop()


class GatewayTimeline:
    def __init__(self,never_offline=False,never_reconnect=False,reboot=False,echo_duration=True):
        self.now=0.0;self.fault_start=None;self.fault_end=0.0
        self.never_offline=never_offline;self.never_reconnect=never_reconnect
        self.reboot=reboot;self.echo_duration=echo_duration
        self.read_requests=[]

    def clock(self):return self.now

    async def sleep(self,seconds):
        self.now+=seconds
        await asyncio.sleep(0)

    async def command(self,method,params,device):
        self.fault_start=self.now;self.fault_end=self.now+params['duration_ms']/1000
        result={'mode':'offline'}
        if self.echo_duration:result['duration_ms']=params['duration_ms']
        return {'state':'applied','result':result}

    async def api(self,path,payload=None,board=False,timeout=None):
        self.read_requests.append((path,board,timeout))
        if not board:raise AssertionError('The console cache must not decide board transitions')
        self.now+=.03
        active=(self.fault_start is not None and self.now>=self.fault_start and
                (self.now<self.fault_end or self.never_reconnect))
        age=round((self.now-self.fault_start)*1000) if active else 0
        connected=age<6000 or self.never_offline
        return {'boot_id':'rebooted' if self.reboot and age>=7000 else 'original',
            'uptime_ms':round(self.now*1000),'devices':[{'device_id':'sim-04',
                'connected':connected,'last_seen_age_ms':age}]}


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def observe(self,scenario):
        return await VERIFIER.observe_simulated_disconnect(scenario.api,scenario.command,
            clock=scenario.clock,sleep=scenario.sleep)

    async def test_cached_status_can_miss_old_window_but_direct_measured_transition_is_required(self):
        # TTL 6s, old default reconnect 8s; cached samples at 0/5/10 never see it.
        old_cached_connected=[not(6<=second<8) for second in (0,5,10)]
        self.assertTrue(all(old_cached_connected))
        scenario=GatewayTimeline();result=await self.observe(scenario)
        self.assertTrue(result['passed'])
        self.assertTrue(result['observed_offline'] and result['reconnected'])
        self.assertEqual(result['trigger_duration_ms'],16000)
        self.assertTrue(all(board for _,board,_ in scenario.read_requests))
        self.assertTrue(any(not item['connected'] and item['last_seen_age_ms']>=6000 for item in result['observations']))

    async def test_never_offline_is_a_failure_and_final_online_is_not_called_reconnection(self):
        result=await self.observe(GatewayTimeline(never_offline=True))
        self.assertFalse(result['passed'])


        self.assertFalse(result['observed_offline'])
        self.assertFalse(result['reconnected'])
        self.assertTrue(result['connected_at_end'])

    async def test_no_reconnection_and_gateway_reboot_remain_failures(self):
        for scenario in (GatewayTimeline(never_reconnect=True),GatewayTimeline(reboot=True)):
            result=await self.observe(scenario)
            self.assertFalse(result['passed'])

    async def test_old_simulator_that_ignores_duration_cannot_pass(self):
        result=await self.observe(GatewayTimeline(echo_duration=False))
        self.assertTrue(result['observed_offline'] and result['reconnected'])
        self.assertFalse(result['passed'])




class StandaloneGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_speaker_or_speech_owner_blocks_mutating_retest(self):
        root=Path(__file__).resolve().parent.parent
        for busy in ({'speaker':{'connected':True}},{'speech_frontend':{'streaming':True}}):
            calls=[]
            class Response:
                status=200
                def __init__(self,url):self.url=url
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                async def json(self,**kwargs):
                    if '127.0.0.1' in self.url:
                        return {'recording':None,'voice':{'active':False},'simulator':{'running':False},
                            'board_connected':True,'board_url':'http://offline.invalid','board':{'firmware':'0.6.0'}}
                    return busy
            class Session:
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                def request(self,method,url,**kwargs):
                    calls.append((method,url));return Response(url)
            with tempfile.TemporaryDirectory(dir=root/'.cache'/'tmp') as directory, \
                 patch.object(VERIFIER,'SIM_OFFLINE_REPORT',Path(directory)/'report.json'), \
                 patch.object(VERIFIER,'ClientSession',return_value=Session()), \
                 patch('builtins.print'):
                with self.assertRaises(SystemExit):await VERIFIER.check_simulated_disconnect_only()
                self.assertTrue(all(method=='GET' for method,_ in calls))
                self.assertFalse(any('/api/simulators/' in url for _,url in calls))
                evidence=json.loads((Path(directory)/'report.json').read_text(encoding='utf-8'))
                self.assertFalse(evidence['passed'])
                self.assertIn('in use',evidence['error'])


if __name__=='__main__':unittest.main()
