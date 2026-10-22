import unittest
from unittest.mock import patch

from mcp_server import get_monitoring_status
from agent.reporting import parse_report, check_facts


class MonitoringDataTests(unittest.TestCase):
    def test_generated_cpu_alert_matches_the_same_observation(self):
        for jitter in (-3.0, 8.0):
            with self.subTest(jitter=jitter), patch('mcp_server.random.uniform', return_value=jitter):
                record = get_monitoring_status('payments-service').model_dump()
            cpu = 88.0 if jitter == -3.0 else 99.0
            self.assertEqual(record['cpu_percent'], cpu)
            self.assertIn(f'CPU {cpu}% (threshold 80%)', record['alerts'])
            self.assertIn('payments-service-3 instance responding slow', record['alerts'])
            collected = [{'source': 'monitoring', 'data': record}]
            report = parse_report('{"hypotheses":[],"actions":[],"limitations":[]}', collected)
            self.assertEqual(check_facts(report, collected, []), [])
