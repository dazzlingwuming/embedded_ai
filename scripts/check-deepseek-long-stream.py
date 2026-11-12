"""Direct, text-only paid API checks; public-domain recital, no board/ASR/TTS."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))

from pc_backend.ai_client import (AIError, capture_stream_events, settings,
                                 stream_reply)


def timing(events):
    deltas=[event for event in events if event['event']=='delta_received']
    finished=[event for event in events if event['event']=='request_finished']
    return {'first_delta_received_ms':deltas[0]['elapsed_ms'] if deltas else None,
            'last_delta_received_ms':deltas[-1]['elapsed_ms'] if deltas else None,
            'finish_received_ms':finished[-1]['elapsed_ms'] if finished else None,
            'finish_reasons':[event['finish_reason'] for event in finished],
            'request_count':sum(event['event']=='request_started' for event in events)}


async def main():
    report={'checked_utc':datetime.now(timezone.utc).isoformat(),'passed':False,
            'cloud_api_used':True,'direct_networking':True,
            'asr_used':False,'tts_used':False,'board_used':False,
            'input_origin':'explicit_public_domain_recital_prompt',
            'official_references':{
                'chat_completions':'https://api-docs.deepseek.com/api/create-chat-completion/',
                'prefix_completion':'https://api-docs.deepseek.com/guides/chat_prefix_completion/'},
            'timestamp_scope':'application receives SSE deltas; not model generation-completion timestamps'}
    target=ROOT/'data'/'deepseek-long-stream-check.json'
    try:
        config=settings()
        if config['proxy'] is not None:raise AIError('此验证需要项目 Direct 配置')
        report.update(model=config['model'],max_tokens=config['max_tokens'])
        probe_config={**config,'max_tokens':8,'max_continuations':3}
        probe_prompt='Output exactly this sequence, preserving single spaces: 1 2 3 4 5 6 7 8 9 10'
        with patch('pc_backend.ai_client.settings',return_value=probe_config), capture_stream_events() as events:
            probe=''.join([delta async for delta in stream_reply(probe_prompt,system_prompt=
                'Return exactly the requested text, without introductions, Markdown, or explanations.')])
        probe_timing=timing(events)
        probe_passed=(' '.join(probe.split())=='1 2 3 4 5 6 7 8 9 10' and
                      probe_timing['finish_reasons'][0]=='length' and probe_timing['finish_reasons'][-1]=='stop')
        report['prefix_continuation_check']={'passed':probe_passed,'max_tokens_per_request':8,
            'output_text':probe,**probe_timing}
        if not probe_passed:raise AIError('真实小预算前缀续写验证未通过')

        prompt='完整朗读《滕王阁序》，从“豫章故郡”开始，到“各倾陆海云尔”结束。只输出原文，不加标题和解释，不省略任何一段。'
        instructions='用户明确要求全文时，输出完整原文，不要缩写或用省略号代替正文。只输出用户指定起止范围的公版原文，不附解释。'
        start=time.monotonic()
        with capture_stream_events() as events:
            chunks=[]
            async for delta in stream_reply(prompt,system_prompt=instructions):
                chunks.append(delta)
        text=''.join(chunks)
        normalized=''.join(char for char in text if char.isalnum())
        anchors=['豫章故郡','序属三秋','桂殿兰宫','落霞与孤鹜齐飞','渔舟唱晚',
                 '萍水相逢','时运不齐','老当益壮','三尺微命','各倾陆海云尔']
        positions=[normalized.find(anchor) for anchor in anchors]
        checks={'starts_with_expected':normalized.startswith(anchors[0]),
                'ends_with_expected':normalized.endswith(anchors[-1]),
                'substantial_full_text':len(normalized)>=700,
                'all_section_anchors_in_order':all(position>=0 for position in positions) and positions==sorted(positions),
                'no_ellipsis_placeholder':'……' not in text and '...' not in text,
                'provider_finished_normally':timing(events)['finish_reasons'][-1]=='stop'}
        report.update(prompt=prompt,output_text=text,expected_start=anchors[0],expected_end=anchors[-1],
            characters=len(text),normalized_characters=len(normalized),completeness_checks=checks,
            completeness_check_scope='specified start/end and section coverage; not a critical-edition word-for-word collation',
            section_anchor_positions=dict(zip(anchors,positions)),elapsed_seconds=round(time.monotonic()-start,3),
            stream_events=events,**timing(events),passed=all(checks.values()))
    except Exception as error:
        report['error']=str(error) if isinstance(error,AIError) else type(error).__name__
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report.get(key) for key in ('passed','model','max_tokens','characters',
        'normalized_characters','first_delta_received_ms','last_delta_received_ms',
        'finish_reasons','completeness_checks','error')},ensure_ascii=True),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(asyncio.run(main()))
