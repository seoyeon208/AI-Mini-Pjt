import unittest
import json
from unittest.mock import Mock, patch

from agent.deps import SupervisorDeps
from agent.graph import validate_node, replan_check_node
from agent.fake_llm import FastLLM, QualityLLM
from agent.ollama_llm import OllamaQualityLLM


class ReviewEvidenceTests(unittest.TestCase):
    def test_review_disables_long_reasoning_without_changing_analysis(self):
        model = OllamaQualityLLM("test", 0)
        response = Mock()
        response.json.return_value = {"message": {"content": '{"checks": []}'}}
        with patch("agent.ollama_llm.httpx.post", return_value=response) as post:
            model.validate("요청", [], {}, [])
            self.assertIs(post.call_args.kwargs["json"].get("think"), False)
            refs = post.call_args.kwargs["json"]["format"]["$defs"]["ClaimReview"]["properties"]
            self.assertEqual(refs["evidence_ids"]["items"]["enum"], [])
            model.analyze([])
            self.assertNotIn("think", post.call_args.kwargs["json"])


class BoundedValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = SupervisorDeps(FastLLM("fast", 0), QualityLLM("quality", 0), Mock(), None, "v2")

    async def test_writer_selects_original_procedure_with_conditions(self):
        model = OllamaQualityLLM("test", 0)
        collected = [{"source": "runbook", "id": "cpu-spike", "title": "CPU", "data": "# CPU\n1. 상태 확인\n2. CPU가 90%를 넘고 지속되면 확장한다."}]
        draft = {"hypotheses": [], "limitations": [],
                 "actions": [{"source": "cpu-spike", "step": 2}]}
        with patch.object(model, "_chat", return_value=(json.dumps(draft), {"usd": 0})):
            report, _ = await model.write("CPU 대응", collected, 1, None)
        self.assertIsNotNone(report)
        self.assertEqual(report["actions"], [{"text": "CPU가 90%를 넘고 지속되면 확장한다.", "sources": ["cpu-spike"]}])
        for invalid in ({"source": "cpu-spike", "step": 9}, {"source": "unknown", "step": 1}):
            draft["actions"] = [invalid]
            with patch.object(model, "_chat", return_value=(json.dumps(draft), {"usd": 0})):
                report, _ = await model.write("CPU 대응", collected, 1, None)
            self.assertIsNone(report)

    async def test_configured_reviewer_is_used_independently_of_writer(self):
        reviewer = Mock()
        review = {"checks": [{"claim_id": "C1", "verdict": "supported", "evidence_ids": ["E1"], "reason": "수치 일치"}]}
        reviewer.validate.return_value = (review, {"usd": 0})
        self.deps.review_llm = reviewer
        record = {"system_name": "auth-service", "status": "degraded", "cpu_percent": 78, "memory_percent": 61}
        state = {"route": "incident", "query": "auth-service 분석", "events": [],
                 "collected": [{"source": "monitoring", "data": record}],
                 "draft": {"observations": [record], "analysis": "CPU 78%, 메모리 61%입니다.",
                           "actions": [], "limitations": []}}
        with patch.object(self.deps.quality_llm, "validate", side_effect=AssertionError("writer must not review")):
            result = await validate_node(state, self.deps)
        self.assertEqual(result["validation_status"], "passed")
        self.assertEqual(result["validation_review"], review)

    async def test_ungrounded_review_is_not_sent_to_rewriter(self):
        record = {"system_name": "log-service", "status": "degraded", "cpu_percent": 20, "memory_percent": 35,
                  "alerts": ["disk usage 95%"]}
        state = {"route": "incident", "query": "log-service 디스크 진단", "events": [],
                 "collected": [{"source": "monitoring", "data": record}],
                 "draft": {"observations": [{k: v for k, v in record.items() if k != "alerts"}],
                           "analysis": "디스크 사용률은 95%입니다.", "actions": [], "limitations": []}}
        review = {"valid": False, "issues": ["95%가 90% 이상 기준과 모순되므로 정상이라고 수정하세요."]}
        with patch.object(self.deps.quality_llm, "validate", return_value=(review, {"usd": 0})):
            result = await validate_node(state, self.deps)
        self.assertEqual(result["validation_status"], "failed")
        self.assertIsNone(result["validation_feedback"])
        self.assertNotIn("정상이라고", " ".join(result["validation_issues"]))

    async def test_second_failed_draft_is_not_forced_to_pass(self):
        result = await validate_node({"route": "incident", "query": "진단", "draft": "틀린 답변",
                                      "collected": [], "events": [], "validation_attempts": 1}, self.deps)
        self.assertEqual(result.get("validation_status"), "failed")
        self.assertEqual(result["validation_attempts"], 2)

    async def test_replan_limit_does_not_claim_missing_evidence_is_sufficient(self):
        result = await replan_check_node({"route": "incident", "requested_systems": ["auth-service"],
                                          "query": "원인 분석", "collected": [], "tasks": [],
                                          "events": [], "replan_rounds": 1}, self.deps)
        self.assertFalse(result["events"][-1]["is_sufficient"])
        self.assertTrue(result.get("evidence_gaps"))

    async def test_manual_cannot_drop_an_action_condition(self):
        result = await validate_node({"route": "lookup", "report_kind": "runbook", "query": "CPU 매뉴얼",
            "draft": {"observations": [], "analysis": "매뉴얼에 기록된 대응 절차입니다.",
                      "actions": [{"text": "즉시 재시작한다.", "sources": ["cpu-spike"]}], "limitations": []},
            "collected": [{"source": "runbook", "id": "cpu-spike", "title": "CPU", "data": "# CPU\n1. CPU가 90% 이상이면 재시작한다."}],
            "events": [], "evidence_gaps": []}, self.deps)
        self.assertEqual(result["validation_status"], "failed")
        self.assertEqual(result["validation_method"], "source_exact")
        self.assertFalse(result["events"][-1]["will_retry"])

    async def test_lookup_does_not_accept_an_added_cause(self):
        record = {"system_name": "auth-service", "status": "degraded", "cpu_percent": 78, "memory_percent": 61}
        result = await validate_node({"route": "lookup", "report_kind": "monitoring", "query": "auth-service 상태",
            "draft": {"observations": [record], "analysis": "배포 오류가 원인으로 확정됐습니다.", "actions": [], "limitations": []},
            "collected": [{"source": "monitoring", "target": "auth-service", "data": record}],
            "events": [], "evidence_gaps": []}, self.deps)
        self.assertEqual(result["validation_status"], "failed")


if __name__ == "__main__":
    unittest.main()
