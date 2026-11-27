"""Queue tasks in existing Codex threads and correlate their local results."""

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from teams import Conflict


def locate_thread(thread_id, codex_home=None):
    thread_id = str(uuid.UUID(thread_id))
    root = (Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'sessions').resolve()
    for path in root.glob(f'*/*/*/*-{thread_id}.jsonl'):
        if not path.resolve().is_relative_to(root):
            continue
        with path.open('rb') as stream:
            header = json.loads(stream.readline())
        meta = header.get('payload', {})
        if header.get('type') == 'session_meta' and meta.get('id') == thread_id:
            return thread_id, path.resolve()
    raise Conflict('找不到这个本机 Codex 线程。请打开目标聊天并复制其 Session ID。')


def marker(task_id):
    return f'[Team Hub task {task_id}]'


def message_text(payload):
    return '\n'.join(block['text'] for block in payload.get('content', [])
                     if isinstance(block, dict) and isinstance(block.get('text'), str))


@dataclass
class RolloutReader:
    offset: int
    turn: str | None = None
    matched: bool = False
    final: str = ''

    def poll(self, path, task_marker):
        with Path(path).open('rb') as stream:
            stream.seek(self.offset)
            while line := stream.readline():
                if not line.endswith(b'\n'):
                    break
                self.offset = stream.tell()
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                result = self.feed(record, task_marker)
                if result:
                    return result
        return None

    def feed(self, record, task_marker):
        kind = record.get('type')
        payload = record.get('payload', {})
        if kind == 'event_msg' and payload.get('type') == 'task_started':
            self.turn = payload.get('turn_id')
            self.matched = False
            self.final = ''
        elif self.turn and kind == 'response_item' and payload.get('type') == 'message':
            if payload.get('role') == 'user':
                self.matched |= message_text(payload).startswith(task_marker + '\n')
            elif self.matched and payload.get('role') == 'assistant' and payload.get('phase') == 'final_answer':
                self.final = message_text(payload)
        elif (self.turn and self.matched and kind == 'event_msg'
              and payload.get('turn_id') == self.turn):
            if payload.get('type') == 'task_complete':
                text = (payload.get('last_agent_message') or self.final).strip()
                return ('completed', text) if text else ('failed', 'Codex 完成了该轮，但没有返回答案。')
            if payload.get('type') == 'turn_aborted':
                return 'interrupted', 'Codex 原聊天中止了该任务。'
        return None


def queue_prompt(thread_id, prompt):
    executable = shutil.which('codex')
    if not executable:
        raise OSError('Codex CLI is unavailable.')
    return subprocess.run([executable, 'queue', '--thread', thread_id, '--message', prompt],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode


class CodexRunner:
    def __init__(self, store, members_lock):
        self.store = store
        self.members_lock = members_lock
        self.readers = {}
        self.retry_after = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='codex-thread-runner')

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except (OSError, ValueError):
                logging.warning('Could not scan Codex jobs this cycle', exc_info=True)
            self.stop.wait(0.65)

    def close(self):
        self.stop.set()
        self.thread.join()

    def tick(self):
        for job in self.store.codex_jobs():
            task_id = job['task_id']
            reader = self.readers.setdefault(task_id, RolloutReader(job['start_offset']))
            try:
                result = reader.poll(job['rollout'], marker(task_id))
            except OSError:
                continue
            if result:
                status, text = result
                self.store.finish(task_id, status, text, job['thread_id'])
                self.readers.pop(task_id, None)
        for binding in self.store.codex_bindings():
            with self.members_lock:
                if not self.store.is_codex_binding(binding['member'], binding['thread_id']):
                    continue
                try:
                    offset = Path(binding['rollout']).stat().st_size
                except OSError:
                    continue
                task = self.store.claim(binding['member'], codex=(binding, offset))
            if task:
                self.dispatch(task, binding['thread_id'])
            self.deliver_peer(binding)

    def deliver_peer(self, binding):
        member, thread_id = binding['member'], binding['thread_id']
        if time.monotonic() < self.retry_after.get(member, 0):
            return
        with self.members_lock:
            if not self.store.is_codex_binding(member, thread_id):
                return
            team = self.store.teams.member_team(member)
            delivery = self.store.messages.reserve_codex(team['id'], member) if team else None
        if not delivery:
            return
        try:
            status = queue_prompt(thread_id, delivery['prompt'])
        except subprocess.TimeoutExpired:
            self.store.messages.received(team['id'], member, delivery['id'], delivery['revision'], 'unconfirmed')
            return
        except OSError:
            status = 1
        if status:
            self.retry_after[member] = time.monotonic() + 5
            self.store.messages.retry_codex(delivery['id'])
        else:
            self.store.messages.received(team['id'], member, delivery['id'], delivery['revision'])

    def dispatch(self, task, thread_id):
        task_id = task['task_id']
        prompt = marker(task_id) + '\n' + task['instruction']
        try:
            exit_code = queue_prompt(thread_id, prompt)
        except subprocess.TimeoutExpired:
            logging.warning('Codex queue timed out for %s. Waiting for the original thread.', task_id)
            return
        except OSError:
            exit_code = -1
        if exit_code:
            self.store.finish(task_id, 'failed', 'Codex CLI 未能把任务加入原线程。', thread_id)
