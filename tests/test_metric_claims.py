import unittest
from agent.reporting import check_facts


class MetricClaimTests(unittest.TestCase):
    def setUp(self):
        self.record = {'system_name': 'auth-service', 'status': 'degraded',
                       'cpu_percent': 78, 'memory_percent': 61, 'alerts': ['p95 latency 2.4s']}
        self.collected = [{'source': 'monitoring', 'data': self.record}]
        self.draft = {'observations': [{k: v for k, v in self.record.items() if k != 'alerts'}],
                      'analysis': '', 'actions': [], 'limitations': []}

    def check(self, text):
        return check_facts({**self.draft, 'analysis': text}, self.collected)

    def test_reversed_metrics_and_false_comparison_are_rejected(self):
        for text in ['auth-service의 CPU는 61%, 메모리는 78%입니다.',
                     'auth-service의 CPU 사용률은 78% 초과입니다.',
                     'auth-service의 메모리는 61% 미만입니다.']:
            with self.subTest(text=text):
                self.assertTrue(self.check(text))

    def test_exact_measurements_valid_comparison_and_conditional_rule_pass(self):
        for text in ['auth-service의 CPU는 78.0％, 메모리는 61퍼센트입니다.',
                     'auth-service의 CPU는 78% 이상입니다.',
                     'CPU가 61% 이상이면 추이를 확인하세요.']:
            with self.subTest(text=text):
                self.assertEqual(self.check(text), [])

    def test_unobserved_disk_cannot_be_declared_normal(self):
        self.assertTrue(self.check('auth-service의 디스크에는 문제가 없습니다.'))
        self.assertEqual(self.check('auth-service의 디스크가 정상인지는 확인할 수 없습니다.'), [])
        self.assertEqual(self.check('auth-service의 디스크 자료는 미확인입니다.'), [])
        self.assertEqual(self.check('디스크 데이터가 없고 CPU 상태만 정상으로 표시됩니다.'), [])

    def test_measurements_are_bound_to_the_named_service(self):
        other = {**self.record, 'system_name': 'web-01', 'cpu_percent': 61}
        self.collected.append({'source': 'monitoring', 'data': other})
        self.draft['observations'].append({k: v for k, v in other.items() if k != 'alerts'})
        self.assertTrue(self.check('auth-service CPU 61%, web-01 CPU 78%입니다.'))
        self.assertEqual(self.check('auth-service CPU 78%, web-01 CPU 61%입니다.'), [])

    def test_action_cannot_remove_original_condition(self):
        doc = {'source': 'runbook', 'id': 'high-latency', 'data': '# 지연\n1. 5분 뒤에도 회복되지 않으면 트래픽을 분산한다.'}
        draft = {**self.draft, 'analysis': '원인 확인이 필요합니다.',
                 'actions': [{'text': '트래픽을 분산한다.', 'sources': ['high-latency']}]}
        issues = check_facts(draft, self.collected + [doc])
        self.assertTrue(any('원문' in issue for issue in issues))
        draft['actions'][0]['text'] = '5분 뒤에도 회복되지 않으면 트래픽을 분산한다.'
        self.assertEqual(check_facts(draft, self.collected + [doc]), [])

    def test_alert_percentages_are_compared_as_measurements_not_lower_bounds(self):
        self.record['alerts'] += ['connection pool 92% used', 'disk usage 95% on /var/log']
        self.assertTrue(self.check('커넥션 풀 사용률 92% 초과로 요청이 지연됩니다.'))
        self.assertTrue(self.check('디스크 사용률은 95% 초과입니다.'))
        self.assertEqual(self.check('커넥션 풀 사용률은 92%, 디스크 사용률은 95%입니다.'), [])

    def test_different_disk_mount_values_are_not_collapsed_into_one_measurement(self):
        self.record['alerts'] += ['disk usage 95% on /var/log', 'disk usage 70% on /tmp']
        self.assertEqual(self.check('디스크 사용률은 70%입니다.'), [])
