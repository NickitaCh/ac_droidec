import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import database
from services import ai_usage, openrouter_router as router


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name)/'usage.db'))
        self.db.start()

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    def test_provider_balance_uses_request_quota_not_credit_balance(self):
        response = Mock()
        response.json.return_value = {'data': {'label': 'SECRET', 'limit_remaining': 999,
                                               'free_model_daily_requests': {'used': 14, 'limit': 50, 'remaining': 36}}}
        with patch.object(ai_usage.requests, 'get', return_value=response) as get:
            quota = ai_usage.provider_quota('private-api-key')
            self.assertEqual(quota['remaining'], 36)
            self.assertEqual(ai_usage.provider_quota('private-api-key')['used'], 14)
        self.assertEqual(get.call_count, 1)
        with ai_usage.connection() as conn:
            stored = str([tuple(row) for row in conn.execute('SELECT value FROM bot_state')])
        self.assertNotIn('SECRET', stored)
        self.assertNotIn('private-api-key', stored)

    def test_failed_refresh_is_marked_stale_without_inventing_balance(self):
        response = Mock()
        response.json.return_value = {'data': {'free_model_daily_requests': {'used': 20, 'limit': 50, 'remaining': 30}}}
        with patch.object(ai_usage.requests, 'get', return_value=response):
            ai_usage.provider_quota('key')
        with patch.object(ai_usage.requests, 'get', side_effect=requests.Timeout):
            quota = ai_usage.provider_quota('key', refresh=True)
            self.assertTrue(quota['stale'])
            self.assertEqual(quota['remaining'], 30)
            self.assertIsNone(ai_usage.provider_quota('another-key', refresh=True))

    def test_atomic_legacy_counter_does_not_lose_parallel_worker_updates(self):
        database.get_bot_state('init')
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: database.record_openrouter_request(), range(20)))
        self.assertEqual(database.get_openrouter_requests_today(), 20)

    def test_router_tracks_retry_rejection_and_tokens_without_text(self):
        limited = Mock(status_code=429)
        limited.json.return_value = {'error': {'message': 'busy'}}
        ok = Mock(status_code=200)
        ok.json.return_value = {'choices': [{'message': {'content': '{"action":"help"}'}}],
                                'usage': {'prompt_tokens': 100, 'completion_tokens': 5, 'cost': 0}}
        models = [({'id': 'first:free', 'params': []}, {}), ({'id': 'second:free', 'params': []}, {})]
        with patch.object(router, 'ranked_candidates', return_value=models), patch.object(router.requests, 'post', side_effect=[limited, ok]):
            data = router.call_json(router.KIND_TEXT, [{'role': 'user', 'content': 'PRIVATE QUESTION'}], 'SECRET', source='mention_assistant')
        self.assertEqual(data, {'action': 'help'})
        summary = ai_usage.summary()[0]
        self.assertEqual((summary['attempts'], summary['ok'], summary['rate_limited']), (2, 1, 1))
        self.assertEqual((summary['input_tokens'], summary['output_tokens']), (100, 5))
        with ai_usage.connection() as conn:
            records = str([dict(row) for row in conn.execute('SELECT * FROM ai_request_usage')])
        self.assertNotIn('PRIVATE QUESTION', records)
        self.assertNotIn('SECRET', records)
        with patch.object(router, 'ranked_candidates', return_value=models[:1]), patch.object(router.requests, 'post', return_value=ok):
            with self.assertRaises(router.RouterError):
                router.call_json(router.KIND_TEXT, [], 'key', source='mention_assistant', validate=lambda _: False, max_bad_outputs=1)
        self.assertEqual(ai_usage.summary()[0]['rejected'], 1)
        self.assertEqual(database.get_openrouter_requests_today(), 2)


if __name__ == '__main__':
    unittest.main()
