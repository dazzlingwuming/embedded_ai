"""Record a fixed, real-camera paired experiment without changing production.

Only subscribes to the existing camera relay. No model calls, audio, settings
changes, JPEG re-encoding or generated frames. Images remain in ignored data/.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from aiohttp import ClientSession, ClientTimeout, WSMsgType
from pc_backend.vision_frames import VisionFrames

DECISIONS = [16, 19, 23, 26, 29, 33, 36, 39, 43, 46, 49, 53, 56, 59, 63, 66, 69, 73]
PHASES = [(0, 15, 'prepare_keyboard'), (15, 25, 'keyboard_stable'),
          (25, 35, 'keyboard_motion'), (35, 45, 'cup_stable'),
          (45, 55, 'cup_motion'), (55, 75, 'return_to_keyboard')]

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

async def capture(destination, console, duration=75, decision_times=None, phases=None):
    decision_times = DECISIONS if decision_times is None else decision_times
    phases = PHASES if phases is None else phases
    destination.mkdir(parents=True, exist_ok=False)
    (destination/'frames').mkdir()
    source = ROOT/'pc_backend/vision_frames.py'
    protocol = {'recorded_plan_utc': stamp(), 'source': 'real_camera_relay_websocket',
        'duration_seconds':duration, 'decision_seconds':decision_times,
        'phases':[{'start':a,'end':b,'intended_action':name} for a,b,name in phases],
        'production_selector_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'policy_parameters':{'window_seconds':3.0,'max_frames':1,'timeout_seconds':2.0},
        'ground_truth':'intended actions are NOT annotations; inspect actual frames before cloud evaluation',
        'no_cloud_calls':True, 'no_settings_changes':True}
    write_json(destination/'protocol.json',protocol)
    sampler=VisionFrames()
    records=[]
    decisions=[]
    by_sha={}
    latest=None
    errors=[]
    t0=None
    async with ClientSession(trust_env=False,timeout=ClientTimeout(total=6)) as client:
        async with client.get(console+'/api/status') as response:
            state=await response.json()
        if not state.get('board_connected') or state.get('voice',{}).get('active') or state.get('recording'):
            raise RuntimeError('Camera unavailable or user audio session active; no takeover attempted')
        write_json(destination/'initial-state.json', {'camera':state['camera'],
            'board_boot_id':state['board'].get('boot_id'),'vision':state['vision'],
            'speaker_volume':state['board']['speaker']['volume']})
        await sampler.start()
        try:
            async with client.ws_connect(console+'/ws/camera',heartbeat=10,max_msg_size=2*1024*1024+1024) as ws:
                hello=await ws.receive_json(timeout=5)
                if hello.get('source')!='real_camera':
                    raise RuntimeError('Not a real camera relay')
                t0=time.monotonic()
                print(json.dumps({'event':'recording_started','utc':stamp(),'directory':str(destination),'duration_seconds':duration}),flush=True)
                frames_file=(destination/'frames.jsonl').open('w',encoding='utf-8')

                async def receive():
                    nonlocal latest
                    async for message in ws:
                        if message.type==WSMsgType.BINARY:
                            received=time.monotonic()
                            raw=message.data
                            index=len(records)
                            sha=hashlib.sha256(raw).hexdigest()
                            path=f'frames/{index:06d}.jpg'
                            (destination/path).write_bytes(raw)
                            record={'index':index,'path':path,'sha256':sha,'bytes':len(raw),
                                'received_utc':stamp(),'elapsed_seconds':received-t0,
                                'received_monotonic':received}
                            records.append(record)
                            by_sha.setdefault(sha,[]).append(record)
                            latest=record
                            frames_file.write(json.dumps(record)+'\n')
                            sampler.offer(raw,received)
                        elif message.type==WSMsgType.TEXT:
                            errors.append(message.json())
                        else:
                            errors.append({'websocket_type':str(message.type)})
                            return

                async def choose(at):
                    await asyncio.sleep(max(0,t0+at-time.monotonic()))
                    before=time.monotonic()
                    baseline=dict(latest) if latest else None
                    chosen=await sampler.select(window_seconds=3.0,max_frames=1,timeout_seconds=2.0)
                    finished=time.monotonic()
                    selected=[]
                    for image in chosen.images:
                        matches=by_sha.get(image['sha256'],[])
                        record=min(matches,key=lambda item:abs(item['received_monotonic']-(finished-image['age_seconds']))) if matches else None
                        if record is None:
                            raise RuntimeError('Selected image missing from the common captured stream')
                        selected.append({**record,'selector_frame_id':image['frame_id'],
                            'quality':image['quality'],'selection_age_seconds':image['age_seconds']})
                    phase=next(name for a,b,name in phases if a<=at<b)
                    result={'id':f'd{len(decisions)+1:02d}','planned_elapsed_seconds':at,
                        'actual_elapsed_seconds':before-t0,'intended_phase':phase,
                        'latest':baseline,'selected':selected,'selection':chosen.public(),
                        'selector_wall_ms':(finished-before)*1000,
                        'latest_age_at_trigger_ms':(before-baseline['received_monotonic'])*1000 if baseline else None,
                        'scored_frames':sampler.status()['scored']}
                    decisions.append(result)
                    write_json(destination/'decisions.json',decisions)
                    print(json.dumps({'event':'decision','id':result['id'],'at':at,'phase':phase,
                        'latest_index':baseline['index'] if baseline else None,
                        'selected_indices':[i['index'] for i in selected],
                        'selector_ms':round(result['selector_wall_ms'],2)}),flush=True)

                receiver=asyncio.create_task(receive())
                try:
                    for at in decision_times:
                        await choose(at)
                    await asyncio.sleep(max(0,t0+duration-time.monotonic()))
                finally:
                    receiver.cancel()
                    await asyncio.gather(receiver,return_exceptions=True)
                    frames_file.close()
        finally:
            await sampler.stop(clear=True)
    write_json(destination/'capture-summary.json', {'completed_utc':stamp(),
        'frames':len(records),'decisions':len(decisions),'errors':errors,
        'duration_seconds':time.monotonic()-t0 if t0 else None,
        'captured_bytes':sum(item['bytes'] for item in records),
        'selector_source_unchanged':hashlib.sha256(source.read_bytes()).hexdigest()==protocol['production_selector_sha256']})
    print(json.dumps({'event':'recording_finished','frames':len(records),'decisions':len(decisions),'errors':errors}),flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console',default='http://127.0.0.1:8765')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--protocol',choices=('mixed-75','single-30'),default='mixed-75')
    args=parser.parse_args()
    if not args.output.resolve().is_relative_to((ROOT/'data').resolve()):
        parser.error('output must be under this project data directory')
    options = {} if args.protocol=='mixed-75' else {
        'duration':30,'decision_times':[4,7,9,11,14,17,19,21,24,27,29],
        'phases':[(0,10,'single_object_stable'),(10,20,'single_object_motion'),
                  (20,30,'single_object_stable_after_motion')]}
    asyncio.run(capture(args.output.resolve(),args.console.rstrip('/'),**options))

if __name__=='__main__':
    main()
