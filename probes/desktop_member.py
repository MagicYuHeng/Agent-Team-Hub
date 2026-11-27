"""Relay Team Hub tasks to the Claude Desktop session owning this MCP process."""

import http.client
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


TRACE_LOG = Path(__file__).resolve().parents[1] / '.team-hub' / 'desktop-delivery.log'


def trace(event, **fields):
    try:
        TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'pid': os.getpid(), 'event': event} | fields
        with TRACE_LOG.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def hub_token():
    token = os.environ.get('TEAM_HUB_TOKEN', '')
    if token or os.name != 'nt':
        return token
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_QUERY_VALUE) as key:
            token, _ = winreg.QueryValueEx(key, 'TEAM_HUB_TOKEN')
    except OSError:
        return ''
    return token if isinstance(token, str) else ''


def valid_uuid(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def write_inbox(address, token, message_id, prompt):
    frames = [
        {'type': 'auth', 'token': token},
        {'type': 'user', 'id': message_id, 'message': {'role': 'user', 'content': prompt}},
    ]
    payload = ''.join(json.dumps(frame) + '\n' for frame in frames).encode('utf-8')
    with open(address, 'r+b', buffering=0) as pipe:
        if pipe.write(payload) != len(payload):
            raise OSError('Incomplete write')


class DesktopMember:
    def __init__(self, port=58767):
        self.instance = str(uuid.uuid4())
        self.port = port
        self.address = os.environ.get('CLAUDE_CODE_MESSAGING_SOCKET', '')
        self.pipe_token = os.environ.get('CLAUDE_CODE_MESSAGING_TOKEN', '')
        self.session_id = valid_uuid(os.environ.get('CLAUDE_CODE_SESSION_ID', ''))
        self.hub_token = hub_token()
        self.name = None
        self.task = None
        self.completion = None
        self.captain_message = None
        self.pending_receipt = None
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        self.runner_started = False
        self.recover_started = False
        self.deliver_after = 0.0

    def has_inbox(self):
        return bool(self.address.lower().startswith('\\\\.\\pipe\\') and self.pipe_token)

    def start_runner(self):
        with self.lock:
            if self.runner_started or self.name is None:
                return
            self.runner_started = True
        threading.Thread(target=self.run, daemon=True).start()

    def start_auto_recover(self, delivery_delay=2.0):
        if not self.session_id or not self.has_inbox():
            return
        with self.lock:
            if self.recover_started or self.name is not None:
                return
            self.recover_started = True
            self.deliver_after = max(self.deliver_after, time.monotonic() + delivery_delay)
        trace('auto_recover_scheduled', session=self.session_id[:8], delay=delivery_delay)
        threading.Thread(target=self._recover_loop, daemon=True).start()

    def _recover_loop(self):
        delay = 2
        try:
            while True:
                with self.lock:
                    if self.name is not None:
                        return
                outcome = self.try_restore()
                if outcome is not None:
                    return
                time.sleep(delay)
                delay = min(delay + 1, 5)
        finally:
            with self.lock:
                self.recover_started = False

    def request(self, path, name=None, **body):
        if not self.hub_token:
            self.hub_token = hub_token()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        headers = {'Authorization': f'Bearer {self.hub_token}', 'Content-Type': 'application/json'}
        if path == 'bind':
            body['captain_ready'] = True
            if self.session_id:
                body['session_id'] = self.session_id
        payload_dict = {'instance': self.instance} | body
        if path != 'restore':
            payload_dict['member'] = name or self.name
        payload = json.dumps(payload_dict)
        try:
            connection.request('POST', f'/desktop/{path}', payload, headers)
            response = connection.getresponse()
            return response.status, json.load(response)
        finally:
            connection.close()

    def try_restore(self, force=False):
        with self.lock:
            if self.name is not None and not force:
                return True
        if not self.session_id or not self.has_inbox():
            return False
        if not self.hub_token:
            self.hub_token = hub_token()
        if not self.hub_token:
            return False
        try:
            status, answer = self.request('restore', session_id=self.session_id)
        except (OSError, ValueError, http.client.HTTPException):
            return None
        if status == 200 and answer.get('bound') and isinstance(answer.get('member'), str):
            with self.lock:
                self.name = answer['member']
                if self.completion is None:
                    self.task = None
            trace('restored', session=self.session_id[:8], member=answer['member'], instance=self.instance[:8])
            self.start_runner()
            return True
        if force and status in (200, 409):
            with self.lock:
                self.name = None
        return False

    def bind(self, name, code=None):
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', name) or name == 'claude':
            return {'bound': False, 'error': 'Choose a lowercase member name distinct from claude.'}
        if self.name is not None and (self.name != name or not code):
            return {'bound': self.name == name, 'member': self.name}
        if not self.has_inbox():
            return {'bound': False, 'error': 'Desktop host did not provide an authenticated Windows inbox.'}
        if not self.hub_token:
            self.hub_token = hub_token()
        if not self.hub_token:
            return {'bound': False, 'error': 'The Desktop MCP process did not inherit TEAM_HUB_TOKEN.'}
        try:
            status, answer = self.request('bind', name=name, code=code)
        except (OSError, ValueError, http.client.HTTPException):
            return {'bound': False, 'error': 'Team Hub at 127.0.0.1:58767 is unreachable.'}
        if status != 200:
            return {'bound': False, 'error': answer.get('error', 'Team Hub rejected the binding.')}
        with self.lock:
            self.name = name
        self.start_runner()
        return answer

    def run(self):
        try:
            while True:
                with self.lock:
                    if self.name is None:
                        return
                try:
                    self.sync()
                except (OSError, ValueError, http.client.HTTPException):
                    pass  # Keep any pending completion and retry on the next poll.
                self.wakeup.wait(1)
                self.wakeup.clear()
        finally:
            with self.lock:
                self.runner_started = False

    def _reclaim_binding(self):
        bind_status, _ = self.request('bind')
        if bind_status == 200:
            return True
        if bind_status == 409 and self.session_id:
            restored = self.try_restore(force=True)
            return bool(restored)
        if bind_status == 409:
            with self.lock:
                self.name = None
        return False

    def sync(self):
        with self.lock:
            if self.name is None:
                return
            completion = self.completion
            ready = self.task is None
        if completion is not None:
            status, _ = self.request('complete', **completion)
            if status == 409:
                if not self._reclaim_binding():
                    return
                status, _ = self.request('complete', **completion)
            if status in (200, 409):
                with self.lock:
                    self.task = self.completion = None
            return
        if time.monotonic() < self.deliver_after:
            return
        status, body = self.request('poll', ready=ready)
        if status == 409:
            if self._reclaim_binding():
                with self.lock:
                    if self.completion is None:
                        self.task = None
            return
        if status == 200 and body.get('task'):
            self.deliver(body['task'])
        if status == 200 and ready and not body.get('task'):
            self.receive_captain_result(body.get('message'))
        if status == 200:
            self.receive_peer(body.get('delivery'))

    def receive_peer(self, delivery):
        if delivery is None:
            self.pending_receipt = None
            return
        receipt = {'id': delivery['id'], 'revision': delivery['revision']}
        if self.pending_receipt and self.pending_receipt['id'] == receipt['id']:
            receipt = self.pending_receipt
        else:
            write_inbox(self.address, self.pipe_token, delivery['id'], delivery['prompt'])
            trace('peer_written', session=(self.session_id or '')[:8], member=self.name,
                  msg_id=delivery['id'], instance=self.instance[:8])
            self.pending_receipt = receipt
        status, _ = self.request('received', **receipt)
        trace('peer_receipt', session=(self.session_id or '')[:8], member=self.name,
              msg_id=delivery['id'], status=status)
        if status == 200:
            self.pending_receipt = None

    def receive_captain_result(self, message):
        with self.lock:
            if message is None:
                self.captain_message = None
                return
            already_written = (self.captain_message and
                               self.captain_message['message_id'] == message['message_id'])
            reference = message['task_id'].replace('-', '')[:12]
            self.captain_message = message | {'reference': reference}
        try:
            if not already_written:
                prompt = f"【来自 {message['from']}】\n{message['result']}"
                write_inbox(self.address, self.pipe_token, message['message_id'], prompt)
        except OSError:
            with self.lock:
                self.captain_message = None
            return
        self.acknowledge(reference)

    def captain_request(self, action, **body):
        if self.name is None:
            self.try_restore()
        if self.name is None:
            return {'error': 'Bind this Desktop conversation first.'}
        try:
            status, result = self.request(action, **body)
            if status == 409 and self._reclaim_binding():
                status, result = self.request(action, **body)
        except (OSError, ValueError, http.client.HTTPException):
            return {'error': 'Team Hub is unreachable.'}
        if not 200 <= status < 300:
            return {'error': result.get('error', 'The Hub rejected the request.')}
        return result

    def dispatch(self, recipient, instruction):
        result = self.captain_request('send', request_id=str(uuid.uuid4()), to=recipient, text=instruction)
        result.pop('task_id', None)
        return result

    def acknowledge(self, reference):
        with self.lock:
            message = self.captain_message
        if not message or message['reference'] != reference:
            return {'error': 'Reference does not match the current Captain result.'}
        return self.captain_request('ack', task_id=message['task_id'], message_id=message['message_id'])

    def deliver(self, task):
        reference = task['task_id'].replace('-', '')[:12]
        with self.lock:
            self.task = task | {'reference': reference}
        prompt = f"【来自 {task.get('from', 'Team Hub')}】\n{task['instruction']}"
        try:
            write_inbox(self.address, self.pipe_token, task['task_id'], prompt)
        except OSError:
            self.set_completion('interrupted', 'Could not confirm the write to the Desktop inbox.')

    def report(self, reference, result):
        if not isinstance(result, str) or not result.strip() or len(result) > 12000:
            return {'recorded': False, 'error': 'Expected a nonempty result of up to 12000 characters.'}
        try:
            result.encode('utf-8')
        except UnicodeError:
            return {'recorded': False, 'error': 'Result contains invalid Unicode.'}
        return self.set_completion('completed', result, reference)

    def set_completion(self, status, result, reference=None):
        with self.lock:
            if self.task is None:
                return {'recorded': False, 'error': 'This Desktop member has no active Hub task.'}
            if status == 'completed' and reference != self.task['reference']:
                return {'recorded': False, 'error': 'Task reference does not match the active assignment.'}
            completion = {'task_id': self.task['task_id'], 'status': status, 'result': result}
            if self.completion is not None and self.completion != completion:
                return {'recorded': False, 'error': 'A result for this task is already pending.'}
            self.completion = completion
        self.wakeup.set()
        return {'recorded': True}
