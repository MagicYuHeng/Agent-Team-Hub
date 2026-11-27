"""Shared isolated Hub server fixture for Team Hub unit test suites."""

import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hub import HubServer, TaskStore


TOKEN = 'isolated-team-hub-test-token-0123456'


class HubTestCase(unittest.TestCase):
    def setUp(self):
        test_root = Path('.team-hub').resolve()
        test_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = TaskStore(self.root / 'hub.db')
        self.server = HubServer(('127.0.0.1', 0), self.store, TOKEN)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def api(self, path, body=None, status=200):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(f'http://127.0.0.1:{self.server.server_port}{path}', data=data,
                          headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {TOKEN}'})
        try:
            response = urlopen(request, timeout=4)
        except HTTPError as error:
            response = error
        with response:
            payload = json.load(response)
            self.assertEqual(response.status, status, payload)
            return payload

    def team(self, name):
        path = self.root / name
        path.mkdir()
        team = self.api('/control/team', {'action': 'create', 'name': name, 'workspace': str(path)})
        return team['team_id'], path

    def member(self, team, name, product, responsibility='测试职责', member=None, **extra):
        return self.api('/control/team', {'action': 'member', 'team_id': team, 'member': member,
                        'name': name, 'product': product, 'responsibility': responsibility} | extra)['member']

    def desktop(self, member, session_id=None):
        code = self.api('/control/rebind', {'member': member})['code']
        binding = {'member': member, 'instance': str(uuid.uuid4())}
        payload = binding | {'code': code}
        if session_id is not None:
            payload['session_id'] = session_id
        self.api('/desktop/bind', payload)
        return binding
