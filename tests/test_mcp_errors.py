import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.mcp_client import MCPClient


class MCPResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_error_flag_cannot_be_accepted_as_valid_data(self):
        client = MCPClient(flaky_first_tool_call=False)
        for flag in ("isError", "is_error"):
            response = SimpleNamespace(content=[SimpleNamespace(text='{"matches": []}')], **{flag: True})
            client.sessions["ops"] = SimpleNamespace(call_tool=AsyncMock(return_value=response))
            with self.subTest(flag=flag), self.assertRaises(RuntimeError):
                await client.call_tool("search_runbooks", {"query": "CPU"})

    async def test_non_object_tool_response_is_rejected(self):
        client = MCPClient(flaky_first_tool_call=False)
        for text in ("[]", "null", '"hello"'):
            client.sessions["ops"] = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
                content=[SimpleNamespace(text=text)], isError=False)))
            with self.subTest(text=text), self.assertRaises(ValueError):
                await client.call_tool("get_monitoring_status", {"system_name": "auth-service"})
