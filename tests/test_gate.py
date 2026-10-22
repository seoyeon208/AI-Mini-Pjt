import json
import unittest
from unittest.mock import patch

from agent.graph import gate_node
from agent.ollama_llm import OllamaFastLLM
from agent.openai_llm import OpenAIFastLLM


class PlanOutputTests(unittest.TestCase):
    def test_backends_do_not_replace_unknown_target_before_gate(self):
        proposed = {"agent": "research", "kind": "monitoring", "target": "billing-service"}
        for cls in (OllamaFastLLM, OpenAIFastLLM):
            model = cls("test", 0)
            with self.subTest(backend=cls.__name__), patch.object(model, "_chat", return_value=(
                json.dumps({"tasks": [proposed]}), {"usd": 0}
            )):
                result, _ = model.plan("billing-service 진단", "절차")
                self.assertEqual(result["tasks"][0]["target"], "billing-service")

    def test_malformed_plan_is_left_empty_for_gate_to_repair(self):
        for cls in (OllamaFastLLM, OpenAIFastLLM):
            model = cls("test", 0)
            for content in ("invalid", "[]", "null", '{}', '{"tasks": null}', '{"tasks": "research"}'):
                with self.subTest(backend=cls.__name__, content=content), patch.object(
                    model, "_chat", return_value=(content, {"usd": 0})
                ):
                    result, _ = model.plan("auth-service 진단", "절차")
                    self.assertEqual(result["tasks"], [])


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def apply_gate(self, tasks, analysis=False):
        return await gate_node({"query": "auth-service 진단", "route": "incident",
                                "requested_systems": ["auth-service"], "needs_analysis": analysis,
                                "tasks": tasks, "events": []}, None)

    async def test_rejects_malformed_unknown_and_unneeded_tasks(self):
        result = await self.apply_gate([
            None, "research", {"agent": "shell", "kind": "exec", "target": "auth-service"},
            {"agent": "research", "kind": "delete", "target": "auth-service"},
            {"agent": "research", "kind": "monitoring", "target": ["auth-service"]},
            {"agent": "research", "kind": "runbook", "target": "../../secret"},
            {"agent": "analysis", "kind": "pattern-analysis", "status": "pending"},
        ])
        self.assertEqual([(t["agent"], t["kind"], t.get("target")) for t in result["tasks"]],
                         [("research", "monitoring", "auth-service"), ("research", "runbook_search", "auth-service")])
        self.assertEqual(result["gate_removed"], 7)
        self.assertEqual(len(result["events"][-1]["rejected_tasks"]), 7)

    async def test_gate_assigns_pending_status_and_unique_ids(self):
        result = await self.apply_gate([
            {"id": "same", "agent": "research", "kind": "monitoring", "target": "auth-service", "status": "done"},
            {"id": "same", "agent": "research", "kind": "runbook", "target": "cpu-spike", "status": "done"},
            {"agent": "research", "kind": "runbook", "target": "cpu-spike"},
            {"agent": "analysis", "kind": "pattern-analysis"},
            {"agent": "analysis", "kind": "pattern-analysis"},
        ], analysis=True)
        self.assertEqual(len(result["tasks"]), 3)
        self.assertEqual(len({t["id"] for t in result["tasks"]}), 3)
        self.assertTrue(all(t["status"] == "pending" for t in result["tasks"]))

    async def test_model_cannot_inject_an_unsearched_manual_into_evidence(self):
        result = await self.apply_gate([{"agent": "research", "kind": "runbook", "target": "db-down"}])
        self.assertFalse(any(t["kind"] == "runbook" for t in result["tasks"]))
        self.assertTrue(any(t["kind"] == "runbook_search" for t in result["tasks"]))

    async def test_empty_plan_restores_requested_monitoring_and_required_analysis(self):
        result = await self.apply_gate([], analysis=True)
        self.assertEqual([(t["agent"], t["kind"]) for t in result["tasks"]],
                         [("research", "monitoring"), ("analysis", "pattern-analysis"), ("research", "runbook_search")])


if __name__ == "__main__":
    unittest.main()
