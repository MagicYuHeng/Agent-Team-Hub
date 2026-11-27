import argparse
import hmac
import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_worker import ClaudeWorker, WorkerError
from codex_native import CodexRunner, rollout_turn_state
from server.config_utils import REPO_ROOT
from server.http_utils import required_text
from server.routes_clients import (
    handle_acp_request,
    handle_agy_cli_request,
    handle_antigravity_request,
    handle_desktop_request,
    handle_peer_request,
    handle_project_request,
)
from server.routes_control import handle_control_post
from server.store import TaskStore
from teams import Conflict


CONSOLE_FILES = {
    '/manage': ('index.html', 'text/html'),
    '/manage/console.css': ('console.css', 'text/css'),
    '/manage/console.js': ('console.js', 'text/javascript'),
    '/manage/modules/core.js': ('modules/core.js', 'text/javascript'),
    '/manage/modules/members.js': ('modules/members.js', 'text/javascript'),
    '/manage/modules/messages.js': ('modules/messages.js', 'text/javascript'),
    '/manage/modules/workspace.js': ('modules/workspace.js', 'text/javascript'),
    '/manage/modules/docs_diff.js': ('modules/docs_diff.js', 'text/javascript'),
    '/manage/modules/docs_studio.js': ('modules/docs_studio.js', 'text/javascript'),
}


class TaskRunner:
    def __init__(self, store, worker):
        self.store = store
        self.worker = worker
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='task-runner')

    def run(self):
        while not self.stop.is_set():
            task = self.store.claim()
            if task is None:
                self.stop.wait(0.2)
                continue
            task_id = task['task_id']
            instruction = task['instruction']
            try:
                session_id, result = self.worker.run(instruction)
            except (WorkerError, OSError) as error:
                logging.warning('Claude task %s failed (%s)', task_id, type(error).__name__)
                self.store.finish(task_id, 'failed', str(error))
            else:
                self.store.finish(task_id, 'completed', result, session_id)

    def close(self):
        self.stop.set()
        self.worker.close()
        self.thread.join()


class HubServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, address, store, token):
        self.store = store
        self.token = f'Bearer {token}'.encode('ascii')
        self.members = {}
        self.members_lock = threading.Lock()
        self.tool_tickets = {}
        self.antigravity_proofs = {}
        self.codex_hook_sessions = {}
        self.codex_active_turns = {}
        super().__init__(address, HubHandler)

    def note_codex_hook(self, session_id, event, stop_active=False):
        if not isinstance(session_id, str) or not session_id:
            return
        now = time.monotonic()
        self.codex_hook_sessions[session_id] = now
        if event == 'Stop' and stop_active:
            self.codex_active_turns.pop(session_id, None)
        elif event in ('UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'):
            self.codex_active_turns[session_id] = now + 180

    def should_defer_codex_to_hook(self, session_id, rollout_path=None):
        if not session_id:
            return False
        now = time.monotonic()
        last_hook = self.codex_hook_sessions.get(session_id)
        if not last_hook or (now - last_hook) > 86400:
            return False
        state = rollout_turn_state(rollout_path) if rollout_path else 'unknown'
        if state == 'idle':
            self.codex_active_turns.pop(session_id, None)
            return False
        if state == 'busy':
            return True
        return self.codex_active_turns.get(session_id, 0) > now

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def expire_members(self):
        now = time.monotonic()
        self.tool_tickets = {key: ticket for key, ticket in self.tool_tickets.items() if ticket[2] > now}
        self.antigravity_proofs = {key: proof for key, proof in self.antigravity_proofs.items() if proof[1] > now}
        for name, (instance, last_seen, _) in list(self.members.items()):
            if now - last_seen > 60:
                self.store.interrupt_member(name, instance)
                del self.members[name]

    def service_actions(self):
        with self.members_lock:
            self.expire_members()


class HubHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *args):
        pass

    def respond(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def authorize(self):
        supplied = self.headers.get('Authorization', '').encode('utf-8')
        if hmac.compare_digest(supplied, self.server.token):
            return True
        self.respond(401, {'error': 'A valid Hub bearer token is required.'})
        return False

    def read_json(self):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 64 * 1024:
            raise ValueError('Request body must be between 1 byte and 64 KiB.')
        body = json.loads(self.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Expected a JSON object.')
        return body

    def do_GET(self):
        if self.path == '/health':
            self.respond(200, {'status': 'ok'})
        elif self.path in CONSOLE_FILES:
            self.serve_console()
        elif self.authorize():
            if self.path == '/inbox':
                self.respond(200, {'message': None})
            elif self.path == '/control':
                with self.server.members_lock:
                    self.server.expire_members()
                    state = self.server.store.console_state(self.server.members, self.server.server_port)
                self.respond(200, state)
            else:
                self.respond(404, {'error': 'Unknown endpoint.'})

    def do_POST(self):
        if not self.authorize():
            return
        try:
            if self.path in ('/tasks', '/inbox/ack'):
                self.respond(410, {'error': '请在当前原会话重新绑定 Team Hub。'})
            elif handle_control_post(self):
                return
            elif self.path.startswith('/antigravity/'):
                self.antigravity_request()
            elif self.path.startswith('/acp/'):
                self.acp_request()
            elif self.path.startswith('/agy-cli/'):
                self.agy_cli_request()
            elif self.path.startswith('/desktop/'):
                self.desktop_request()
            elif self.path.startswith('/project/'):
                self.project_request()
            else:
                self.respond(404, {'error': 'Unknown endpoint.'})
        except Conflict as error:
            self.respond(409, {'error': str(error)})
        except (ValueError, UnicodeError, OSError) as error:
            self.respond(400, {'error': str(error)})

    def serve_console(self):
        filename, content_type = CONSOLE_FILES[self.path]
        path = REPO_ROOT / 'console' / filename
        content = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', f'{content_type}; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def peer_request(self, action, team, member, body):
        handle_peer_request(self, action, team, member, body)

    def antigravity_request(self):
        handle_antigravity_request(self)

    def acp_request(self):
        handle_acp_request(self)

    def agy_cli_request(self):
        handle_agy_cli_request(self)

    def desktop_request(self):
        handle_desktop_request(self)

    def project_request(self):
        handle_project_request(self)


def main():
    parser = argparse.ArgumentParser(description='Run the local Antigravity to Claude team Hub.')
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--port', type=int, default=58767)
    parser.add_argument('--database', type=Path, default=Path('.team-hub/team.db'))
    args = parser.parse_args()
    token = os.environ.get('TEAM_HUB_TOKEN', '')
    if len(token) < 32 or not all(33 <= ord(char) <= 126 for char in token):
        parser.error('Set TEAM_HUB_TOKEN to at least 32 printable ASCII characters without spaces.')
    if not args.workspace.is_dir():
        parser.error('--workspace must point to an existing directory.')
    args.database.parent.mkdir(parents=True, exist_ok=True)
    store = TaskStore(args.database)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    with HubServer(('127.0.0.1', args.port), store, token) as server:
        store.recover()
        runner = TaskRunner(store, ClaudeWorker(args.workspace.resolve()))
        codex_runner = CodexRunner(store, server.members_lock, server=server)
        runner.thread.start()
        codex_runner.thread.start()
        logging.info('Team Hub listening at http://127.0.0.1:%s', server.server_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            codex_runner.close()
            runner.close()


if __name__ == '__main__':
    main()
