import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph, research_node
from agent.ollama_llm import OllamaFastLLM
from agent.openai_llm import OpenAIFastLLM


def decision(route, *, kind=None, target=None, analysis=False):
    return dict(route=route, needs_analysis=analysis, kind=kind,
                target=target, reason="테스트 분류")


class RoutingOutputTests(unittest.TestCase):
    def test_backends_accept_three_routes(self):
        for cls in (OllamaFastLLM, OpenAIFastLLM):
            model = cls("test", 0)
            for result in (decision("chat"), decision("lookup", kind="monitoring", target="auth-service"),
                           decision("incident", analysis=True)):
                with self.subTest(backend=cls.__name__, route=result["route"]):
                    with patch.object(model, "_chat", return_value=(json.dumps(result), {"usd": 0})):
                        actual, _ = model.route("요청")
                    self.assertEqual(actual, result)

    def test_invalid_or_contradictory_output_never_becomes_chat(self):
        invalid = ["not json", "[]", "{}", json.dumps(decision("unknown")),
                   json.dumps({**decision("chat"), "needs_analysis": "false"}),
                   json.dumps(decision("lookup", kind="monitoring", analysis=True))]
        for cls in (OllamaFastLLM, OpenAIFastLLM):
            model = cls("test", 0)
            for content in invalid:
                with self.subTest(backend=cls.__name__, content=content):
                    with patch.object(model, "_chat", return_value=(content, {"usd": 0})):
                        result, _ = model.route("현재 CPU 알려줘")
                    self.assertEqual(result["route"], "incident")
                    self.assertTrue(result["needs_analysis"])


class GraphRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fast = FastLLM("fast-test", 0)
        self.quality = QualityLLM("quality-test", 0)
        self.mcp = Mock()
        async def tool_result(name, arguments):
            if name == "search_runbooks":
                return {"matches": [{"id": "cpu-spike", "title": "CPU 급증", "score": 2.0,
                                     "uri": "runbook://cpu-spike", "excerpt": "CPU 확인"}]}
            return {"system_name": arguments["system_name"], "status": "degraded", "cpu_percent": 78,
                    "memory_percent": 61, "alerts": ["latency"], "last_checked": "2026-10-20T10:00:00"}
        self.mcp.call_tool = AsyncMock(side_effect=tool_result)
        self.mcp.find_resource_template = AsyncMock(return_value=("docs", "runbook://{incident_type}"))
        self.mcp.read_resource = AsyncMock(return_value="# CPU 급증\n1. CPU 확인\n2. 프로세스 확인")
        self.mcp.find_prompt = AsyncMock(return_value="docs")
        self.mcp.get_prompt = AsyncMock(return_value="진단 절차")
        self.graph = build_graph(SupervisorDeps(self.fast, self.quality, self.mcp, None, "v2"))

    async def run_route(self, result, query="auth-service CPU 알려줘"):
        with patch.object(self.fast, "route", return_value=(result, {"usd": 0})):
            return await self.graph.ainvoke({"query": query, "events": [], "cost_usd": 0})

    async def test_chat_preserves_memory_and_uses_no_tools(self):
        result = await self.graph.ainvoke({"query": "내 이름이 뭐야?", "events": [],
            "history": [{"role": "user", "content": "제 이름은 서연입니다"}]})
        self.assertIn("서연", result["final"])
        self.mcp.call_tool.assert_not_called()
        self.mcp.find_prompt.assert_not_called()
        self.assertEqual(result["response_model"], "fast-test")

    async def test_monitoring_lookup_skips_full_pipeline(self):
        with patch.object(self.fast, "plan", side_effect=AssertionError("lookup must not plan")), \
             patch.object(self.fast, "write", side_effect=AssertionError("facts need no generation")), \
             patch.object(self.fast, "validate", side_effect=AssertionError("exact facts need no LLM judge")):
            result = await self.run_route(decision("lookup", kind="monitoring", target="auth-service"))
        self.mcp.call_tool.assert_awaited_once_with("get_monitoring_status", {"system_name": "auth-service"})
        self.mcp.find_resource_template.assert_not_called()
        self.assertIn("78", result["final"])
        self.assertEqual(result["validation_status"], "passed")
        self.assertNotIn("replan_rounds", result)
        self.assertEqual(result["response_model"], "source-template")
        self.assertEqual(result["validation_method"], "source_exact")
        self.assertEqual(result["report"]["actions"], [])
        self.assertEqual(result["report"]["limitations"], [])

    async def test_runbook_lookup_searches_then_reads_resource(self):
        with patch.object(self.fast, "write", side_effect=AssertionError("manual steps must stay verbatim")), \
             patch.object(self.fast, "validate", side_effect=AssertionError("verbatim steps need source checks")):
            result = await self.run_route(decision("lookup", kind="runbook", target="cpu-spike"), "CPU 급증 매뉴얼 찾아줘")
        self.assertEqual([c.args[0] for c in self.mcp.call_tool.await_args_list], ["search_runbooks"])
        self.mcp.read_resource.assert_awaited_once_with("runbook://cpu-spike", server="docs")
        self.assertIn("프로세스 확인", result["final"])
        self.assertEqual(result["report"]["actions"], [
            {"text": "CPU 확인", "sources": ["cpu-spike"]},
            {"text": "프로세스 확인", "sources": ["cpu-spike"]}])
        self.assertEqual(result["report"]["limitations"], [])

    async def test_korean_particle_after_system_name_is_allowed(self):
        result = await self.run_route(decision("lookup", kind="monitoring", target="auth-service"), "auth-service의 CPU 알려줘")
        self.mcp.call_tool.assert_awaited_once()
        self.assertIn("78", result["final"])

    async def test_system_name_substring_does_not_select_another_system(self):
        await self.run_route(decision("lookup", kind="monitoring", target="auth-service"), "unknown-auth-service CPU 알려줘")
        self.mcp.call_tool.assert_not_called()

    async def test_lookup_with_missing_unknown_or_invented_target_asks_without_tools(self):
        for target in (None, "unknown-service", "auth-service"):
            with self.subTest(target=target):
                result = await self.run_route(decision("lookup", kind="monitoring", target=target), "CPU 사용률 알려줘")
                self.assertIn("시스템", result["final"])
        self.mcp.call_tool.assert_not_called()
        self.mcp.find_prompt.assert_not_called()

    async def test_complex_analysis_and_report_use_quality_model(self):
        # 분류에서 요구한 분석은 고품질 모델이 수행한다.
        with patch.object(self.quality, "analyze", wraps=self.quality.analyze) as analyze:
            with patch.object(self.quality, "write", wraps=self.quality.write) as write:
                result = await self.run_route(decision("incident", analysis=True), "auth-service와 web-01 공통 원인 분석")
        analyze.assert_called_once()
        self.assertGreaterEqual(write.call_count, 1)
        self.assertEqual(result["response_model"], "quality-test")
        self.assertIn("1)", result["final"])

    async def test_incident_without_valid_targets_stops_before_planning(self):
        for query in ("원인 분석해줘", "billing-service 원인 분석",
                      "auth-service와 billing-service 비교", "unknown-auth-service 진단",
                      "auth-service와 payments-service-v2 비교"):
            with self.subTest(query=query), patch.object(self.fast, "plan", wraps=self.fast.plan) as plan:
                result = await self.run_route(decision("incident", analysis=True), query)
                self.assertTrue(result.get("gate_blocked_reason"))
                self.assertNotIn("CPU", result["final"])
                plan.assert_not_called()
        self.mcp.call_tool.assert_not_called()
        self.mcp.find_prompt.assert_not_called()

    async def test_gate_recovers_omitted_service_and_removes_invented_and_duplicate_tasks(self):
        tasks = [{"agent": "research", "kind": "monitoring", "target": target, "status": "pending"}
                 for target in ("payments-service", "payments-service", "auth-service", "billing-service")]
        with patch.object(self.fast, "plan", return_value=({"tasks": tasks}, {"usd": 0})):
            result = await self.run_route(decision("incident", analysis=True),
                                          "payments-service와 gateway-service 공통 원인 분석")
        actual = [c.args[1]["system_name"] for c in self.mcp.call_tool.await_args_list if c.args[0] == "get_monitoring_status"]
        self.assertCountEqual(actual, ["payments-service", "gateway-service"])
        gate = next(e for e in result["events"] if e["type"] == "GateApplied")
        self.assertEqual(gate["removed_tasks"], 3)
        self.assertEqual(gate["added_targets"], ["gateway-service"])
        self.assertEqual(sum(t["agent"] == "analysis" for t in result["tasks"]), 1)

    async def test_lookup_with_multiple_requested_systems_does_not_drop_one(self):
        result = await self.run_route(decision("lookup", kind="monitoring", target="auth-service"),
                                      "auth-service와 web-01 상태 알려줘")
        self.assertEqual(result["route"], "incident")
        self.assertCountEqual([c.args[1]["system_name"] for c in self.mcp.call_tool.await_args_list if c.args[0] == "get_monitoring_status"],
                              ["auth-service", "web-01"])

    async def test_single_system_does_not_silently_add_web_01(self):
        await self.run_route(decision("incident", analysis=True), "auth-service 원인 분석")
        self.assertEqual([c.args[1]["system_name"] for c in self.mcp.call_tool.await_args_list
                          if c.args[0] == "get_monitoring_status"], ["auth-service"])

    async def test_date_is_not_mistaken_for_an_unknown_system(self):
        result = await self.run_route(decision("incident", analysis=True), "auth-service 2026-10-20 장애 원인 분석")
        self.assertFalse(result.get("gate_blocked_reason"))
        self.assertEqual([c.args[1]["system_name"] for c in self.mcp.call_tool.await_args_list
                          if c.args[0] == "get_monitoring_status"], ["auth-service"])

    async def test_replan_retries_only_missing_monitoring(self):
        tool = self.mcp.call_tool.side_effect
        missing_once = True
        async def incomplete(name, arguments):
            nonlocal missing_once
            data = await tool(name, arguments)
            if name == "get_monitoring_status" and arguments["system_name"] == "auth-service" and missing_once:
                data["status"] = "unknown"
                missing_once = False
            return data
        self.mcp.call_tool.side_effect = incomplete
        result = await self.run_route(decision("incident", analysis=True), "auth-service와 web-01 비교")
        targets = [c.args[1]["system_name"] for c in self.mcp.call_tool.await_args_list if c.args[0] == "get_monitoring_status"]
        self.assertEqual(targets.count("auth-service"), 2)
        self.assertEqual(targets.count("web-01"), 1)
        self.assertEqual(result["replan_rounds"], 1)
        self.assertEqual(result["evidence_gaps"], [])
        self.assertEqual(result["validation_status"], "passed")

    async def test_empty_search_stops_after_one_replan_and_discloses_gap(self):
        tool = self.mcp.call_tool.side_effect
        async def empty_search(name, arguments):
            return {"matches": []} if name == "search_runbooks" else await tool(name, arguments)
        self.mcp.call_tool.side_effect = empty_search
        result = await self.run_route(decision("incident", analysis=True), "auth-service 장애 원인 분석")
        calls = [c.args[0] for c in self.mcp.call_tool.await_args_list]
        self.assertEqual(calls.count("search_runbooks"), 2)
        self.assertEqual(calls.count("get_monitoring_status"), 1)
        self.assertEqual(result["replan_rounds"], 1)
        self.assertEqual(result["validation_status"], "partial")
        self.assertIn("근거 부족", result["final"])
        self.mcp.read_resource.assert_not_called()

    async def test_semantic_rejection_is_sent_to_one_rewrite(self):
        original_validate = self.quality.validate
        def review_report(*args):
            review, usage = original_validate(*args)
            if args[2]["analysis"] == "배포 오류로 확정됐습니다.":
                review["checks"][0].update(verdict="unsupported", reason="근거 없는 배포 원인 단정")
            return review, usage
        original = self.quality.write
        async def write_report(*args, **kwargs):
            report, usage = await original(*args, **kwargs)
            if args[2] == 1:
                report["analysis"] = "배포 오류로 확정됐습니다."
            return report, usage
        with patch.object(self.quality, "validate", side_effect=review_report) as validate:
            with patch.object(self.quality, "write", side_effect=write_report) as write:
                result = await self.run_route(decision("incident", analysis=True), "auth-service 원인 분석")
        self.assertEqual(write.await_count, 2)
        self.assertIn("근거 없는 배포", write.await_args_list[1].args[3])
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(result["validation_status"], "passed")

    async def test_failed_drafts_never_reach_answer_stream(self):
        original = self.quality.write
        async def bad_report(*args, **kwargs):
            report, usage = await original(*args, **kwargs)
            report["observations"][0]["cpu_percent"] = 99
            report["analysis"] = "검증 전 잘못된 진단"
            return report, usage
        with patch.object(self.quality, "write", side_effect=bad_report) as write:
            with patch.object(self.quality, "validate", side_effect=AssertionError("invalid facts must fail before LLM review")):
                chunks = [item async for item in self.graph.astream(
                    {"query": "auth-service 원인 분석", "events": []}, stream_mode=["updates", "custom"])]
        self.assertEqual(write.await_count, 2)
        self.assertIn("cpu_percent", write.await_args_list[1].args[3])
        answer = "".join(chunk["delta"] for mode, chunk in chunks if mode == "custom")
        self.assertIn("CPU 78%", answer)
        self.assertNotIn("99%", answer)
        self.assertNotIn("검증 전 잘못된 진단", answer)
        self.assertIn("검증을 통과하지 못해", answer)
        final = next(delta for mode, chunk in chunks if mode == "updates"
                     for name, delta in chunk.items() if name == "finalize")
        self.assertEqual(final["report"]["actions"], [])
        self.assertNotIn("검증 전 잘못된 진단", final["report"]["analysis"])
        self.assertEqual(final["report"]["observations"][0]["cpu_percent"], 78)

    async def test_failed_monitor_does_not_discard_other_system(self):
        tool = self.mcp.call_tool.side_effect
        async def fail_one(name, arguments):
            if name == "get_monitoring_status" and arguments["system_name"] == "auth-service":
                raise ConnectionError("private internal address")
            return await tool(name, arguments)
        self.mcp.call_tool.side_effect = fail_one
        result = await self.run_route(decision("incident", analysis=True), "auth-service와 web-01 비교")
        self.assertIn("web-01: 상태=degraded, CPU 78%", result["final"])
        self.assertNotIn("auth-service: 상태=", result["final"])
        self.assertEqual(result["validation_status"], "partial")
        self.assertIn("auth-service", result["final"])
        self.assertNotIn("private internal address", result["final"])
        self.assertEqual(result["research_failures"][0]["target"], "auth-service")
        self.assertFalse(any(e["type"] == "ReplanCreated" for e in result["events"]))

    async def test_search_failure_preserves_monitoring(self):
        tool = self.mcp.call_tool.side_effect
        async def fail_search(name, arguments):
            if name == "search_runbooks":
                raise ValueError("invalid tool response")
            return await tool(name, arguments)
        self.mcp.call_tool.side_effect = fail_search
        result = await self.run_route(decision("incident", analysis=True), "auth-service CPU 원인 분석")
        self.assertEqual(result["validation_status"], "partial")
        self.assertIn("CPU 78%", result["final"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["research_failures"][0]["kind"], "runbook_search")

    async def test_one_unreadable_manual_does_not_hide_other_manual(self):
        self.mcp.call_tool.side_effect = None
        self.mcp.call_tool.return_value = {"matches": [{"id": "cpu-spike"}, {"id": "network-timeout"}]}
        async def read(uri, server):
            if "cpu-spike" in uri:
                raise OSError("unreadable file")
            return "# Network timeout\n1. 연결 경로 확인"
        self.mcp.read_resource.side_effect = read
        result = await self.run_route(decision("lookup", kind="runbook"), "CPU와 timeout 매뉴얼 찾아줘")
        self.assertEqual([d["id"] for d in result["sources"]], ["network-timeout"])
        self.assertIn("연결 경로 확인", result["final"])
        self.assertEqual(result["validation_status"], "partial")
        self.assertEqual(result["research_failures"][0]["target"], "cpu-spike")

    async def test_timeout_stops_after_three_attempts_without_replan_reset(self):
        tool = self.mcp.call_tool.side_effect
        async def timeout(name, arguments):
            if name == "get_monitoring_status":
                raise asyncio.TimeoutError()
            return await tool(name, arguments)
        self.mcp.call_tool.side_effect = timeout
        result = await self.run_route(decision("incident", analysis=True), "auth-service 원인 분석")
        calls = [c for c in self.mcp.call_tool.await_args_list if c.args[0] == "get_monitoring_status"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(result["research_failures"][0]["code"], "timeout")
        self.assertEqual(result["validation_status"], "partial")

    async def test_total_research_failure_does_not_invent_a_diagnosis(self):
        self.mcp.call_tool.side_effect = ConnectionError("offline")
        with patch.object(self.quality, "analyze", side_effect=AssertionError("no evidence")), \
             patch.object(self.quality, "write", side_effect=AssertionError("no evidence")), \
             patch.object(self.quality, "validate", side_effect=AssertionError("no evidence")):
            result = await self.run_route(decision("incident", analysis=True), "auth-service 원인 분석")
        self.assertEqual(result["validation_status"], "unavailable")
        self.assertNotIn("CPU 0", result["final"])
        self.assertNotIn("healthy", result["final"])
        self.assertIn("진단", result["final"])
        self.assertEqual(len(result["research_failures"]), 2)

    async def test_hanging_tool_is_bounded_and_finishes_with_unavailable_result(self):
        async def hang(*args):
            await asyncio.Event().wait()
        self.mcp.call_tool.side_effect = hang
        with patch("agent.graph.RESEARCH_TIMEOUT_SECONDS", 0.005):
            result = await asyncio.wait_for(
                self.run_route(decision("lookup", kind="monitoring", target="auth-service")), timeout=3)
        self.assertEqual(result["validation_status"], "unavailable")
        self.assertEqual(result["research_failures"][0]["code"], "timeout")
        self.assertEqual(result["research_failures"][0]["attempts"], 3)
        self.assertEqual(result["retry_count"], 2)

    async def test_cancelled_research_is_not_treated_as_a_failed_tool(self):
        self.mcp.call_tool.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await research_node({"query": "auth-service 상태", "events": [], "tasks": [
                {"id": "task-0", "agent": "research", "kind": "monitoring",
                 "target": "auth-service", "status": "pending"}]},
                SupervisorDeps(self.fast, self.quality, self.mcp, None, "v2"))

    async def test_stream_exposes_route_model_and_answer(self):
        chunks = [item async for item in self.graph.astream(
            {"query": "안녕", "events": []}, stream_mode=["updates", "custom"])]
        events = [event for mode, chunk in chunks if mode == "updates"
                  for delta in chunk.values() for event in delta.get("events", [])]
        route = next(e for e in events if e["type"] == "RouteDecided")
        self.assertEqual(route["response_model"], "fast-test")
        self.assertTrue(any(mode == "custom" and chunk["type"] == "AnswerToken" for mode, chunk in chunks))


if __name__ == "__main__":
    unittest.main()
