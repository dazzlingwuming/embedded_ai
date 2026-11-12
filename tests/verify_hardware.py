"""Real-board acceptance: audio recording, four simulated peers, and failure semantics."""
import argparse
from array import array
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import traceback
import uuid
import wave

from aiohttp import ClientSession, ClientTimeout, ClientError

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / 'data' / 'hardware-verification.json'
SIM_OFFLINE_REPORT = ROOT / 'data' / 'simulator-offline-verification.json'
SIM_DEVICE_TTL_MS = 6000
OBSERVED_OFFLINE_DURATION_MS = 16000


def save_report(results):
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


async def observe_simulated_disconnect(api, command, device='sim-04',
        duration_ms=OBSERVED_OFFLINE_DURATION_MS, clock=time.monotonic, sleep=asyncio.sleep):
    """Observe real board TTL transitions, not the console's cached status.

    A default 8s fault leaves only 2s beyond the 6s board TTL. The console's
    polling plus camera probe can skip that entire interval. This acceptance
    fixture requests a measured 16s silence and still requires both actual
    board transitions, the age threshold, and the acknowledged duration.
    """
    initial = await api('/api/status', board=True, timeout=3)
    initial_peer = next((peer for peer in initial['devices'] if peer['device_id']==device), None)
    evidence = {'passed':False, 'observer':'direct_board_status', 'device_id':device,
        'requested_offline_duration_ms':duration_ms, 'device_ttl_ms':SIM_DEVICE_TTL_MS,
        'observed_offline':False, 'reconnected':False, 'connected_at_end':bool(initial_peer and initial_peer['connected']),
        'trigger_state':None, 'trigger_duration_ms':None, 'observations':[], 'read_errors':[]}
    if not initial_peer or not initial_peer['connected']:
        evidence['error']='peer_not_online_before_fault'
        return evidence
    started = clock()
    deadline = started+duration_ms/1000+12
    trigger = asyncio.create_task(command('set_fault', {'mode':'offline','duration_ms':duration_ms}, device=device))
    previous_uptime = initial.get('uptime_ms')
    evidence['max_board_sample_gap_ms']=0
    gateway_changed=False
    try:
        while clock()<deadline:
            try:
                async with asyncio.timeout(min(3,max(.001,deadline-clock()))):
                    snapshot=await api('/api/status', board=True, timeout=3)
                if snapshot.get('boot_id')!=initial.get('boot_id'):
                    gateway_changed=True;break
                peer=next((peer for peer in snapshot['devices'] if peer['device_id']==device), None)
                if peer is None:
                    evidence['read_errors'].append('device_missing')
                else:
                    age=peer.get('last_seen_age_ms')
                    evidence['connected_at_end']=bool(peer['connected'])
                    uptime=snapshot.get('uptime_ms')
                    if isinstance(uptime,(int,float)) and isinstance(previous_uptime,(int,float)):
                        evidence['max_board_sample_gap_ms']=max(evidence['max_board_sample_gap_ms'],uptime-previous_uptime)
                    previous_uptime=uptime
                    evidence['observations'].append({'elapsed_ms':round((clock()-started)*1000,1),
                        'board_uptime_ms':uptime,'connected':peer['connected'],'last_seen_age_ms':age})
                    if peer['connected'] is False and isinstance(age,(int,float)) and age>=SIM_DEVICE_TTL_MS:
                        evidence['observed_offline']=True
                    elif evidence['observed_offline'] and peer['connected'] is True and isinstance(age,(int,float)) and age<SIM_DEVICE_TTL_MS:
                        evidence['reconnected']=True;break
            except (ClientError,OSError,asyncio.TimeoutError) as error:
                evidence['read_errors'].append(type(error).__name__)
            if clock()<deadline:
                await sleep(min(.25,deadline-clock()))
        if gateway_changed:
            evidence['error']='gateway_restarted_during_fault'
        try:
            async with asyncio.timeout(max(.001,deadline-clock())):
                result=await trigger
            evidence['trigger_state']=result.get('state')
            evidence['trigger_duration_ms']=(result.get('result') or {}).get('duration_ms')
        except (ClientError,OSError,asyncio.TimeoutError) as error:
            evidence['trigger_error']=type(error).__name__
        evidence['passed']=(not gateway_changed and evidence['observed_offline'] and evidence['reconnected']
            and evidence['trigger_state']=='applied' and evidence['trigger_duration_ms']==duration_ms)
        return evidence
    finally:
        if not trigger.done():trigger.cancel()
        await asyncio.gather(trigger,return_exceptions=True)


