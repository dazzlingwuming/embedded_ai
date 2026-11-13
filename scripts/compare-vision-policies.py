"""Paired, opt-in evaluation of two frozen real-camera frame policies.

prepare: freeze all captured decision points and blind image IDs for annotation.
run: use identical prompts/model, independent turns, randomized paired order,
     bounded calls and no retries. Production code/settings are never changed.
summarize: score against annotations sealed before the first request.
"""
import argparse
import asyncio
import base64
from contextlib import aclosing
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from aiohttp import ClientSession, ClientTimeout
from PIL import Image, ImageDraw, ImageFont
from pc_backend.ai_client import capture_stream_events, settings, stream_reply

OBJECTS={'screen','keyboard','cup','other','unclear'}
STATES={'light_page','dark_page','icon_grid','obstructed','unclear','not_applicable'}
POLICIES=('latest','quality')
PROMPT=(
    '只根据本轮图片判断画面中央的主要实体和屏幕内容，不根据旧对话或物体常识补全。'
    '只返回严格 JSON，两个字段：'
    'object 从 screen、keyboard、cup、other、unclear 中选择。'
    'screen 指实际拍摄的电脑屏幕，即使屏幕里显示其他东西仍算 screen；'
    'cup 包括带把马克杯、杯状容器。实体无法辨认选 unclear。'
    'screen_state：object 是 screen 时，从 light_page（主要为浅底页面）、'
    'dark_page（主要为深底页面或文本）、icon_grid（成组大图标或快捷入口，可能有壁纸）、'
    'obstructed（屏幕主体被近处物体遮挡，当前内容无法辨认）、unclear 中选择；'
    'object 不是 screen 时必须填 not_applicable。不要解释，不要输出其他字段。')
SYSTEM='你是图像观察助手。仅根据实际图片回答；没有图片或看不清时明确选择 unclear。'

def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def local(path):
    value=Path(path).resolve()
    if not value.is_relative_to((ROOT/'data').resolve()):
        raise ValueError('Experimental files must be under this project data directory')
    return value

def parse_prediction(value):
    text=value.strip()
    if text.startswith('```') and text.endswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    answer=json.loads(text)
    if not isinstance(answer,dict) or set(answer)!={'object','screen_state'}:
        raise ValueError('Response must have exactly object and screen_state')
    if answer['object'] not in OBJECTS or answer['screen_state'] not in STATES:
        raise ValueError('Unrecognized classification value')
    if answer['object']!='screen' and answer['screen_state']!='not_applicable':
        raise ValueError('Non-screen object has a screen state')
    if answer['object']=='screen' and answer['screen_state']=='not_applicable':
        raise ValueError('Screen object must have an applicable screen state')
    return answer

def summarize_numbers(values):
    values=sorted(v for v in values if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v))
    if not values:
        return {'n':0,'mean':None,'median':None,'p95':None,'min':None,'max':None}
    return {'n':len(values),'mean':round(statistics.mean(values),3),
        'median':round(statistics.median(values),3),'p95':round(values[math.ceil(.95*len(values))-1],3),
        'min':round(values[0],3),'max':round(values[-1],3)}

