import unittest

from runbook_search import search_runbooks
from agent.evidence import assess_evidence
from agent.reporting import check_facts, parse_review


def monitoring(system="auth-service", cpu=78):
    return {"source": "monitoring", "target": system, "data": {
        "system_name": system, "cpu_percent": cpu, "memory_percent": 61,
        "status": "degraded", "alerts": ["CPU high"], "last_checked": "2026-10-20T10:00:00"}}


DOC = {"source": "runbook", "id": "cpu-spike", "title": "CPU 급증", "uri": "runbook://incident-response/cpu-spike",
       "data": "# CPU 급증\n1. CPU 확인\n2. 프로세스 확인"}


class SearchTests(unittest.TestCase):
    def test_latency_does_not_retrieve_db_failure_from_connection_alone(self):
        found = search_runbooks("응답 지연 p95 latency 2.4s connection pool 92% used")
        self.assertEqual({m["id"] for m in found["matches"]}, {"high-latency"})

    def test_memory_and_disk_do_not_retrieve_cpu_from_usage_alone(self):
        found = search_runbooks("메모리 증가 디스크 부족 memory usage climbing heap near limit")
        self.assertEqual({m["id"] for m in found["matches"]}, {"memory-leak", "disk-full"})

    def test_ambiguous_usage_is_not_enough_to_select_a_manual(self):
        self.assertEqual(search_runbooks("service usage connection") ["matches"], [])

    def test_specific_database_and_network_symptoms_are_preserved(self):
        for query, expected in (("database connection refused", "db-down"),
                                ("network packet loss", "network-timeout"),
                                ("CPU usage high", "cpu-spike")):
            with self.subTest(query=query):
                self.assertEqual(search_runbooks(query, limit=1)["matches"][0]["id"], expected)

    def test_composite_symptoms_retrieve_both_manuals(self):
        results = search_runbooks("CPU 급증 upstream timeout 504 네트워크 타임아웃")
        self.assertTrue({"cpu-spike", "network-timeout"} <= {r["id"] for r in results["matches"]})
        for result in results["matches"]:
            self.assertTrue(result["uri"].endswith(result["id"]))
            self.assertTrue(result["excerpt"])

    def test_no_relevant_document_does_not_default_to_db_down(self):
        self.assertEqual(search_runbooks("우주양말 양자얽힘") ["matches"], [])

    def test_korean_and_english_memory_queries_find_same_manual(self):
        for query in ("메모리가 계속 늘어나요 누수", "memory heap OOM"):
            self.assertEqual(search_runbooks(query, limit=1)["matches"][0]["id"], "memory-leak")


class EvidenceTests(unittest.TestCase):
    def test_one_manual_does_not_hide_missing_other_system(self):
        state = {"route": "incident", "requested_systems": ["auth-service", "gateway-service"],
                 "collected": [monitoring(), DOC, {"source": "search", "target": "auth-service",
                     "data": {"matches": [{"id": "cpu-spike"}]}}]}
        gaps = assess_evidence(state)
        self.assertIn(("monitoring", "gateway-service"), [(g["kind"], g["target"]) for g in gaps])
        self.assertNotIn(("monitoring", "auth-service"), [(g["kind"], g["target"]) for g in gaps])

    def test_unknown_monitoring_and_missing_document_remain_gaps(self):
        record = monitoring()
        record["data"]["status"] = "unknown"
        state = {"route": "incident", "requested_systems": ["auth-service"], "collected": [record,
                 {"source": "search", "target": "auth-service", "data": {"matches": [{"id": "cpu-spike"}]}}]}
        self.assertEqual({(g["kind"], g["target"]) for g in assess_evidence(state)},
                         {("monitoring", "auth-service"), ("runbook", "cpu-spike")})


