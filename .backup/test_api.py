import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api


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

    def test_chat_delegates_to_agent(self):
        with patch.object(api.main, 'natural_task_add', return_value=None), \
             patch.object(api.main, 'natural_task_complete', return_value=None), \
             patch.object(api.main, 'agent_chat', return_value='Hello from the local assistant') as agent_chat:
            response = self.client.post('/api/chat', json={'message': 'Hey, how are you?'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply'], 'Hello from the local assistant')
        agent_chat.assert_called_once_with('Hey, how are you?')

    def test_server_is_bound_to_loopback(self):
        self.assertEqual(api.HOST, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