def prepare(datasets,destination):
    destination.mkdir(parents=True,exist_ok=False)
    cases=[]
    manifest=[]
    source=ROOT/'pc_backend/vision_frames.py'
    for group,dataset in enumerate(datasets,1):
        protocol=read(dataset/'protocol.json')
        summary=read(dataset/'capture-summary.json')
        if summary['errors'] or not summary['selector_source_unchanged']:
            raise ValueError('Capture failed or selector changed')
        if protocol['production_selector_sha256']!=digest(source):
            raise ValueError('Selector differs from the captured experiment')
        decisions=read(dataset/'decisions.json')
        unique={f['sha256']:f for d in decisions for f in ([d['latest']]+d['selected']) if f}
        frames=list(unique.values())
        random.Random(20261113+group).shuffle(frames)
        mapping={}
        for index,frame in enumerate(frames,1):
            path=dataset/frame['path']
            if digest(path)!=frame['sha256']:
                raise ValueError('Captured frame bytes changed')
            name=f'g{group}-b{index:02d}'
            mapping[frame['sha256']]=name
            manifest.append({'image_id':name,'path':path.relative_to(ROOT).as_posix(),
                'sha256':frame['sha256']})
        for decision in decisions:
            if not decision['latest'] or len(decision['selected'])!=1:
                raise ValueError('This paired image experiment requires one image for each policy')
            def record(frame):
                return {**frame,'image_id':mapping[frame['sha256']],
                    'path':(dataset/frame['path']).relative_to(ROOT).as_posix()}
            cases.append({'case_id':f'g{group}-{decision["id"]}',
                'dataset':dataset.relative_to(ROOT).as_posix(),
                'trigger_elapsed_seconds':decision['actual_elapsed_seconds'],
                'selector_ms':decision['selector_wall_ms'],
                'latest_age_at_trigger_ms':decision['latest_age_at_trigger_ms'],
                'latest':record(decision['latest']),'quality':record(decision['selected'][0]),
                'selection_reason':decision['selection']['reason'],
                'selection_warnings':decision['selection']['warnings']})
        group_images=[item for item in manifest if item['image_id'].startswith(f'g{group}-')]
        font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',19)
        for page in range(math.ceil(len(group_images)/12)):
            with Image.new('RGB',(960,1440),'white') as sheet:
                draw=ImageDraw.Draw(sheet)
                for position,item in enumerate(group_images[page*12:(page+1)*12]):
                    x=position%3*320; y=position//3*360
                    with Image.open(ROOT/item['path']) as image:
                        sheet.paste(image.resize((320,320)),(x,y+36))
                    draw.text((x+6,y+6),item['image_id'],fill='black',font=font)
                sheet.save(destination/f'review-g{group}-{page+1:02d}.jpg',quality=93)
    save(destination/'cases.json',cases)
    save(destination/'image-manifest.json',manifest)
    save(destination/'annotation-template.json',{'reviewer':'assistant_visual_review_before_cloud',
        'blinded_to_policy':True,'annotations':[{'image_id':item['image_id'],
            'object':None,'screen_state':None,'notes':''} for item in manifest]})
    save(destination/'experiment.json',{'prepared_utc':utc(),'selector_sha256':digest(source),
        'case_count':len(cases),'distinct_images':len(manifest),'prompt':PROMPT,'system':SYSTEM,
        'ground_truth_status':'awaiting_blinded_visual_annotation',
        'design':'all prescheduled decision points, live selector on the same relay frames',
        'input_images':'original JPEGs, no synthetic perturbations or re-encoding',
        'latency_scope':'selector waiting measured live; model latency measured later on frozen images',
        'token_scope':'actual API usage for full requests; no assumed per-image token formula',
        'no_production_settings_changes':True})
    print(json.dumps({'prepared':True,'cases':len(cases),'images':len(manifest)}))

def annotations(destination):
    data=read(destination/'annotations.json')
    if data.get('blinded_to_policy') is not True:
        raise ValueError('Require policy-blind annotations before any model call')
    result={item['image_id']:item for item in data['annotations']}
    manifest=read(destination/'image-manifest.json')
    if set(result)!={item['image_id'] for item in manifest}:
        raise ValueError('Annotations must cover exactly the frozen image manifest')
    for item in result.values():
        parse_prediction(json.dumps({key:item[key] for key in ('object','screen_state')}))
    return result

def schedule(cases,repeats):
    rng=random.Random(20261113)
    result=[]
    for repeat in range(repeats):
        order=list(range(len(cases))); rng.shuffle(order)
        for index in order:
            pair=POLICIES if (index+repeat)%2==0 else tuple(reversed(POLICIES))
            for position,policy in enumerate(pair):
                result.append({'trial_id':f'r{repeat+1}-{cases[index]["case_id"]}-{policy}',
                    'repeat':repeat+1,'case_id':cases[index]['case_id'],
                    'policy':policy,'pair_position':position+1})
    return result

