import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pc_backend.ai_client import (AIError, capture_stream_events, settings,
                                 stream_reply)


def frame(content='', finish=None):
    return ('data: '+json.dumps({'choices':[{'delta':{'content':content},
            'finish_reason':finish}]},ensure_ascii=False)+'\n\n').encode('utf-8')


class Content:
    def __init__(self, frames):
        self.lines = iter(b''.join(frames).splitlines(keepends=True))

    async def readline(self):
        return next(self.lines,b'')


class Response:
    def __init__(self, frames, status=200):
        self.status, self.content, self.closed = status, Content(frames), False

    async def __aenter__(self): return self

    async def __aexit__(self,*args): self.closed=True


class Session:
    def __init__(self,responses):
        self.responses=iter(responses)
        self.requests=[]
        self.options=None
        self.closed=False

    def factory(self,**kwargs):
        self.options=kwargs
        return self

    async def __aenter__(self):return self

    async def __aexit__(self,*args):self.closed=True

    async def post(self,url,**kwargs):
        self.requests.append((url,kwargs))
        return next(self.responses)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config={'model':'deepseek-flash','key':'sk-test-not-a-real-secret',
                     'proxy':None,'max_tokens':8192,'max_continuations':3,
                     'read_timeout_seconds':.01}

    async def collect(self,session,**arguments):
        with patch('pc_backend.ai_client.settings',return_value=self.config), \
             patch('pc_backend.ai_client.ClientSession',session.factory):
            return ''.join([delta async for delta in stream_reply('请求',**arguments)])

    async def test_first_delta_is_yielded_without_waiting_for_the_rest_and_close_releases_response(self):
        response=Response([frame('首片'),frame('末片','stop')])
        session=Session([response])
        with patch('pc_backend.ai_client.settings',return_value=self.config), \
             patch('pc_backend.ai_client.ClientSession',session.factory):
            answer=stream_reply('请求')
            self.assertEqual(await asyncio.wait_for(anext(answer),.2),'首片')
            self.assertFalse(response.closed)
            await answer.aclose()
        self.assertTrue(response.closed and session.closed)
        self.assertEqual(len(session.requests),1)

    async def test_consumer_backpressure_does_not_consume_the_active_read_timeout(self):
        session=Session([Response([frame('首片'),frame('末片','stop')])])
        with patch('pc_backend.ai_client.settings',return_value=self.config), \
             patch('pc_backend.ai_client.ClientSession',session.factory):
            answer=stream_reply('请求')
            self.assertEqual(await anext(answer),'首片')
            await asyncio.sleep(.03)  # TTS/speaker consumer is paused past the read deadline.
            self.assertEqual(await anext(answer),'末片')
            with self.assertRaises(StopAsyncIteration):await anext(answer)
        self.assertIsNone(session.options['timeout'].total)
        self.assertIsNone(session.options['timeout'].sock_read)

    async def test_actual_stalled_network_read_times_out_without_retrying_partial_output(self):
        response=Response([])
        async def stuck():await asyncio.Event().wait()
        response.content.readline=stuck
        session=Session([response])
        with self.assertRaisesRegex(AIError,'读取超时'):
            await self.collect(session)
        self.assertTrue(response.closed and session.closed)
        self.assertEqual(len(session.requests),1)

    async def test_length_finish_continues_with_official_prefix_and_only_yields_new_text(self):
        session=Session([Response([frame('前半','length')]),Response([frame('后半','stop')])])
        with capture_stream_events() as events:
            self.assertEqual(await self.collect(session),'前半后半')
        self.assertEqual([event['finish_reason'] for event in events if event['event']=='request_finished'],['length','stop'])
        self.assertEqual(session.requests[1][0],'https://api.deepseek.com/beta/chat/completions')
        self.assertEqual(session.requests[1][1]['json']['messages'][-1],
                         {'role':'assistant','content':'前半','prefix':True})
        self.assertFalse(session.requests[1][1]['allow_redirects'])
        self.assertNotIn('sk-test',repr(events))

    async def test_empty_stop_after_a_full_prefix_is_a_valid_completion(self):
        session=Session([Response([frame('已经完整','length')]),Response([frame('','stop')])])
        self.assertEqual(await self.collect(session),'已经完整')

    async def test_exhausted_length_budget_is_explicitly_incomplete(self):
        self.config['max_continuations']=0
        session=Session([Response([frame('部分','length')])])
        with self.assertRaisesRegex(AIError,'尚未完整结束'):
            await self.collect(session)
        self.assertEqual(len(session.requests),1)

    async def test_eof_without_finish_reason_does_not_masquerade_as_complete(self):
        session=Session([Response([frame('部分'),b'data: [DONE]\n\n'])])
        with self.assertRaisesRegex(AIError,'可能不完整'):
            await self.collect(session)
        self.assertEqual(len(session.requests),1)

    async def test_aborted_or_unrecognized_finish_reasons_do_not_leak_vendor_values(self):
        for reason in ('aborted','sk-test-not-a-real-secret'):
            with self.subTest(reason=reason):
                session=Session([Response([frame('部分',reason)])])
                with self.assertRaises(AIError) as failure:await self.collect(session)
                self.assertNotIn('sk-test',str(failure.exception))

    async def test_system_and_heard_history_are_preserved_without_character_slicing(self):
        prompt='系统提示'*800
        history=[{'role':'assistant','content':'已经完整播出的长句。'*800}]
        session=Session([Response([frame('回答','stop')])])
        await self.collect(session,history=history,system_prompt=prompt)
        messages=session.requests[0][1]['json']['messages']
        self.assertEqual(messages[0]['content'],prompt)
        self.assertEqual(messages[1]['content'],history[0]['content'])

    async def test_sse_comments_crlf_and_multiline_data_are_decoded(self):
        multiline=(b': keepalive\r\n\r\nevent: message\r\n'
                   b'data: {"choices": [\r\n'
                   b'data: {"delta":{"content":"hello"},"finish_reason":"stop"}]}\r\n\r\n')
        session=Session([Response([multiline])])
        self.assertEqual(await self.collect(session),'hello')


class SettingsTests(unittest.TestCase):
    def test_default_output_budget_is_8192_and_larger_valid_config_is_not_silently_clamped(self):
        root=Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(dir=root/'.cache'/'tmp') as directory:
            temporary=Path(directory);(temporary/'local').mkdir()
            with patch('pc_backend.ai_client.ROOT',temporary):
                self.assertEqual(settings()['max_tokens'],8192)
                (temporary/'local'/'ai.json').write_text(json.dumps({'max_tokens':12000}))
                self.assertEqual(settings()['max_tokens'],12000)
                (temporary/'local'/'ai.json').write_text(json.dumps({'max_tokens':1000000}))
                with self.assertRaises(AIError):settings()


if __name__=='__main__':unittest.main()
