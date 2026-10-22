import json
import unittest
from copy import deepcopy

from agent import reporting
from agent.reporting import checked_review_issues, report_input, parse_report


class ReviewReferenceTests(unittest.TestCase):
    def setUp(self):
        self.collected = [{"source": "monitoring", "data": {
            "system_name": "auth-service", "status": "degraded",
            "cpu_percent": 78, "memory_percent": 61, "alerts": ["latency"]}}]
        self.draft = {"observations": [], "analysis": 'CPU는 "61%"입니다.',
                      "actions": [], "limitations": ["배포 기록은 미확인입니다."]}
        self.review = {"checks": [
            {"claim_id": "C1", "verdict": "contradiction", "evidence_ids": ["E1"],
             "reason": "CPU 수치를 메모리 값으로 바꾸었습니다."},
            {"claim_id": "C2", "verdict": "limitation", "evidence_ids": ["E1"],
             "reason": "제공된 자료에 배포 기록이 없습니다."}]}

    def test_feedback_uses_original_text_without_model_copying_quotes(self):
        feedback = checked_review_issues(self.review, self.collected, self.draft)
        self.assertEqual(len(feedback), 1)
        self.assertIn(self.draft['analysis'], feedback[0])
        self.assertIn('"cpu_percent": 78', feedback[0])
        context = json.loads(report_input("진단", self.collected, [], draft=self.draft))
        self.assertEqual(context['claims']['C2']['section'], 'limitation')
        self.assertEqual(context['evidence']['E1']['source'], 'monitoring:auth-service')

    def test_missing_duplicate_unknown_and_unreferenced_reviews_fail_closed(self):
        invalids = []
        missing = deepcopy(self.review); missing['checks'].pop(); invalids.append(missing)
        duplicate = deepcopy(self.review); duplicate['checks'].append(duplicate['checks'][0]); invalids.append(duplicate)
        for field, value in [('claim_id', 'C99'), ('evidence_ids', ['E99']), ('evidence_ids', [])]:
            invalid = deepcopy(self.review); invalid['checks'][0][field] = value; invalids.append(invalid)
        for review in invalids:
            with self.subTest(review=review), self.assertRaises(ValueError):
                checked_review_issues(review, self.collected, self.draft)

    def test_plausible_hypothesis_and_disclosed_limits_are_not_errors(self):
        draft = {**self.draft, 'analysis': '외부 호출 지연 가능성이 있으나 로그 확인이 필요합니다.'}
        review = deepcopy(self.review)
        review['checks'][0]['verdict'] = 'hypothesis'
        self.assertEqual(checked_review_issues(review, self.collected, draft), [])

    def test_source_exact_actions_are_not_rewritten_or_reviewed_twice(self):
        collected = [{'source': 'runbook', 'id': 'cpu-spike', 'data': '# CPU\n1. 90%가 지속되면 확장한다.'}]
        draft = {'analysis': '원인 확인이 필요합니다.', 'limitations': [],
                 'actions': [{'text': '90%가 지속되면 확장한다.', 'sources': ['cpu-spike']}]}
        context = json.loads(report_input('진단', collected, [], draft=draft))
        self.assertEqual(len(context['claims']), 1)
        self.assertEqual(draft['actions'][0]['text'], '90%가 지속되면 확장한다.')
        draft['actions'][0]['text'] = '확장한다.'
        context = json.loads(report_input('진단', collected, [], draft=draft))
        self.assertEqual(context['claims']['C2']['section'], 'action')

    def test_writer_separates_original_alerts_from_hypotheses(self):
        draft = {"hypotheses": [{"explanation": "외부 호출 지연", "evidence_ids": ["E1"],
                                 "verification": ["호출 추적", "쿼리·외부 호출 로그"]}],
                 "actions": [], "limitations": []}
        report = parse_report(json.dumps(draft), self.collected)
        self.assertIsNotNone(report)
        self.assertEqual(report['observations'][0]['cpu_percent'], 78)
        self.assertIn('auth-service: latency', report['analysis'])
        self.assertIn('가능 원인(가설): 외부 호출 지연', report['analysis'])
        context = json.loads(report_input('진단', self.collected, [], draft=report))
        self.assertNotIn('auth-service: latency', [c['text'] for c in context['claims'].values()])
        draft['hypotheses'][0]['evidence_ids'] = ['E99']
        self.assertIsNone(parse_report(json.dumps(draft), self.collected))

    def test_verification_cannot_hide_an_unconditional_operational_action(self):
        draft = {"hypotheses": [{"explanation": "외부 호출 지연", "evidence_ids": ["E1"],
                                 "verification": ["인스턴스를 로드밸런서에서 제외한다"]}],
                 "actions": [], "limitations": []}
        self.assertIsNone(parse_report(json.dumps(draft), self.collected))

    def test_hypothesis_does_not_rewrite_measurements_already_shown_as_facts(self):
        for explanation in ["커넥션 풀 사용률 92% 초과", "커넥션 풀 사용률 92 초과", "5분 동안 회복되지 않음", "디스크 사용률 ９５percent 이상", "사용률 ٩٥percent 이상"]:
            draft = {"hypotheses": [{"explanation": explanation, "evidence_ids": ["E1"],
                                     "verification": ["시계열 모니터링"]}],
                     "actions": [], "limitations": []}
            with self.subTest(explanation=explanation):
                self.assertIsNone(parse_report(json.dumps(draft), self.collected))

    def test_writer_schema_only_allows_actual_document_and_evidence_ids(self):
        docs = [{"source": "runbook", "id": "cpu-spike", "data": "# CPU\n1. 상태 확인"}]
        schema = reporting.draft_schema(self.collected + docs)
        self.assertEqual(schema['$defs']['ActionReference']['properties']['source']['enum'], ['cpu-spike'])
        self.assertEqual(schema['$defs']['Hypothesis']['properties']['evidence_ids']['items']['enum'], ['E1', 'E2'])

    def test_decimal_measurement_is_not_split_into_two_claims(self):
        draft = {'analysis': '지연은 2.4s입니다. 원인은 미확인입니다.', 'actions': [], 'limitations': []}
        context = json.loads(report_input('진단', self.collected, [], draft=draft))
        self.assertEqual([c['text'] for c in context['claims'].values()],
                         ['지연은 2.4s입니다.', '원인은 미확인입니다.'])

    def test_typed_missing_data_is_not_treated_as_an_observed_fact(self):
        report = parse_report(json.dumps({"hypotheses": [{"explanation": "외부 호출 지연",
            "evidence_ids": ["E1"], "verification": ["서비스 상세 로그", "호출 추적"]}],
            "actions": [], "limitations": []}), self.collected)
        claims, _ = reporting.review_items(self.collected, report)
        self.assertEqual(len(claims), 1)
        self.assertIn('가능 원인(가설)', claims['C1']['text'])
        # 허용 자료명 뒤에 사실 주장이나 조치를 붙이면 검수에서 빠지면 안 된다.
        report['analysis'] += '\n확인할 자료: 서비스 상세 로그, CPU 정상\n확인할 자료: 인스턴스를 제외한다'
        claims, _ = reporting.review_items(self.collected, report)
        self.assertEqual(len(claims), 3)