async def evaluate(destination,repeats,limit):
    labels=annotations(destination)
    cases=read(destination/'cases.json')
    lookup={case['case_id']:case for case in cases}
    jobs=schedule(cases,repeats)
    if len(jobs)>limit:
        raise ValueError(f'Planned {len(jobs)} requests exceeds explicit {limit} call limit')
    if (destination/'trials.jsonl').exists():
        raise ValueError('Trial file exists; this command never repeats or overwrites cloud calls')
    config=settings()
    if not config['key'] or config['proxy']:
        raise ValueError('Require configured DeepSeek key and Direct connection')
    if digest(ROOT/'pc_backend/vision_frames.py')!=read(destination/'experiment.json')['selector_sha256']:
        raise ValueError('Production selector changed since capture')
    for item in read(destination/'image-manifest.json'):
        if digest(ROOT/item['path'])!=item['sha256']:
            raise ValueError('Image bytes differ from the sealed dataset')
    # No transcript, microphone or speaker is acquired by this benchmark.
    async with ClientSession(trust_env=False,timeout=ClientTimeout(total=5)) as client:
        async with client.get('http://127.0.0.1:8765/api/status') as response:
            status=await response.json()
        if status.get('voice',{}).get('active') or status.get('recording'):
            raise ValueError('User audio session active; run comparison later')
    plan={'sealed_utc':utc(),'annotations_sha256':digest(destination/'annotations.json'),
        'cases_sha256':digest(destination/'cases.json'),'repeats':repeats,
        'request_limit':limit,'planned_requests':len(jobs),'jobs':jobs,
        'model_alias':config['model'],'output_token_limit':128,
        'history':False,'thinking':'disabled','concurrency':1,'retries':0,
        'network':'Direct','system':SYSTEM,'prompt':PROMPT}
    save(destination/'run-plan.json',plan)
    bounded={**config,'max_tokens':128,'max_continuations':0,'read_timeout_seconds':30}
    with (destination/'trials.jsonl').open('x',encoding='utf-8') as output:
        for number,job in enumerate(jobs,1):
            case=lookup[job['case_id']]
            frame=case[job['policy']]
            raw=(ROOT/frame['path']).read_bytes()
            result={**job,'image_id':frame['image_id'],'image_sha256':frame['sha256'],
                'image_bytes':len(raw),'started_utc':utc(),'error':None,
                'selector_ms':case['selector_ms'] if job['policy']=='quality' else 0.0}
            # Identical opaque frame ID for both arms; actual receive time is
            # retained. No policy, expected label or quality enters the prompt.
            image={'jpeg':raw,'frame_id':'benchmark-observation',
                'received_utc':frame['received_utc'],'quality':{}}
            pieces=[]
            first=None
            events=[]
            started=time.perf_counter()
            try:
                with patch('pc_backend.ai_client.settings',return_value=bounded),capture_stream_events() as events:
                    async with aclosing(stream_reply(PROMPT,[],SYSTEM,images=[image])) as reply:
                        async for part in reply:
                            if first is None:
                                first=(time.perf_counter()-started)*1000
                            pieces.append(part)
                result['prediction']=parse_prediction(''.join(pieces))
            except Exception as error:
                result['error']=type(error).__name__+': '+str(error)[:300]
            result['first_token_ms']=first
            result['model_total_ms']=(time.perf_counter()-started)*1000
            result['output_text']=''.join(pieces)
            result['request_events']=[event for event in events if event['event']!='delta_received']
            result['request_count']=sum(e['event']=='request_started' for e in events)
            finished=[event for event in events if event['event']=='request_finished']
            result['usage']=finished[-1].get('usage') if finished else None
            result['response_model']=finished[-1].get('response_model') if finished else None
            result['finish_reason']=finished[-1].get('finish_reason') if finished else None
            result['constructed_selection_plus_first_token_ms']=(result['selector_ms']+first) if first is not None else None
            output.write(json.dumps(result,ensure_ascii=False)+'\n'); output.flush()
            print(json.dumps({'completed':number,'planned':len(jobs),'trial_id':job['trial_id'],
                'first_token_ms':round(first,1) if first is not None else None,
                'usage':result['usage'],'error':result['error']}),flush=True)
    summarize(destination)