async def check_simulated_disconnect_only():
    """A separate real-board retest; no recording, camera, speaker or speech API."""
    report={'test_type':'real_gateway_simulated_disconnect_only', 'passed':False,
            'uses_audio':False, 'uses_camera':False, 'cloud_api_used':False,
            'physical_instruments':False, 'started_utc':datetime.now(timezone.utc).isoformat()}
    fleet_started=False
    async with ClientSession(trust_env=False,timeout=ClientTimeout(total=12)) as client:
        board_url=None
        async def api(path,payload=None,board=False,timeout=None):
            base=board_url if board else 'http://127.0.0.1:8765'
            options={'json':payload}
            if timeout is not None:options['timeout']=ClientTimeout(total=timeout)
            async with client.request('GET' if payload is None else 'POST',base+path,**options) as response:
                data=await response.json(content_type=None)
                assert response.status<400,(response.status,data)
                return data
        async def command(method,params=None,device='sim-04'):
            return await api('/api/commands',{'request_id':uuid.uuid4().hex,'device_id':device,
                'method':method,'params':params or {},'ttl_ms':3000})
        try:
            current=await api('/api/status')
            if current['recording'] or current.get('voice',{}).get('active'):
                raise RuntimeError('Stop active voice/recording before the isolated simulator retest')
            if current.get('simulator',{}).get('running'):
                raise RuntimeError('An existing simulator fleet is already running')
            assert current['board_connected'],'Board is not connected'
            board_url=current['board_url'].rstrip('/')
            report.update(board_url=board_url,firmware=current['board'].get('firmware'))
            board=await api('/api/status',board=True,timeout=3)
            if (board.get('speaker',{}).get('connected') or board.get('speaker',{}).get('active')
                    or board.get('speech_frontend',{}).get('streaming')):
                raise RuntimeError('A board speaker/speech stream is in use by another task')
            fleet_started=True
            await api('/api/simulators/start',{'count':4})
            deadline=time.monotonic()+12
            while True:
                board=await api('/api/status',board=True,timeout=3)
                if sum(bool(peer['connected']) for peer in board['devices'])==4:break
                if time.monotonic()>=deadline:raise RuntimeError('Four simulated peers did not register')
                await asyncio.sleep(.25)
            report['transition']=await observe_simulated_disconnect(api,command)
            report['passed']=report['transition']['passed']
        except Exception as error:
            report['error']=str(error) if isinstance(error,RuntimeError) else type(error).__name__
        finally:
            if fleet_started:
                try:await api('/api/simulators/stop',{})
                except Exception as error:
                    report['passed']=False;report['cleanup_error']=type(error).__name__
    report['finished_utc']=datetime.now(timezone.utc).isoformat()
    SIM_OFFLINE_REPORT.parent.mkdir(exist_ok=True)
    SIM_OFFLINE_REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)
    if not report['passed']:raise SystemExit(1)


