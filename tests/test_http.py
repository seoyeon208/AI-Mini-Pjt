"""실제 MCP subprocess + 임시 SQLite로 HTTP/SSE 연결을 검증한다. 외부 LLM은 사용하지 않는다."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

with patch("dotenv.load_dotenv"):
    import server

from agent.session_store import SqliteSessionStore


class HttpRoutingTests(unittest.TestCase):
    def test_http_and_sse_with_real_mcp(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "LLM_BACKEND": "fake", "SUPERVISOR_VERSION": "v2",
            "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false",
        }):
            store = SqliteSessionStore(Path(directory) / "session.db")
            with patch.object(server, "SqliteSessionStore", return_value=store), TestClient(server.app) as client:
                health = client.get("/health").json()
                self.assertIn("auth-service", health["systems"])
                self.assertEqual(len(health["systems"]), len(set(health["systems"])))
                for query, route, tier in (
                    ("안녕", "chat", "fast"),
                    ("auth-service CPU 알려줘", "lookup", "source"),
                    ("payments-service CPU 급증과 gateway-service timeout 공통 원인 분석", "incident", "quality"),
                ):
                    with self.subTest(route=route):
                        response = client.post("/chat", json={"message": query, "thread_id": route})
                        self.assertEqual(response.status_code, 200)
                        body = response.json()
                        self.assertEqual(body["metadata"]["route"], route)
                        self.assertEqual(body["metadata"]["response_model"], "source-template" if tier == "source" else f"{tier}-model-stub")
                        self.assertTrue(body["reply"])
                        if route == "incident":
                            self.assertEqual({o["system_name"] for o in body["metadata"]["report"]["observations"]},
                                             {"payments-service", "gateway-service"})
                            self.assertTrue(body["metadata"]["report"]["actions"])
                            self.assertTrue({"cpu-spike", "network-timeout"} <= {d["id"] for d in body["metadata"]["sources"]})
                            self.assertEqual(body["metadata"]["validation_status"], "passed")
                            self.assertEqual(body["metadata"]["evidence_gaps"], [])
                response = client.get("/chat/stream", params={"message": "CPU 급증 대응 매뉴얼 찾아줘"})
                events = [json.loads(line[6:]) for line in response.text.splitlines()
                          if line.startswith("data: ") and line != "data: [DONE]"]
                self.assertEqual(events[0]["route"], "lookup")
                self.assertEqual(events[0]["response_model"], "source-template")
                self.assertTrue(any(e["type"] == "AnswerToken" for e in events))
                self.assertEqual(events[-1]["type"], "FinalAnswer")
                self.assertIn("CPU", events[-1]["reply"])
                self.assertEqual(events[-1]["metadata"]["validation_status"], "passed")
                self.assertTrue(events[-1]["metadata"]["sources"])
                self.assertEqual(events[-1]["metadata"]["report"]["observations"], [])
                self.assertTrue(events[-1]["metadata"]["report"]["actions"])
                validation_at = next(i for i, e in enumerate(events) if e["type"] == "Validation")
                answer_at = next(i for i, e in enumerate(events) if e["type"] == "AnswerToken")
                self.assertLess(validation_at, answer_at)
                self.assertIn("data: [DONE]", response.text)

                original_tool = server.app.state.deps.mcp.call_tool
                async def offline_monitor(name, arguments):
                    if name == "get_monitoring_status" and arguments["system_name"] == "auth-service":
                        raise ConnectionError("internal connection details")
                    return await original_tool(name, arguments)
                with patch.object(server.app.state.deps.mcp, "call_tool", side_effect=offline_monitor):
                    query = "auth-service와 web-01 CPU 장애 원인 분석"
                    body = client.post("/chat", json={"message": query}).json()
                    self.assertEqual(body["metadata"]["validation_status"], "partial")
                    self.assertEqual(body["metadata"]["research_failures"][0]["target"], "auth-service")
                    self.assertIn("web-01: 상태=", body["reply"])
                    self.assertNotIn("auth-service: 상태=", body["reply"])
                    self.assertEqual([o["system_name"] for o in body["metadata"]["report"]["observations"]], ["web-01"])
                    self.assertTrue(body["metadata"]["report"]["limitations"])
                    response = client.get("/chat/stream", params={"message": query})
                    events = [json.loads(line[6:]) for line in response.text.splitlines()
                              if line.startswith("data: ") and line != "data: [DONE]"]
                    self.assertTrue(any(e["type"] == "TaskFailed" for e in events))
                    self.assertEqual(events[-1]["metadata"]["validation_status"], "partial")
                    self.assertTrue(events[-1]["metadata"]["research_failures"])
                    self.assertNotIn("internal connection details", response.text)
                    self.assertIn("data: [DONE]", response.text)

                response = client.post("/chat", json={"message": "billing-service가 느려요"})
                body = response.json()
                self.assertEqual(response.status_code, 200)
                self.assertIn("billing-service", body["metadata"]["gate_blocked_reason"])
                self.assertIn("등록되지 않은", body["reply"])
                self.assertFalse(any(e["type"] in ("PlanCreated", "TaskCompleted") for e in body["events"]))

                response = client.get("/chat/stream", params={"message": "원인 분석해줘"})
                events = [json.loads(line[6:]) for line in response.text.splitlines()
                          if line.startswith("data: ") and line != "data: [DONE]"]
                gate = next(e for e in events if e["type"] == "GateApplied")
                self.assertTrue(gate["blocked_reason"])
                self.assertEqual(events[-1]["type"], "FinalAnswer")
                self.assertIn("시스템명", events[-1]["reply"])
                self.assertFalse(any(e["type"] == "TaskCompleted" for e in events))


if __name__ == "__main__":
    unittest.main()