def summarize(destination):
    plan=read(destination/'run-plan.json')
    if digest(destination/'annotations.json')!=plan['annotations_sha256'] or digest(destination/'cases.json')!=plan['cases_sha256']:
        raise ValueError('Labels or cases changed after cloud evaluation started')
    labels=annotations(destination)
    cases=read(destination/'cases.json')
    lookup={case['case_id']:case for case in cases}
    trials=[json.loads(line) for line in (destination/'trials.jsonl').read_text(encoding='utf-8').splitlines() if line]
    if len({trial['trial_id'] for trial in trials})!=len(trials):
        raise ValueError('Duplicate cloud trial')
    for trial in trials:
        expected=labels[trial['image_id']]
        current=labels[lookup[trial['case_id']]['latest']['image_id']]
        prediction=trial.get('prediction',{})
        trial['object_correct']=not trial['error'] and prediction.get('object')==expected['object']
        trial['frame_joint_correct']=not trial['error'] and all(prediction.get(key)==expected[key] for key in ('object','screen_state'))
        trial['trigger_joint_correct']=not trial['error'] and all(prediction.get(key)==current[key] for key in ('object','screen_state'))
        trial['screen_state_applicable']=expected['object']=='screen'
        trial['screen_state_correct']=trial['screen_state_applicable'] and not trial['error'] and prediction.get('screen_state')==expected['screen_state']
    stats={}
    for policy in POLICIES:
        rows=[trial for trial in trials if trial['policy']==policy]
        screen=[trial for trial in rows if trial['screen_state_applicable']]
        def accuracy(key,items=rows):
            correct=sum(bool(item[key]) for item in items)
            return {'correct':correct,'n':len(items),'percent':round(100*correct/len(items),2) if items else None}
        usage_rows=[row for row in rows if row['usage']]
        stats[policy]={'completed_trials':len(rows),'errors':sum(bool(row['error']) for row in rows),
            'object_accuracy':accuracy('object_correct'),'frame_joint_accuracy':accuracy('frame_joint_correct'),
            'trigger_joint_accuracy':accuracy('trigger_joint_correct'),
            'screen_state_accuracy':accuracy('screen_state_correct',screen),
            'selector_ms':summarize_numbers([row['selector_ms'] for row in rows]),
            'first_token_ms':summarize_numbers([row['first_token_ms'] for row in rows]),
            'model_total_ms':summarize_numbers([row['model_total_ms'] for row in rows]),
            'constructed_selection_plus_first_token_ms':summarize_numbers([row['constructed_selection_plus_first_token_ms'] for row in rows]),
            'actual_request_count':sum(row['request_count'] for row in rows),
            'usage_reported_trials':len(usage_rows),
            'tokens':{key:{'sum':sum(row['usage'].get(key,0) for row in usage_rows),
                **summarize_numbers([row['usage'].get(key) for row in usage_rows])}
                for key in ('prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens')}}
    def rate_of_disagreement():
        wrong=0
        for case in cases:
            a,b=(labels[case[p]['image_id']] for p in POLICIES)
            wrong+=any(a[k]!=b[k] for k in ('object','screen_state'))
        return {'count':wrong,'n':len(cases)}
    paired=[]
    for repeat in range(1,plan['repeats']+1):
        for case in cases:
            rows={r['policy']:r for r in trials if r['repeat']==repeat and r['case_id']==case['case_id']}
            if set(rows)!=set(POLICIES):
                continue
            paired.append({'case_id':case['case_id'],'repeat':repeat,
                'latest_correct':bool(rows['latest']['trigger_joint_correct']),
                'quality_correct':bool(rows['quality']['trigger_joint_correct']),
                'first_token_difference_ms':(rows['quality']['first_token_ms']-rows['latest']['first_token_ms'])
                    if all(row['first_token_ms'] is not None for row in rows.values()) else None})
    unambiguous_ids={case['case_id'] for case in cases
        if not any(labels[case[p]['image_id']].get('ambiguous',False) for p in POLICIES)}
    stable_labels={policy:[row for row in trials if row['policy']==policy and row['case_id'] in unambiguous_ids]
                   for policy in POLICIES}
    report={'summarized_utc':utc(),'complete':len(trials)==plan['planned_requests'],
        'scope':'pilot on correlated real-camera screen/keyboard/cup recordings; not a universal vision benchmark',
        'case_count':len(cases),'repeats':plan['repeats'],'completed_requests':sum(row['request_count'] for row in trials),
        'model_alias':plan['model_alias'],'response_models':sorted({row['response_model'] for row in trials if row['response_model']}),
        'production_selector_unchanged':digest(ROOT/'pc_backend/vision_frames.py')==read(destination/'experiment.json')['selector_sha256'],
        'labels_sealed_before_cloud':True,'annotation_reviewer':read(destination/'annotations.json')['reviewer'],
        'statistics':stats,'paired_current_scene':{
            'both_correct':sum(p['latest_correct'] and p['quality_correct'] for p in paired),
            'latest_only_correct':sum(p['latest_correct'] and not p['quality_correct'] for p in paired),
            'quality_only_correct':sum(not p['latest_correct'] and p['quality_correct'] for p in paired),
            'both_wrong':sum(not p['latest_correct'] and not p['quality_correct'] for p in paired),
            'quality_minus_latest_first_token_ms':summarize_numbers([p['first_token_difference_ms'] for p in paired])},
        'policy_frame_label_disagreement':rate_of_disagreement(),
        'annotation_ambiguous_images':[name for name,item in labels.items() if item.get('ambiguous',False)],
        'common_unambiguous_case_count':len(unambiguous_ids),
        'common_unambiguous_trigger_accuracy':{policy:{'correct':sum(bool(row['trigger_joint_correct']) for row in rows),
            'n':len(rows),'percent':round(100*sum(bool(row['trigger_joint_correct']) for row in rows)/len(rows),2) if rows else None}
            for policy,rows in stable_labels.items()},
        'identical_frame_pairs':sum(case['latest']['sha256']==case['quality']['sha256'] for case in cases),
        'quality_selected_after_trigger_count':sum(case['quality']['received_monotonic']>case['latest']['received_monotonic']+case['latest_age_at_trigger_ms']/1000 for case in cases),
        'limitations':['Only a few continuous clips; adjacent samples and repeats are correlated',
            'Ground truth is assistant visual annotation, without independent human frame-by-frame review',
            'Main objects and screen layout only; OCR, exact label reading and general object accuracy untested',
            'First-token latency is for this bounded classification prompt; not speech end-to-end latency',
            'Selection plus first-token numbers are sums of separate measurements, not live end-to-end observations',
            'API prompt usage includes text, image and metadata; isolated image-token billing not available',
            'Cache-hit tokens are reported separately; total tokens are not monetary cost',
            'Production frame selection and thresholds remain unchanged']}
    save(destination/'summary.json',report)
    save(destination/'scored-trials.json',trials)
    save(destination/'paired-results.json',paired)
    headers=['strategy','object_accuracy','screen_state_accuracy','frame_joint_accuracy','trigger_joint_accuracy','median_selection_ms','median_first_token_ms','p95_first_token_ms','input_tokens','output_tokens','total_tokens']
    rows=[]
    for policy,s in stats.items():
        rows.append([policy,s['object_accuracy']['percent'],s['screen_state_accuracy']['percent'],s['frame_joint_accuracy']['percent'],s['trigger_joint_accuracy']['percent'],s['selector_ms']['median'],s['first_token_ms']['median'],s['first_token_ms']['p95'],s['tokens']['prompt_tokens']['sum'],s['tokens']['completion_tokens']['sum'],s['tokens']['total_tokens']['sum']])
    (destination/'summary.csv').write_text(','.join(headers)+'\n'+'\n'.join(','.join(map(str,row)) for row in rows)+'\n',encoding='utf-8-sig')
    render_report(destination,report,cases,trials)
    print(json.dumps({'complete':report['complete'],'cases':len(cases),'requests':report['completed_requests'],'statistics':stats,'paired':report['paired_current_scene']},ensure_ascii=True),flush=True)