async def main(seconds, include_video=False):
    results = {'test_type': 'real_esp32_with_software_instrument_peers',
               'simulator_host': 'same_pc_as_console', 'physical_usb_instruments': False, 'checks': [],
               'passed': False, 'status': 'running', 'started_utc': datetime.now(timezone.utc).isoformat(),
               'requested_joint_stable_seconds': seconds}
    save_report(results)
    async with ClientSession(trust_env=False, timeout=ClientTimeout(total=40)) as client:
        async def api(path, payload=None, board=False, timeout=None):
            base = 'http://192.168.2.38' if board else 'http://127.0.0.1:8765'
            options={'json':payload}
            if timeout is not None:options['timeout']=ClientTimeout(total=timeout)
            async with client.request('GET' if payload is None else 'POST', base + path, **options) as response:
                data = await response.json(content_type=None)
                assert response.status < 400, (response.status, data)
                return data

        def record_check(name, condition, **evidence):
            results['checks'].append({'check': name, 'passed': bool(condition), **evidence})
            save_report(results)
            print('PASS' if condition else 'FAIL', name, json.dumps(evidence, ensure_ascii=False), flush=True)

        def passed(name, **evidence):
            record_check(name, True, **evidence)

        async def command(method, params=None, device='sim-01', request_id=None, ttl=3000):
            return await api('/api/commands', {'request_id': request_id or uuid.uuid4().hex,
                'device_id': device, 'method': method, 'params': params or {}, 'ttl_ms': ttl})

        current = await api('/api/status')
        assert current['board_connected'] and current['board']['audio']['ready']
        assert current['board']['audio']['alc_register'] == 0
        if include_video:
            assert current['board']['video_enabled'] and current['board']['camera']['ready']
        results['initial_board'] = current['board']
        recording = await api('/api/recordings/start', {'label': '联测：真实音视频与四台网络模拟仪器' if include_video else '联测：真实麦克风与四台网络模拟仪器'})
        started = time.monotonic()
        finished = None
        video_ws = video_task = None
        video_sample = {'enabled': include_video, 'frames': 0, 'bytes': 0, 'error': None}
        try:
            if include_video:
                video_ws = await client.ws_connect('http://127.0.0.1:8765/ws/camera')
                assert (await video_ws.receive_json(timeout=5))['source'] == 'real_camera'
                async def receive_video():
                    async for message in video_ws:
                        if message.type.name == 'BINARY':
                            assert message.data.startswith(b'\xff\xd8') and message.data.endswith(b'\xff\xd9')
                            video_sample['frames'] += 1
                            video_sample['bytes'] += len(message.data)
                        elif message.type.name == 'TEXT' and message.json().get('error'):
                            video_sample['error'] = message.json()['error']
                video_task = asyncio.create_task(receive_video())
            await api('/api/simulators/start', {'count': 4})
            for _ in range(30):
                current = await api('/api/status')
                peers = [d for d in current['board']['devices'] if d['connected']]
                if len(peers) == 4:
                    break
                await asyncio.sleep(.3)
            assert len(peers) == 4, current['simulator']
            passed('four_simulated_peers_registered', device_ids=[d['device_id'] for d in peers])
            identities = await asyncio.gather(*(command('identify', device=d['device_id']) for d in peers))
            assert all(r['state'] == 'completed' and r['result']['source'] == 'simulated' for r in identities)
            assert all(r['gateway_boot_id'] == current['board']['boot_id'] for r in identities)
            passed('four_bidirectional_paths_through_gateway', gateway_boot_id=current['board']['boot_id'])

            baseline = await command('read_status')
            writes_before = baseline['result']['parameter_writes']
            rid = uuid.uuid4().hex
            changed = await command('set_parameter', {'name': 'amplitude', 'value': 2}, request_id=rid)
            repeated = await command('set_parameter', {'name': 'amplitude', 'value': 2}, request_id=rid)
            after = await command('read_status')
            assert changed['state'] == repeated['state'] == 'applied'
            assert after['result']['parameter_writes'] == writes_before + 1
            passed('duplicate_request_applies_once', request_id=rid)
            conflict = await command('set_parameter', {'name': 'amplitude', 'value': 3}, request_id=rid)
            assert conflict['state'] == 'rejected' and conflict['reason'] == 'request_id_conflict'
            passed('duplicate_id_with_changed_payload_rejected')
            waveform = await command('read_waveform')
            assert waveform['state'] == 'completed' and len(waveform['result']['samples']) == 128
            assert waveform['result']['parameters']['amplitude'] == 2
            passed('simulated_parameter_and_waveform_correlate')

            assert (await command('set_fault', {'mode': 'reject'}))['state'] == 'applied'
            rejected = await command('set_parameter', {'name': 'amplitude', 'value': 3})
            assert rejected['state'] == 'rejected'
            await command('set_fault', {'mode': 'normal'})
            assert (await command('read_status'))['result']['parameters']['amplitude'] == 2
            passed('device_rejection_does_not_modify_parameter')

            await command('set_fault', {'mode': 'timeout'})
            # Leave time for a peer to claim the job on a jittery WLAN; this
            # check exercises a missing result after receipt, not queue expiry.
            timed = await command('read_status', ttl=3000)
            assert timed['state'] == 'unknown'
            await command('set_fault', {'mode': 'normal'})
            passed('received_command_timeout_is_unknown')

            await command('set_fault', {'mode': 'lost_ack'})
            lost_id = uuid.uuid4().hex
            lost = await command('set_parameter', {'name': 'frequency_hz', 'value': 75}, request_id=lost_id, ttl=3000)
            assert lost['state'] == 'unknown', lost
            await command('set_fault', {'mode': 'normal'})
            check = await command('read_status')
            writes = check['result']['parameter_writes']
            assert check['result']['parameters']['frequency_hz'] == 75
            again = await command('set_parameter', {'name': 'frequency_hz', 'value': 75}, request_id=lost_id, ttl=3000)
            assert again['state'] == 'unknown'
            assert (await command('read_status'))['result']['parameter_writes'] == writes
            passed('lost_ack_can_be_applied_without_replay')

            offline = await observe_simulated_disconnect(api,command)
            record_check('simulated_device_disconnect_and_reconnect_visible',offline.pop('passed'),**offline)

            # Pause local workers, register a silent peer, expire a queued write, then poll.
            await api('/api/simulators/stop', {})
            session = uuid.uuid4().hex
            await api('/api/sim/register', {'device_id': 'sim-04', 'session_id': session,
                'name': '过期命令验证', 'source': 'simulated'}, board=True)
            ack_id = uuid.uuid4().hex
            await api('/api/commands', {'request_id': ack_id, 'device_id': 'sim-04',
                'method': 'read_status', 'params': {}, 'ttl_ms': 3000}, board=True)
            claimed = await api(f'/api/sim/poll?device_id=sim-04&session_id={session}', board=True)
            assert claimed['command']['request_id'] == ack_id
            acknowledgement = {'device_id': 'sim-04', 'session_id': session, 'request_id': ack_id,
                'source': 'simulated', 'state': 'completed', 'result': {'source': 'simulated', 'fixture': 'ack_retry'}}
            first_ack = await api('/api/sim/result', acknowledgement, board=True)
            repeated_ack = await api('/api/sim/result', acknowledgement, board=True)
            assert first_ack == repeated_ack and first_ack['state'] == 'completed'
            passed('repeated_result_acknowledgement_is_idempotent')
            expired_id = uuid.uuid4().hex
            queued = await api('/api/commands', {'request_id': expired_id, 'device_id': 'sim-04',
                'method': 'set_parameter', 'params': {'name': 'amplitude', 'value': 4}, 'ttl_ms': 250}, board=True)
            assert queued['state'] == 'queued'
            await asyncio.sleep(.4)
            expired = await api('/api/commands/' + expired_id, board=True)
            assert expired['state'] == 'expired'
            polled = await api(f'/api/sim/poll?device_id=sim-04&session_id={session}', board=True)
            assert polled['command'] is None
            passed('queued_expired_write_not_replayed_on_reconnect')
            await api('/api/simulators/start', {'count': 4})
            await asyncio.sleep(1)
            healthy_cycles = 0
            failed_cycles = []
            audio_interrupted = False
            last_progress = 0
            # The requested stability window begins after the deliberately
            # disruptive fault checks. Recording and video span both phases.
            joint_started = time.monotonic()
            while healthy_cycles + len(failed_cycles) == 0 or time.monotonic() - joint_started < seconds:
                replies = await asyncio.gather(*(command('read_status', device=f'sim-{n:02d}') for n in range(1, 5)))
                failures = [{'device_id': r.get('device_id'), 'state': r.get('state'), 'reason': r.get('reason')}
                            for r in replies if r['state'] != 'completed']
                if failures:
                    failed_cycles.append({'elapsed_seconds': round(time.monotonic()-joint_started, 2), 'commands': failures})
                    print('FAILED CYCLE', json.dumps(failed_cycles[-1]), flush=True)
                snapshot = await api('/api/status')
                if not snapshot['recording']:
                    audio_interrupted = True
                    break
                assert snapshot['board']['boot_id'] == results['initial_board']['boot_id'], 'Unexpected gateway restart'
                healthy_cycles += not failures
                elapsed = time.monotonic() - joint_started
                if elapsed - last_progress > 30:
                    print(f'STABILITY {elapsed:.0f}s; audio frames={snapshot["audio"]["frames"]}; command cycles={healthy_cycles}', flush=True)
                    last_progress = elapsed
                await asyncio.sleep(1)
            joint_elapsed = time.monotonic()-joint_started
            results['joint_stable_seconds'] = round(joint_elapsed, 2)
            record_check('joint_audio_and_four_peer_operation', healthy_cycles > 0 and joint_elapsed >= seconds and not failed_cycles and not audio_interrupted,
                         requested_joint_stable_seconds=seconds, joint_stable_seconds=round(joint_elapsed, 2),
                         total_recording_seconds=round(time.monotonic()-started, 2), successful_command_cycles=healthy_cycles,
                         failed_cycles=failed_cycles, audio_interrupted=audio_interrupted)
            if include_video:
                live = await api('/api/status')
                record_check('concurrent_camera_during_microphone_and_four_peers',
                    video_sample['frames'] > 100 and not video_sample['error'] and not video_task.done() and
                    live['board']['camera']['capture_errors'] == 0 and live['board']['camera']['encode_errors'] == 0,
                    **video_sample, timing=live['camera'])
        finally:
            finished = await api('/api/recordings/stop', {})
            if finished.get('status') == 'idle':
                # Capture may already have stopped itself on a network gap.
                # Keep the real metadata instead of losing it to an idle reply.
                metadata = ROOT / 'data' / 'sessions' / recording['session_id'] / 'metadata.json'
                if metadata.is_file():
                    finished = json.loads(metadata.read_text(encoding='utf-8'))
            await api('/api/simulators/stop', {})
            if video_ws:
                await video_ws.close()
            if video_task:
                video_task.cancel()
                await asyncio.gather(video_task, return_exceptions=True)
            results['video_sample'] = video_sample
            results['recording'] = finished
            results['total_recording_seconds'] = finished.get('duration_seconds')
            save_report(results)
        record_check('recording_completed_without_interruption', finished['status'] == 'completed',
                     status=finished['status'], stop_reason=finished.get('stop_reason'))
        path = ROOT / 'data' / 'sessions' / finished['session_id'] / 'audio.wav'
        with wave.open(str(path)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 48000)
            nframes = wav.getnframes()
            values = array('h', wav.readframes(min(nframes, 48000 * 5)))
        assert nframes == finished['samples_written'] and len(set(values)) > 10
        passed('real_pcm_wav_readable_and_not_all_zero', duration=nframes/48000,
               unique_values_in_first_5s=len(set(values)), missing_frames=finished['missing_frames'],
               rms_first_5s=round(math.sqrt(sum(x*x for x in values)/len(values)), 3))
        results['recording'] = finished
        results['final_status'] = await api('/api/status')
        results['passed'] = all(item['passed'] for item in results['checks'])
        results.update(status='passed' if results['passed'] else 'failed', finished_utc=datetime.now(timezone.utc).isoformat())
        save_report(results)
        print('ALL HARDWARE CHECKS PASSED' if results['passed'] else 'HARDWARE CHECKS HAVE FAILURES; SEE REPORT', flush=True)
        if not results['passed']:
            raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=180, help='Four-peer stability duration after fault checks; recording/video span both phases')
    parser.add_argument('--video', action='store_true', help='Receive the real camera concurrently with audio and instrument peers')
    parser.add_argument('--sim-offline-only', action='store_true', help='Retest only simulator disconnect/reconnect; no audio, video or cloud calls')
    try:
        args = parser.parse_args()
        if args.sim_offline_only:asyncio.run(check_simulated_disconnect_only())
        else:asyncio.run(main(args.seconds, args.video))
    except Exception:
        target = SIM_OFFLINE_REPORT if getattr(args,'sim_offline_only',False) else REPORT
        failed = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
        failed.update(passed=False, status='failed', error=traceback.format_exc(),
                      finished_utc=datetime.now(timezone.utc).isoformat())
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(failed,ensure_ascii=False,indent=2),encoding='utf-8')
        raise