class FactTests(unittest.TestCase):
    def test_action_must_remain_original_even_when_added_measurement_is_correct(self):
        report = self.report()
        report["actions"] = [{"text": "auth-service의 CPU 78%에 대해 프로세스를 확인하세요.", "sources": ["cpu-spike"]}]
        self.assertTrue(check_facts(report, [monitoring(), DOC]))
        report["actions"][0]["text"] = "web-01의 CPU 78%에 대해 프로세스를 확인하세요."
        report["observations"].append({"system_name": "web-01", "status": "degraded", "cpu_percent": 20, "memory_percent": 61})
        issues = check_facts(report, [monitoring(), monitoring("web-01", cpu=20), DOC])
        self.assertTrue(any("조치 원문" in issue for issue in issues))

    def test_instance_named_in_an_actual_alert_is_not_invented(self):
        record = monitoring()
        record["data"]["alerts"] = ["auth-service-3 instance responding slow"]
        report = self.report()
        report["analysis"] = "auth-service-3 응답 지연 알림이 있습니다."
        self.assertEqual(check_facts(report, [record, DOC]), [])
        report["analysis"] = "auth-service-9 응답 지연 알림이 있습니다."
        self.assertTrue(check_facts(report, [record, DOC]))

    def report(self):
        return {"observations": [{"system_name": "auth-service", "status": "degraded", "cpu_percent": 78,
                                  "memory_percent": 61}], "analysis": "CPU 상승의 원인은 추가 확인이 필요합니다.",
                "actions": [{"text": "프로세스 확인", "sources": ["cpu-spike"]}], "limitations": []}

    def test_valid_facts_and_citations_pass(self):
        self.assertEqual(check_facts(self.report(), [monitoring(), DOC]), [])

    def test_wrong_metrics_system_status_and_citation_are_rejected(self):
        for field, value in (("cpu_percent", 99), ("memory_percent", 12),
                             ("system_name", "billing-service"), ("status", "healthy")):
            report = self.report()
            report["observations"][0][field] = value
            with self.subTest(field=field):
                self.assertTrue(check_facts(report, [monitoring(), DOC]))
        report = self.report()
        report["actions"][0]["sources"] = ["invented-manual"]
        self.assertTrue(check_facts(report, [monitoring(), DOC]))

    def test_invalid_validator_output_cannot_pass(self):
        for output in ('{}', 'not json', '{"relevance": "5"}', '[]'):
            self.assertIn("error", parse_review(output))

    def test_valid_review_needs_no_invented_issue(self):
        self.assertEqual(parse_review('{"checks": []}'), {"checks": []})

    def test_rejected_verdict_without_an_error_cannot_pass(self):
        self.assertIn("error", parse_review('{"valid": false, "issues": []}'))

    def test_wrong_percentage_in_limitations_cannot_bypass_fact_check(self):
        report = self.report()
        report["limitations"] = ["CPU 99%는 임시로 관측되었습니다."]
        self.assertTrue(check_facts(report, [monitoring(), DOC]))

    def test_repeating_verified_percentages_is_not_a_fact_error(self):
        report = self.report()
        report["analysis"] = "auth-service의 CPU는 78.0％, 메모리는 61퍼센트입니다."
        self.assertEqual(check_facts(report, [monitoring(), DOC]), [])

    def test_action_percentage_must_match_a_whole_value_in_its_source(self):
        report = self.report()
        doc = {**DOC, "data": "# CPU\n1. 증가율 190%인지 확인한다."}
        report["actions"] = [{"text": "CPU 90%이면 재시작한다.", "sources": ["cpu-spike"]}]
        self.assertTrue(check_facts(report, [monitoring(), doc]))

    def test_manual_answer_requires_steps_even_without_monitoring(self):
        report = {"observations": [], "analysis": "매뉴얼을 조회했습니다.", "actions": [], "limitations": []}
        self.assertTrue(check_facts(report, [DOC], mode="runbook"))
        report["actions"] = [{"text": "CPU 확인", "sources": ["cpu-spike"]}]
        self.assertEqual(check_facts(report, [DOC], mode="runbook"), [])

    def test_status_lookup_cannot_add_unrequested_actions(self):
        self.assertTrue(check_facts(self.report(), [monitoring(), DOC], mode="monitoring"))


if __name__ == "__main__":
    unittest.main()