def render_report(destination,report,cases,trials):
    """Portable local evidence document, with untouched JPEGs embedded inline."""
    def escape(value):
        return html.escape(str(value))
    def table(headings,rows):
        return '<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+escape(v)+'</th>' for v in headings)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    def accuracy(value):
        return f'{value["correct"]}/{value["n"]} ({value["percent"]}%)'
    statistics=report['statistics']
    rows=[]
    metrics=[
        ('主体识别',lambda s:accuracy(s['object_accuracy'])),
        ('屏幕状态识别（辅助）',lambda s:accuracy(s['screen_state_accuracy'])),
        ('主体与状态均符合提问时画面',lambda s:accuracy(s['trigger_joint_accuracy'])),
        ('选图附加等待：中位数 / P95',lambda s:f'{s["selector_ms"]["median"]} / {s["selector_ms"]["p95"]} ms'),
        ('模型首个文字增量：中位数 / P95',lambda s:f'{s["first_token_ms"]["median"]} / {s["first_token_ms"]["p95"]} ms'),
        ('每次请求输入 Token（图文合计）',lambda s:s['tokens']['prompt_tokens']['mean']),
        ('累计输入 Token',lambda s:s['tokens']['prompt_tokens']['sum']),
        ('其中缓存命中输入 Token',lambda s:s['tokens']['prompt_cache_hit_tokens']['sum']),
        ('累计输出 Token',lambda s:s['tokens']['completion_tokens']['sum']),
        ('累计总 Token',lambda s:s['tokens']['total_tokens']['sum']),
    ]
    for name,get in metrics:
        rows.append([name,*(get(statistics[p]) for p in POLICIES)])
    sections=[
        '<h1>同批实拍：最新帧与当前选帧策略</h1>',
        '<p>'+escape(f'{report["case_count"]} 个固定时点，每种策略重复 {report["repeats"]} 次，共 {report["completed_requests"]} 次真实 DeepSeek 图片请求。')+'</p>',
        '<p>只新增测试工具和本地记录，没有修改生产选帧算法、阈值、开发板固件或配置。两种策略使用同一路实拍原始 JPEG、相同提示词和模型别名；独立轮次、交替配对顺序、无重试。</p>',
        '<h2>完整样本结果</h2>',table(['指标','直接取最新帧','当前选帧算法'],rows),
        '<p>“最新帧 0 ms”表示没有等待评分，未单独测量读内存中帧引用的微小开销。模型延迟在采样结束后，使用固定图片测量；不是完整语音通话延迟。</p>',
        '<p>主体正确率是粗分类，不能代表标签文字读取、OCR 或复杂物体识别。输入 Token 包含提示词、图片和元数据，服务没有在本报告中单独给出图片 Token。总 Token 也不是人民币费用。</p>',
        '<h2>按实拍片段分组</h2>',
    ]
    grouped=[]
    for group in sorted({case['case_id'].split('-')[0] for case in cases}):
        row=[group]
        for policy in POLICIES:
            sample=[t for t in trials if t['case_id'].startswith(group+'-') and t['policy']==policy]
            row.append(f'{sum(bool(t["object_correct"]) for t in sample)}/{len(sample)}')
        grouped.append(row)
    sections.append(table(['片段（g1 屏幕；g2 键盘及切换；g3 杯状容器）','最新帧主体正确次数','当前算法主体正确次数'],grouped))
    sections.extend([
        '<h2>标注和样本边界</h2>',
        '<p>先对选中图片随机编号、隐藏策略身份，再由本助手目视查看图片及必要原图做标注；标注文件在第一笔云请求前封存 SHA256。标注尚未经过独立人工逐帧复核。</p>',
        '<p>两种策略都保留全部预定时点。三张中心遮挡或空白表面的图在测试前标记为类别边界有歧义；以下辅助统计从两边同时排除涉及这些图片的相同配对，不替代上面的完整样本结果。</p>',
        table(['共同无歧义子集：主体与状态均符合提问时画面','最新帧','当前算法'],[
            ['正确次数',*(accuracy(report['common_unambiguous_trigger_accuracy'][p]) for p in POLICIES)]]),
        '<p>深色导航页与图标网格的分类边界可能重叠，因此屏幕状态分类仅作辅助指标。相邻时点来自同一连续视频；重复调用不等于新增独立场景，不据此宣称统计显著或推导通用阈值。</p>',
        '<p>'+escape(f'当前算法有 {report["quality_selected_after_trigger_count"]}/{report["case_count"]} 次选到提问时点之后才收到的帧；这是等待评分期间继续收图的结果。另有 {report["policy_frame_label_disagreement"]["count"]} 组，两种策略给出的画面内容标签不同。')+'</p>',
    ])
    examples=[]
    for case in cases:
        a=[t for t in trials if t['case_id']==case['case_id'] and t['policy']=='latest']
        b=[t for t in trials if t['case_id']==case['case_id'] and t['policy']=='quality']
        if (a and b and not any(t['error'] for t in a+b)
                and all(not t['object_correct'] for t in a) and all(t['object_correct'] for t in b)):
            examples.append((case,a,b))
    if examples:
        sections.append('<h2>能看到收益的一组实拍（个例）</h2>')
        for case,a,b in examples[:2]:
            sections.append('<p>'+escape(f'{case["case_id"]}：最新帧两次均未识别主体，当前算法两次均识别主体；这只证明这个样本有收益。')+'</p><div class="pair">')
            for policy,tr in [('latest',a),('quality',b)]:
                frame=case[policy]
                raw=(ROOT/frame['path']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=frame['sha256']:
                    raise ValueError('Evidence JPEG changed')
                sections.append('<figure><figcaption>'+escape('直接取最新帧' if policy=='latest' else '当前选帧算法')+'</figcaption><img alt="实际发送的原始 JPEG" src="data:image/jpeg;base64,'+base64.b64encode(raw).decode('ascii')+'"><p>'+escape(json.dumps(tr[0].get('prediction'),ensure_ascii=False))+'</p><small>'+escape(frame['sha256'])+'</small></figure>')
            sections.append('</div>')
    timing=destination/'scoring-timing.json'
    if timing.exists():
        score=read(timing)
        sections.extend(['<h2>本地计算耗时</h2>','<p>'+escape(f'同批 {score["measurements"]} 次 JPEG 解码与评分测量：中位数 {score["median_ms"]:.3f} ms，P95 {score["p95_ms"]:.3f} ms；不含读磁盘和模型调用。')+'</p>',
            '<p>评分计算耗时与选图等待耗时需要分开：当前程序每半秒评分一次，并可能等待最新待评分帧，所以不能把约四分之一秒的等待解释成图像计算需要这么久。</p>'])
    sections.extend(['<details><summary>完整 JSON 统计与限制</summary><pre>'+escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre></details>'])
    style='body{font:16px/1.7 system-ui,sans-serif;margin:32px auto;padding:0 24px;max-width:1100px;color:#17333a;background:#fafcfc}h1{font-size:28px}h2{font-size:21px;margin-top:32px}table{width:100%;border-collapse:collapse}td,th{border:1px solid #b6c7c9;padding:9px;text-align:left}th{background:#e9f1f1}.table-wrap{overflow-x:auto}.pair{display:flex;gap:20px;flex-wrap:wrap}figure{margin:0;flex:1;min-width:260px}figure img{width:100%;height:auto}small{word-break:break-all}pre{white-space:pre-wrap;word-break:break-word;font-size:12px}'
    document='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>实拍选帧对比结果</title><style>'+style+'</style><body>'+''.join(sections)+'</body></html>'
    (destination/'report.html').write_text(document,encoding='utf-8')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare'); prep.add_argument('--dataset',action='append',required=True); prep.add_argument('--output',required=True)
    run=sub.add_parser('run'); run.add_argument('--output',required=True); run.add_argument('--run-cloud',action='store_true'); run.add_argument('--repeats',type=int,choices=(1,2,3),default=2); run.add_argument('--limit-requests',type=int,default=200)
    summary=sub.add_parser('summarize'); summary.add_argument('--output',required=True)
    args=parser.parse_args()
    destination=local(args.output)
    if args.command=='prepare':
        prepare([local(path) for path in args.dataset],destination)
    elif args.command=='run':
        if not args.run_cloud:
            parser.error('Cloud calls require explicit --run-cloud; nothing was sent')
        asyncio.run(evaluate(destination,args.repeats,args.limit_requests))
    else:
        summarize(destination)

if __name__=='__main__':
    main()
