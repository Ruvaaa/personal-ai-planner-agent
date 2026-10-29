import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import gemini_service


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    def test_health_is_safe(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        body = response.text.lower()
        self.assertNotIn('client_secret', body)
        self.assertEqual(response.json()['timezone'], 'Africa/Harare')

    def test_tasks_read_through_python(self):
        task = {'id': 1, 'title': 'API test', 'status': 'pending'}
        with patch.object(api.main, 'load_tasks', return_value=[task]):
            response = self.client.get('/api/tasks')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tasks'], [task])

    def test_task_write_delegates_to_python(self):
        task = {'id': 4, 'title': 'Write test', 'status': 'pending'}
        with patch.object(api.main, 'add_task', return_value=(task, None)) as add_task:
            response = self.client.post('/api/tasks', json={'title': 'Write test'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), task)
        add_task.assert_called_once()

    def test_chat_delegates_to_agent_when_gemini_is_unconfigured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GEMINI_API_KEY', None)
            with patch.object(api.main, 'natural_task_add', return_value=None), \
                 patch.object(api.main, 'natural_task_complete', return_value=None), \
                 patch.object(api.main, 'agent_chat', return_value='Hello from the local assistant') as agent_chat:
                response = self.client.post('/api/chat', json={'message': 'Hey, how are you?'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply'], 'Hello from the local assistant')
        agent_chat.assert_called_once_with('Hey, how are you?')

    def test_missing_gemini_key_returns_clear_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GEMINI_API_KEY', None)
            with patch.object(api.main, 'natural_task_add', return_value=None), \
                 patch.object(api.main, 'natural_task_complete', return_value=None), \
                 patch.object(api.main, 'agent_chat', return_value='Fallback reply'):
                response = self.client.post('/api/chat', json={'message': 'Hello'})
        self.assertEqual(response.status_code, 200)

    def test_gemini_filters_internal_thought_blocks(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}, clear=False):
            fake_client = SimpleNamespace()
            fake_response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(thought=True, text='internal reasoning'),
                SimpleNamespace(thought=False, text='Final answer only.')
            ]))])
            fake_client.models = SimpleNamespace(generate_content=lambda **kwargs: fake_response)
            with patch.object(gemini_service.genai, 'Client', return_value=fake_client):
                answer = gemini_service.generate_reply('What should I do today?', 'context', [])
        self.assertEqual(answer, 'Final answer only.')

    def test_server_is_bound_to_loopback(self):
        self.assertEqual(api.HOST, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
