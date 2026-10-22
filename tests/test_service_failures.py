"""모델 장애와 지연이 HTTP/SSE 사용 흐름을 막는 회귀를 검사한다."""
import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

with patch('dotenv.load_dotenv'):
    import server

from agent.graph import route_node
from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.ollama_llm import OllamaQualityLLM
from agent.openai_llm import OpenAIQualityLLM, OpenAIFastLLM
from agent.session_store import SqliteSessionStore


class ServiceFailureTests(unittest.TestCase):
    def test_model_failure_ends_request_without_saving_a_success(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            'LLM_BACKEND': 'fake', 'SUPERVISOR_VERSION': 'v2',
            'LANGCHAIN_TRACING_V2': 'false', 'LANGSMITH_TRACING': 'false',
        }):
            store = SqliteSessionStore(Path(directory) / 'session.db')
            with patch.object(server, 'SqliteSessionStore', return_value=store), \
                    TestClient(server.app, raise_server_exceptions=False) as client:
                for failure, status, code in (
                    (httpx.ConnectError('private endpoint'), 503, 'dependency_unavailable'),
                    (httpx.ReadTimeout('private endpoint'), 504, 'dependency_timeout'),
                    (ValueError('private malformed response'), 500, 'request_failed'),
                ):
                    with self.subTest(code=code), patch.object(
                            server.app.state.deps.fast_llm, 'route', side_effect=failure):
                        response = client.post('/chat', json={'message': '안녕', 'thread_id': code})
                        self.assertEqual(response.status_code, status)
                        self.assertIn('application/json', response.headers['content-type'])
                        self.assertEqual(response.json()['error']['code'], code)
                        self.assertNotIn('private', response.text)
                        stream = client.get('/chat/stream', params={'message': '안녕', 'thread_id': code})
                        self.assertIn('data: [DONE]', stream.text)
                        events = [json.loads(line[6:]) for line in stream.text.splitlines()
                                  if line.startswith('data: {')]
                        self.assertEqual(events[-1]['type'], 'RequestFailed')
                        self.assertEqual(events[-1]['code'], code)
                        self.assertFalse(any(e['type'] == 'FinalAnswer' for e in events))
                        self.assertNotIn('private', stream.text)
                        self.assertEqual(store.load(code), [])
                self.assertEqual(client.post('/chat', json={'message': '안녕'}).status_code, 200)

    def test_failure_after_partial_tokens_is_not_a_final_answer(self):
        class BrokenGraph:
            async def astream(self, *args, **kwargs):
                yield 'custom', {'type': 'AnswerToken', 'delta': '미완성 답변'}
                raise httpx.ReadTimeout('private endpoint')

        with patch.object(server.app.state, 'graph', BrokenGraph(), create=True), \
                patch.object(server.app.state, 'deps', SupervisorDeps(None, None, None, SqliteSessionStore(Path(':memory:')), 'v2'), create=True):
            async def read():
                response = await server.chat_stream('질문', 'partial')
                return ''.join([chunk async for chunk in response.body_iterator])
            try:
                text = asyncio.run(read())
                self.assertIn('RequestFailed', text)
                self.assertNotIn('FinalAnswer', text)
                self.assertIn('[DONE]', text)
                self.assertEqual(server.app.state.deps.session_store.load('partial'), [])
            finally:
                server.app.state.deps.session_store.close()


class ResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def assert_responsive(self, operation, blocking_result, target, method):
        order = []
        def blocking_call(*args, **kwargs):
            time.sleep(0.15)
            order.append('model_done')
            return blocking_result
        async def heartbeat():
            await asyncio.sleep(0.02)
            order.append('heartbeat')
        with patch.object(target, method, side_effect=blocking_call):
            await asyncio.gather(operation(), heartbeat())
        self.assertEqual(order, ['heartbeat', 'model_done'])

    async def test_route_keeps_event_loop_available(self):
        fast = FastLLM('fast', 0)
        deps = SupervisorDeps(fast, QualityLLM('quality', 0), None, None, 'v2')
        result = fast.route('안녕')
        await self.assert_responsive(lambda: route_node({'query': '안녕', 'events': []}, deps),
                                     result, fast, 'route')

    async def test_writing_keeps_event_loop_available(self):
        for model_type in (OllamaQualityLLM, OpenAIQualityLLM):
            model = model_type('test', 0)
            with self.subTest(model=model_type.__name__):
                await self.assert_responsive(lambda: model.write('질문', [], 1, None),
                    ('{"hypotheses":[],"actions":[],"limitations":[]}', {'usd': 0}), model, '_chat')

    async def test_openai_chat_keeps_event_loop_available(self):
        model = OpenAIFastLLM('test', 0)
        await self.assert_responsive(lambda: model.chat('안녕', []), ('안녕하세요', {'usd': 0}), model, '_chat')
