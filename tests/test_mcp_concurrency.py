import asyncio
from pathlib import Path
import unittest

from agent.deps import SupervisorDeps
from agent.graph import research_node
from agent.mcp_client import MCPClient

SERVER = Path(__file__).parent / 'fixtures' / 'concurrent_mcp.py'


class MCPConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    # MCP context는 연 태스크에서 닫아야 하므로 각 테스트 안에서 start/stop한다.
    async def test_real_stdio_calls_overlap_and_keep_their_own_responses(self):
        client = MCPClient(flaky_first_tool_call=False, server_scripts={'ops': SERVER})
        await client.start()
        try:
            names = ['auth-service', 'payments-service', 'gateway-service']
            results = await asyncio.gather(*(client.call_tool('get_monitoring_status', {'system_name': name})
                                             for name in names))
            self.assertEqual([r['system_name'] for r in results], names)
            self.assertEqual([r['cpu_percent'] for r in results], [20, 30, 40])
            self.assertLess(max(r['started'] for r in results), min(r['finished'] for r in results))
            self.assertLess(results[1]['finished'], results[0]['finished'])
        finally:
            await client.stop()

    async def test_research_keeps_successful_parallel_results_when_one_tool_fails(self):
        client = MCPClient(flaky_first_tool_call=False, server_scripts={'ops': SERVER})
        await client.start()
        try:
            names = ['auth-service', 'cache-service', 'payments-service']
            state = {'query': '상태 확인', 'events': [], 'tasks': [
                {'id': name, 'agent': 'research', 'kind': 'monitoring', 'target': name, 'status': 'pending'}
                for name in names]}
            result = await research_node(state, SupervisorDeps(None, None, client, None, 'v2'))
            records = {c['data']['system_name']: c['data'] for c in result['collected']}
            self.assertEqual(set(records), {'auth-service', 'payments-service'})
            self.assertLess(max(r['started'] for r in records.values()), min(r['finished'] for r in records.values()))
            self.assertEqual([(f['target'], f['code'], f['attempts']) for f in result['research_failures']],
                             [('cache-service', 'tool_error', 1)])
            self.assertEqual({t['target']: t['status'] for t in result['tasks']},
                             {'auth-service': 'done', 'cache-service': 'failed', 'payments-service': 'done'})
        finally:
            await client.stop()

    async def test_cancelling_one_call_does_not_break_another_or_the_session(self):
        client = MCPClient(flaky_first_tool_call=False, server_scripts={'ops': SERVER})
        await client.start()
        try:
            async def cancelled():
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(0.03):
                        await client.call_tool('get_monitoring_status', {'system_name': 'auth-service'})
            _, healthy = await asyncio.gather(cancelled(), client.call_tool(
                'get_monitoring_status', {'system_name': 'payments-service'}))
            self.assertEqual(healthy['system_name'], 'payments-service')
            again = await client.call_tool('get_monitoring_status', {'system_name': 'gateway-service'})
            self.assertEqual(again['cpu_percent'], 40)
        finally:
            await client.stop()
