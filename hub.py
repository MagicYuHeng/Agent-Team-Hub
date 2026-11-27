import argparse
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_worker import ClaudeWorker, WorkerError
from codex_native import CodexRunner, locate_thread
from docs_store import DocStore, detect_git_identity, inspect_worktree_ai
from messages import Messages
from teams import ANTIGRAVITY, Conflict, Teams, display_worktree, team_mode
from workspace import Projects, acp_mcp_descriptor, agy_cli_wait_command, configure_global_access, global_access_status, load_team_rules


CONSOLE_FILES = {
    '/manage': ('index.html', 'text/html'),
    '/manage/console.css': ('console.css', 'text/css'),
    '/manage/console.js': ('console.js', 'text/javascript'),
}


class TaskStore:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL UNIQUE,
                    instruction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    session_id TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            if 'recipient' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN recipient TEXT NOT NULL DEFAULT 'claude'")
            if 'worker_instance' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN worker_instance TEXT')
            if 'team_id' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN team_id INTEGER NOT NULL DEFAULT 1')
            db.execute('''
                CREATE TABLE IF NOT EXISTS member_controls (
                    name TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0
                )
            ''')
            db.execute('INSERT OR IGNORE INTO member_controls (name) SELECT DISTINCT recipient FROM tasks')
            db.execute('''CREATE TABLE IF NOT EXISTS codex_bindings (
                member TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, rollout TEXT NOT NULL
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS codex_jobs (
                task_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, rollout TEXT NOT NULL, start_offset INTEGER NOT NULL
            )''')
        self.messages = Messages(self)
        self.teams = Teams(self)
        self.projects = Projects(self)
        self.docs = DocStore(self)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE team_messages SET delivered='unconfirmed' WHERE delivered='submitting'")
            db.execute('''
                UPDATE tasks SET status = 'interrupted', result = ? WHERE status = 'running'
                AND NOT EXISTS (SELECT 1 FROM codex_jobs WHERE codex_jobs.task_id=tasks.task_id)
            ''', ('The Hub stopped before it recorded a Claude result.',))

    def submit(self, request_id, instruction, recipient='claude', connected=True, team_id=1):
        with self.connect() as db:
            db.execute('''
                INSERT INTO tasks (task_id, request_id, instruction, recipient, team_id, status)
                SELECT ?, ?, ?, ?, ?, 'queued' WHERE ? ON CONFLICT(request_id) DO NOTHING
            ''', (str(uuid.uuid4()), request_id, instruction, recipient, team_id, connected))
            task = db.execute('SELECT * FROM tasks WHERE request_id = ?', (request_id,)).fetchone()
            if task is None:
                return None
            if (task['instruction'], task['recipient'], task['team_id']) != (instruction, recipient, team_id):
                raise ValueError('request_id already belongs to a different recipient or instruction.')
            return {'task_id': task['task_id'], 'status': task['status']}

    def claim(self, recipient='claude', instance=None, codex=None):
        with self.connect() as db:
            task = db.execute('''
                UPDATE tasks SET status = 'running', worker_instance = ?
                WHERE rowid = (
                    SELECT rowid FROM tasks WHERE recipient = ? AND status = 'queued'
                    AND team_id = (SELECT id FROM teams WHERE active=1)
                    AND EXISTS (SELECT 1 FROM roster JOIN teams ON teams.id=roster.team_id
                                WHERE teams.active=1 AND teams.captain<>? AND roster.member=?)
                    ORDER BY rowid LIMIT 1
                )
                AND NOT EXISTS (SELECT 1 FROM tasks WHERE recipient = ? AND status = 'running')
                AND NOT EXISTS (SELECT 1 FROM member_controls WHERE name = ? AND paused = 1)
                RETURNING task_id, instruction, team_id, recipient
            ''', (instance, recipient, recipient, recipient, recipient, recipient)).fetchone()
            if task is not None and codex is not None:
                binding, offset = codex
                db.execute('''INSERT INTO codex_jobs (task_id,thread_id,rollout,start_offset) VALUES (?,?,?,?)''',
                           (task['task_id'], binding['thread_id'], binding['rollout'], offset))
            if task is None and instance is not None:
                return db.execute('''
                    SELECT task_id, instruction, team_id, recipient FROM tasks
                    WHERE recipient = ? AND worker_instance = ? AND status = 'running'
                ''', (recipient, instance)).fetchone()
            return task

    def codex_bindings(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM codex_bindings')]

    def is_codex_binding(self, member, thread_id=None):
        with self.connect() as db:
            row = db.execute('SELECT thread_id FROM codex_bindings WHERE member=?', (member,)).fetchone()
            return row is not None and (thread_id is None or row['thread_id'] == thread_id)

    def bind_codex(self, member, thread_id, rollout):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            team = self.teams.member_team(member, db)
            if not team or not db.execute("""SELECT 1 FROM roster WHERE team_id=? AND member=?
                                             AND (product IS NULL OR product='codex')""",
                                          (team['id'], member)).fetchone():
                raise Conflict('请选择 Codex 成员。')
            previous = db.execute('SELECT thread_id FROM codex_bindings WHERE member=?', (member,)).fetchone()
            if previous and previous[0] == thread_id:
                return
            if db.execute("SELECT 1 FROM tasks WHERE recipient=? AND status='running'", (member,)).fetchone():
                raise Conflict('请等这个成员正在执行的旧任务结束，再更换绑定。')
            if db.execute("SELECT 1 FROM team_messages WHERE recipient=? AND delivered='submitting'",
                          (member,)).fetchone():
                raise Conflict('该原会话正在接收消息，请稍后再换绑。')
            if db.execute('SELECT 1 FROM codex_bindings WHERE thread_id=? AND member<>?',
                          (thread_id, member)).fetchone():
                raise Conflict('这个 Codex 聊天已绑定其他成员。')
            db.execute('''INSERT INTO codex_bindings(member,thread_id,rollout) VALUES (?,?,?)
                ON CONFLICT(member) DO UPDATE SET thread_id=excluded.thread_id,rollout=excluded.rollout''',
                (member, thread_id, str(rollout)))
            db.execute('UPDATE member_controls SET instance=NULL,rebind_code=NULL,claude_session_id=NULL WHERE name=?',
                       (member,))
            db.execute("UPDATE roster SET product='codex' WHERE team_id=? AND member=?", (team['id'], member))
            self.teams.bump(db, team['id'])

    def codex_jobs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT j.* FROM codex_jobs j
                JOIN tasks t ON t.task_id=j.task_id WHERE t.status='running' ''')]

    def remember_member(self, name):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO member_controls (name) VALUES (?)', (name,))

    def set_paused(self, name, paused):
        with self.connect() as db:
            return db.execute('UPDATE member_controls SET paused = ? WHERE name = ?',
                              (paused, name)).rowcount == 1

    def console_state(self, bindings, port=58767):
        teams = self.teams.list()
        alerts = []
        with self.connect() as db:
            for team in teams:
                team_id = team['id']
                is_peer = team_mode(team) == 'peer'
                if is_peer:
                    team['hooks'] = {'deployed': False, 'observed': {}}
                    team['worktrees'] = []
                    team['team_rules'] = ''
                    team['external_alerts'] = []
                else:
                    team['hooks'] = self.projects.status(db, team_id)
                    team['worktrees'] = self.projects.worktree_status(db, team)
                    team['team_rules'] = load_team_rules(team['workspace'])
                    team_alerts = self.docs.detect_external_changes(db, team)
                    team['external_alerts'] = team_alerts
                    alerts.extend(team_alerts)
                team['activity'] = [dict(row) for row in db.execute('''
                    SELECT m.id,m.sender_name AS sender,COALESCE(r.alias,m.recipient) AS recipient,
                           m.body,COALESCE(m.delivered,'queued') AS status
                    FROM team_messages m LEFT JOIN roster r ON r.team_id=m.team_id AND r.member=m.recipient
                    WHERE m.team_id=? ORDER BY m.rowid DESC LIMIT 30
                ''', (team_id,))]
                team['legacy_tasks'] = [dict(row) for row in db.execute('''
                    SELECT t.task_id, COALESCE(r.alias,t.recipient) AS recipient, t.status,
                           t.acknowledged,t.instruction,t.result
                    FROM tasks t LEFT JOIN roster r ON r.team_id=t.team_id AND r.member=t.recipient
                    WHERE t.team_id=? ORDER BY t.rowid DESC LIMIT 30
                ''', (team_id,))]
                team['members'] = self.teams.roster(db, team, bindings)
                for member in team['members']:
                    control = db.execute('''SELECT m.paused,
                        (m.instance IS NOT NULL OR m.claude_session_id IS NOT NULL) AS desktop_bound,cb.thread_id
                        FROM member_controls m LEFT JOIN codex_bindings cb ON m.name=cb.member WHERE m.name=?''',
                                         (member['member'],)).fetchone()
                    if control:
                        member.update(dict(control))
        return {'teams': teams, 'alerts': alerts, 'global_access': global_access_status(port)}

    def finish(self, task_id, status, result, session_id=None):
        with self.connect() as db:
            db.execute('''
                UPDATE tasks SET status = ?, result = ?, session_id = ? WHERE task_id = ?
            ''', (status, result, session_id, task_id))

    def inbox(self, team_id=1):
        with self.connect() as db:
            task = db.execute('''
                SELECT t.task_id, t.status, t.result, t.session_id, COALESCE(r.alias,t.recipient) AS "from"
                FROM tasks t LEFT JOIN roster r ON r.team_id=t.team_id AND r.member=t.recipient
                WHERE t.team_id=? AND t.acknowledged=0 AND t.result IS NOT NULL ORDER BY t.rowid LIMIT 1
            ''', (team_id,)).fetchone()
        if task is None:
            return None
        return dict(task) | {'message_id': f"result:{task['task_id']}"}

    def interrupt_member(self, recipient, instance):
        with self.connect() as db:
            db.execute('''
                UPDATE tasks SET status = 'interrupted', result = ?
                WHERE recipient = ? AND worker_instance = ? AND status = 'running'
            ''', ('The Desktop connection expired before Hub received a result.', recipient, instance))

    def finish_member(self, recipient, instance, task_id, status, result):
        with self.connect() as db:
            changed = db.execute('''
                UPDATE tasks SET status = ?, result = ?
                WHERE task_id = ? AND recipient = ? AND worker_instance = ? AND status = 'running'
            ''', (status, result, task_id, recipient, instance))
            if changed.rowcount:
                return True
            task = db.execute('''
                SELECT status, result FROM tasks
                WHERE task_id = ? AND recipient = ? AND worker_instance = ?
            ''', (task_id, recipient, instance)).fetchone()
            return task is not None and tuple(task) == (status, result)

    def acknowledge(self, task_id, message_id, team_id=1):
        if message_id != f'result:{task_id}':
            return False
        with self.connect() as db:
            return db.execute('''
                UPDATE tasks SET acknowledged = 1 WHERE task_id = ? AND team_id=? AND result IS NOT NULL
            ''', (task_id, team_id)).rowcount == 1

    def acknowledge_reference(self, team_id, ref):
        with self.connect() as db:
            matches = db.execute('''SELECT task_id FROM tasks WHERE team_id=? AND result IS NOT NULL
                AND substr(replace(task_id,'-',''),1,12)=?''', (team_id, ref)).fetchall()
            if len(matches) != 1:
                return False
            db.execute('UPDATE tasks SET acknowledged=1 WHERE task_id=?', (matches[0]['task_id'],))
            return True


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
        super().__init__(address, HubHandler)

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
            elif self.path == '/control/pause':
                body = self.read_json()
                member = required_text(body, 'member', 48)
                if type(body.get('paused')) is not bool:
                    raise ValueError('Expected a boolean paused value.')
                found = self.server.store.set_paused(member, body['paused'])
                self.respond(200 if found else 404, {'updated': found})
            elif self.path == '/control/team':
                with self.server.members_lock:
                    body = self.read_json()
                    result = self.server.store.teams.edit(body.get('action'), body)
                self.respond(200, result)
            elif self.path == '/control/project':
                body = self.read_json()
                with self.server.members_lock:
                    result = self.server.store.projects.change(body.get('team_id'), body.get('action'),
                                                               self.server.server_port)
                self.respond(200, result)
            elif self.path == '/control/global-access':
                body = self.read_json()
                with self.server.members_lock:
                    result = configure_global_access(body.get('action'), self.server.server_port)
                self.respond(200, result)
            elif self.path == '/control/worktrees':
                body = self.read_json()
                team_id = body.get('team_id')
                if type(team_id) is not int:
                    raise ValueError('请选择 Team。')
                with self.server.store.connect() as db:
                    team = self.server.store.teams.get(db, team_id)
                    if not team:
                        raise Conflict('该 Team 不可用。')
                    worktrees = self.server.store.projects.worktree_status(db, team)
                self.respond(200, {'worktrees': worktrees})
            elif self.path == '/control/worktree-ai':
                body = self.read_json()
                team_id = body.get('team_id')
                if type(team_id) is not int:
                    raise ValueError('请选择 Team。')
                with self.server.store.connect() as db:
                    team = self.server.store.teams.get(db, team_id)
                    if not team:
                        raise Conflict('该 Team 不可用。')
                    workspace = team['workspace']
                result = inspect_worktree_ai(workspace, body.get('worktree', '') or '', body.get('log_path'))
                self.respond(200, result)
            elif self.path == '/control/message/cancel':
                body = self.read_json()
                team_id = body.get('team_id')
                if type(team_id) is not int:
                    raise ValueError('请选择 Team。')
                result = self.server.store.messages.cancel(team_id, required_text(body, 'id', 128))
                self.respond(200, result)
            elif self.path == '/control/message/send':
                body = self.read_json()
                team_id = body.get('team_id')
                if type(team_id) is not int:
                    raise ValueError('请选择 Team。')
                result = self.server.store.messages.send_operator(
                    team_id, required_text(body, 'to', 48), required_text(body, 'text', 12000))
                self.respond(202, result)
            elif self.path == '/control/git-identity/detect':
                body = self.read_json()
                team_id = body.get('team_id')
                workspace = None
                if type(team_id) is int:
                    with self.server.store.connect() as db:
                        team = self.server.store.teams.get(db, team_id)
                        workspace = team['workspace'] if team else None
                self.respond(200, detect_git_identity(workspace))
            elif self.path == '/control/doc':
                body = self.read_json()
                team_id = body.get('team_id')
                if type(team_id) is not int:
                    raise ValueError('请选择 Team。')
                with self.server.store.connect() as db:
                    doc_team = self.server.store.teams.get(db, team_id)
                if doc_team and team_mode(doc_team) == 'peer':
                    raise Conflict('无分工 Team 不管理工作区规范文档。')
                action = body.get('action')
                docs = self.server.store.docs
                if action == 'get':
                    result = docs.get_doc(team_id, required_text(body, 'doc_key', 32), body.get('worktree', '') or '')
                elif action == 'revision':
                    rev_id = body.get('revision_id')
                    if type(rev_id) is not int:
                        raise ValueError('请选择历史版本。')
                    result = docs.get_revision(team_id, rev_id)
                elif action == 'save':
                    result = docs.save_doc(
                        team_id,
                        required_text(body, 'doc_key', 32),
                        body.get('content', ''),
                        worktree=body.get('worktree', '') or '',
                        sync_worktrees=body.get('sync_worktrees'),
                        sync_mode=body.get('sync_mode', 'shared_if_marked'),
                        content_scope=body.get('content_scope', 'full') or 'full',
                        auto_commit=bool(body.get('auto_commit') or body.get('commit_worktrees')),
                        commit_worktrees=body.get('commit_worktrees'),
                        note=str(body.get('note', '') or '')[:120],
                    )
                elif action == 'reset':
                    result = docs.reset_default(team_id, required_text(body, 'doc_key', 32))
                elif action == 'rollback':
                    rev_id = body.get('revision_id')
                    if type(rev_id) is not int:
                        raise ValueError('请选择历史版本。')
                    result = docs.rollback(team_id, rev_id, auto_commit=bool(body.get('auto_commit')))
                elif action == 'accept_external':
                    result = docs.accept_external(
                        team_id, required_text(body, 'doc_key', 32), body.get('worktree', '') or '')
                elif action == 'agents_overview':
                    result = docs.agents_overview(team_id)
                elif action == 'save_shared':
                    result = docs.save_shared_block(
                        team_id,
                        body.get('content', ''),
                        body.get('target_worktrees') or [],
                        commit_worktrees=body.get('commit_worktrees') or [],
                        note=str(body.get('note', '') or '')[:120],
                    )
                elif action == 'search_similar':
                    result = docs.find_similar_clauses(
                        team_id,
                        body.get('query', ''),
                        min_score=body.get('min_score', 0.55),
                        source_worktree=body.get('source_worktree'),
                    )
                elif action == 'batch_remove':
                    result = docs.batch_remove_clauses(
                        team_id,
                        body.get('items') or [],
                        commit_worktrees=body.get('commit_worktrees') or [],
                        note=str(body.get('note', '') or '')[:120],
                    )
                elif action == 'batch_replace':
                    result = docs.batch_replace_clauses(
                        team_id,
                        body.get('items') or [],
                        body.get('replacement', ''),
                        commit_worktrees=body.get('commit_worktrees') or [],
                        note=str(body.get('note', '') or '')[:120],
                    )
                elif action == 'preview_append':
                    result = docs.preview_batch_append(
                        team_id,
                        body.get('text', ''),
                        body.get('worktrees') or [],
                        position=body.get('position', 'before_shared') or 'before_shared',
                        section_heading=body.get('section_heading', '') or '',
                    )
                elif action == 'batch_append':
                    result = docs.batch_append(
                        team_id,
                        body.get('text', ''),
                        body.get('worktrees') or [],
                        position=body.get('position', 'before_shared') or 'before_shared',
                        section_heading=body.get('section_heading', '') or '',
                        commit_worktrees=body.get('commit_worktrees') or [],
                        note=str(body.get('note', '') or '')[:120],
                    )
                else:
                    raise ValueError('未知文档操作。')
                self.respond(200, result)
            elif self.path == '/control/antigravity':
                body = self.read_json()
                with self.server.members_lock:
                    code = self.server.store.teams.prepare_antigravity(
                        body.get('team_id'), required_text(body, 'member', 48), body.get('label', ''))
                self.respond(200, {'code': code})
            elif self.path == '/control/acp':
                body = self.read_json()
                with self.server.members_lock:
                    code = self.server.store.teams.prepare_acp(
                        body.get('team_id'), required_text(body, 'member', 48), body.get('label', ''))
                self.respond(200, {'code': code})
            elif self.path == '/control/agy-cli':
                body = self.read_json()
                with self.server.members_lock:
                    code = self.server.store.teams.prepare_agy_cli(
                        body.get('team_id'), required_text(body, 'member', 48), body.get('label', ''))
                self.respond(200, {'code': code})
            elif self.path.startswith('/antigravity/'):
                self.antigravity_request()
            elif self.path.startswith('/acp/'):
                self.acp_request()
            elif self.path.startswith('/agy-cli/'):
                self.agy_cli_request()
            elif self.path == '/control/rebind':
                member = required_text(self.read_json(), 'member', 48)
                with self.server.members_lock:
                    code = self.server.store.teams.prepare_binding(member)
                self.respond(200, {'code': code})
            elif self.path == '/control/codex-bind':
                body = self.read_json()
                member = required_text(body, 'member', 48)
                thread, path = locate_thread(required_text(body, 'thread_id', 36))
                with self.server.members_lock:
                    self.server.store.bind_codex(member, thread, path)
                    self.server.members.pop(member, None)
                self.respond(200, {'bound': True, 'thread_id': thread})
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
        path = Path(__file__).resolve().parent / 'console' / filename
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
        store = self.server.store
        team_id = team['id']
        if action in ('send', 'dispatch'):
            name = required_text(body, 'to', 48)
            instruction = required_text(body, 'instruction' if action == 'dispatch' else 'text', 12000)
            request_id = body.get('request_id')
            if request_id is not None:
                request_id = required_text(body, 'request_id', 128)
            response = store.messages.send(team_id, member, name, instruction, request_id)
            self.respond(202, response)
        elif action == 'roster':
            self.server.expire_members()
            is_peer = team_mode(team) == 'peer'
            with store.connect() as db:
                roster = store.teams.roster(db, team, self.server.members)
                if is_peer:
                    rows = [
                        {'name': slot['name'], 'product': slot['product'],
                         'note': slot.get('note', ''), 'online': slot['online']}
                        for slot in roster
                    ]
                else:
                    fields = ('name', 'product', 'role', 'role_revision', 'model', 'worktree', 'responsibility', 'online')
                    rows = [{key: (display_worktree(slot[key]) if key == 'worktree' else slot[key]) for key in fields}
                            for slot in roster]
            self.respond(200, {'team': team['name'], 'mode': 'peer' if is_peer else 'structured', 'members': rows})
        elif action == 'whoami':
            with store.connect() as db:
                identity = store.teams.member_identity(db, team, member)
            if not identity:
                raise Conflict('当前会话对应的成员已移出 Team。')
            identity.pop('worktree_raw', None)
            self.respond(200, identity)
        elif action == 'received':
            ok = store.messages.received(team_id, member, required_text(body, 'id', 128), body.get('revision'))
            self.respond(200 if ok else 409, {'accepted': ok})
        else:
            self.respond(404, {'error': 'Unknown member operation.'})

    def antigravity_request(self):
        action = self.path.split('/')[-1]
        body = self.read_json()
        store = self.server.store
        with self.server.members_lock:
            if action == 'open':
                conversation_id = str(uuid.UUID(required_text(body, 'conversation_id', 36)))
                proof = secrets.token_urlsafe(24)
                self.server.antigravity_proofs[proof] = (conversation_id, time.monotonic() + 300)
                self.respond(200, {'proof': proof})
                return
            if action in ('bind', 'poll', 'restore'):
                instance = str(uuid.UUID(required_text(body, 'instance', 36)))
                if action in ('bind', 'restore'):
                    key = body.get('proof')
                    proof = self.server.antigravity_proofs.get(key) if isinstance(key, str) else None
                    conversation_id = proof[0] if proof and proof[1] > time.monotonic() else None
                    if action == 'restore':
                        result = (store.teams.restore_antigravity(instance, conversation_id) if conversation_id else
                                  {'bound': False})
                        if result['bound']:
                            self.server.antigravity_proofs.pop(key, None)
                        self.respond(200, result)
                        return
                    if key is not None and not conversation_id:
                        existing = store.teams.antigravity_window(instance=instance)
                        conversation_id = existing['conversation_id'] if existing else None
                        if not conversation_id:
                            raise Conflict('侧栏验证已过期。请关闭侧栏，从原聊天重新打开。')
                if action == 'bind':
                    code = required_text(body, 'code', 10).strip().lower()
                    result = store.teams.bind_antigravity(instance, code, conversation_id)
                    if conversation_id:
                        self.server.antigravity_proofs.pop(key, None)
                    self.respond(200, result)
                    return
                team = store.teams.antigravity_window(instance=instance)
                if not team:
                    self.respond(200, {'bound': False})
                    return
                is_peer = team_mode(team) == 'peer'
                self.respond(200, {'bound': True, 'team': team['name'], 'mode': 'peer' if is_peer else 'structured',
                                   'name': team['alias'],
                                   'label': team['label'], 'route': team['route'], 'active': True,
                                   'role': 'peer' if is_peer else team['roles']['antigravity'],
                                   'hook_observed': False if is_peer else store.projects.observed(team['id'], 'antigravity'),
                                   'delivery': store.messages.next(team['id'], team['member']),
                                   'message': store.inbox(team['id']) if team['captain'] == team['member'] else None})
                return
            route = required_text(body, 'route', 48)
            team = store.teams.antigravity_window(route=route) or store.teams.agy_cli_member(route=route)
            if not team:
                raise Conflict('这个 Antigravity 会话的绑定已失效。')
            if action == 'ack':
                ref = required_text(body, 'ref', 12)
                if team['captain'] != team['member'] or not store.acknowledge_reference(team['id'], ref):
                    raise Conflict('该旧结果不属于此窗口。')
                self.respond(200, {'acknowledged': True})
            else:
                self.peer_request(action, team, team['member'], body)

    def _acp_poll_payload(self, route=None, instance=None):
        store = self.server.store
        team = store.teams.acp_member(route=route, instance=instance, touch=True)
        if not team:
            return {'bound': False}
        is_peer = team_mode(team) == 'peer'
        return {
            'bound': True,
            'team': team['name'],
            'mode': 'peer' if is_peer else 'structured',
            'name': team['alias'],
            'label': team['label'],
            'route': team['route'],
            'delivery': store.messages.next(team['id'], team['member']),
        }

    def acp_request(self):
        action = self.path.split('/')[-1]
        body = self.read_json()
        store = self.server.store
        port = self.server.server_port
        with self.server.members_lock:
            if action == 'bind':
                session_id = required_text(body, 'session_id', 128)
                instance = required_text(body, 'instance', 64)
                code = required_text(body, 'code', 10).strip().lower()
                label = body.get('label')
                result = store.teams.bind_acp(session_id, instance, code, label)
                result['mcp'] = acp_mcp_descriptor(port, result['route'])
                self.respond(200, result)
                return
            if action == 'restore':
                session_id = required_text(body, 'session_id', 128)
                instance = required_text(body, 'instance', 64)
                result = store.teams.restore_acp(session_id, instance)
                if result.get('bound') and result.get('route'):
                    result['mcp'] = acp_mcp_descriptor(port, result['route'])
                self.respond(200, result)
                return
            if action == 'unbind':
                route = required_text(body, 'route', 48)
                self.respond(200, store.teams.unbind_acp(route))
                return
            if action == 'poll':
                if isinstance(body.get('routes'), list):
                    routes = body['routes']
                    if len(routes) > 50 or any(not isinstance(r, str) or not 1 <= len(r) <= 48 for r in routes):
                        raise ValueError('routes 必须是最多 50 条有效 route 字符串的列表。')
                    self.respond(200, {'results': {r: self._acp_poll_payload(route=r) for r in routes}})
                    return
                if body.get('route'):
                    self.respond(200, self._acp_poll_payload(route=required_text(body, 'route', 48)))
                    return
                if body.get('instance'):
                    self.respond(200, self._acp_poll_payload(instance=required_text(body, 'instance', 64)))
                    return
                raise ValueError('请提供 route、routes 或 instance。')
            route = required_text(body, 'route', 48)
            team = store.teams.acp_member(route=route, touch=True)
            if not team:
                raise Conflict('这个 AGY ACP 会话的绑定已失效。')
            if action == 'context':
                paths = body.get('paths')
                if paths is not None and (not isinstance(paths, list) or len(paths) > 20):
                    raise ValueError('paths 格式不正确。')
                event = body.get('event', 'prompt')
                ctx = store.projects.acp_context(team, team['member'], team['session_id'], event=event, paths=paths)
                self.respond(200, ctx)
                return
            self.peer_request(action, team, team['member'], body)

    def agy_cli_request(self):
        action = self.path.split('/')[-1]
        body = self.read_json()
        store = self.server.store
        port = self.server.server_port
        with self.server.members_lock:
            if action == 'bind':
                code = required_text(body, 'code', 10).strip().lower()
                session_id = body.get('session_id')
                if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 128):
                    raise ValueError('session_id 格式不正确。')
                result = store.teams.bind_agy_cli(code, session_id=session_id, label=body.get('label'))
                result['wait_command'] = agy_cli_wait_command(port, result['route'])
                result['instruction'] = '请立即在后台运行 wait_command 启动收件监听；后续每次收到队友消息被唤醒并处理回复后，请重新在后台运行同一条 wait_command。'
                self.respond(200, result)
                return
            if action == 'restore':
                session_id = body.get('session_id')
                if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
                    self.respond(200, {'bound': False})
                    return
                result = store.teams.restore_agy_cli(session_id)
                if result.get('bound') and result.get('route'):
                    result['wait_command'] = agy_cli_wait_command(port, result['route'])
                    result['instruction'] = '已自动恢复绑定。请立即在后台运行 wait_command 恢复自动收件监听；后续每次收到队友消息被唤醒并处理回复后，请重新在后台运行同一条 wait_command。'
                self.respond(200, result)
                return
            if action == 'poll':
                route = body.get('route')
                session_id = body.get('session_id')
                team = (store.teams.agy_cli_member(route=route, touch=True) if isinstance(route, str) and route
                        else store.teams.agy_cli_member(session_id=session_id, touch=True) if isinstance(session_id, str) and session_id
                        else None)
                if not team:
                    self.respond(200, {'bound': False})
                    return
                is_peer = team_mode(team) == 'peer'
                delivery = store.messages.next(team['id'], team['member'])
                if delivery and body.get('accept') is True:
                    store.messages.received(team['id'], team['member'], delivery['id'], delivery['revision'])
                self.respond(200, {
                    'bound': True,
                    'team': team['name'],
                    'mode': 'peer' if is_peer else 'structured',
                    'name': team['alias'],
                    'label': team['label'],
                    'route': team['route'],
                    'delivery': delivery,
                })
                return
            route = required_text(body, 'route', 48)
            team = store.teams.agy_cli_member(route=route, touch=True)
            if not team:
                raise Conflict('这个 AGY CLI 会话的绑定已失效。')
            self.peer_request(action, team, team['member'], body)

    def desktop_request(self):
        action = self.path.split('/')[-1]
        body = self.read_json()
        instance = str(uuid.UUID(required_text(body, 'instance', 36)))
        server = self.server
        store = server.store
        if action == 'restore':
            session_id = str(uuid.UUID(required_text(body, 'session_id', 36)))
            with server.members_lock:
                server.expire_members()
                if any(owner == instance for owner, _, _ in server.members.values()):
                    raise Conflict('这个会话已经绑定另一个成员。')
                restored = store.teams.restore_desktop(instance, session_id)
                if not restored.get('bound'):
                    self.respond(200, {'bound': False})
                    return
                member = restored['member']
                previous = server.members.get(member)
                if previous and previous[0] != instance:
                    store.interrupt_member(member, previous[0])
                server.members[member] = (instance, time.monotonic(), True)
                self.respond(200, restored)
                return
        member = required_text(body, 'member', 48)
        with server.members_lock:
            server.expire_members()
            binding = server.members.get(member)
            team = store.teams.member_team(member)
            if not team or store.is_codex_binding(member):
                raise Conflict('请选择未绑定其他产品的 Claude Code 成员。')
            if action == 'bind':
                if any(name != member and owner == instance for name, (owner, _, _) in server.members.items()):
                    raise Conflict('这个会话已经绑定另一个成员。')
                raw_session = body.get('session_id')
                session_id = str(uuid.UUID(required_text(body, 'session_id', 36))) if raw_session is not None else None
                store.teams.bind(member, instance, body.get('code'), session_id)
                if binding and binding[0] != instance:
                    store.interrupt_member(member, binding[0])
                server.members[member] = (instance, time.monotonic(), True)
                self.respond(200, {'bound': True, 'member': member, 'team': team['name']})
                return
            if not binding or binding[0] != instance:
                raise Conflict('当前 Desktop 绑定已失效。')
            server.members[member] = (instance, time.monotonic(), True)
            if action == 'poll':
                task = store.claim(member, instance) if body.get('ready') is True else None
                message = store.inbox(team['id']) if team['captain'] == member else None
                self.respond(200, {'task': dict(task) if task else None, 'message': message,
                                  'delivery': store.messages.next(team['id'], member)})
            elif action == 'complete':
                task_id = required_text(body, 'task_id', 36)
                status = body.get('status')
                if status not in ('completed', 'interrupted'):
                    raise ValueError('Expected completed or interrupted.')
                result = required_text(body, 'result', 12000)
                if not store.finish_member(member, instance, task_id, status, result):
                    raise Conflict('这个旧任务不属于此会话。')
                self.respond(200, {'accepted': True})
            elif action == 'ack':
                if team['captain'] != member or not store.acknowledge(
                        body.get('task_id'), body.get('message_id'), team['id']):
                    raise Conflict('该旧结果不属于此窗口。')
                self.respond(200, {'acknowledged': True})
            else:
                self.peer_request(action, team, member, body)

    def project_request(self):
        action = self.path.split('/')[-1]
        body = self.read_json()
        server = self.server
        store = server.store
        with server.members_lock:
            if action == 'context':
                self.respond(200, store.projects.context(body.get('team_id'), body.get('product'),
                                                         body.get('session_id'), body.get('event'),
                                                         body.get('paths')))
                return
            tickets = server.tool_tickets
            if action == 'ticket':
                session_id = required_text(body, 'session_id', 36)
                with store.connect() as db:
                    if body.get('scope') != 'global':
                        if not store.projects.deployed_team(db, body.get('team_id'), body.get('paths')):
                            raise Conflict('当前项目尚未部署 Team Hub Hook。')
                    row = db.execute('SELECT member FROM codex_bindings WHERE thread_id=?', (session_id,)).fetchone()
                if not row or not store.teams.member_team(row[0]):
                    raise Conflict('请先在管理台绑定此 Codex 聊天。')
                tool = required_text(body, 'tool', 20)
                if tool not in ('send', 'roster', 'whoami'):
                    raise ValueError('未知 Team 工具。')
                key = uuid.uuid4().hex
                tickets[key] = (row[0], tool, time.monotonic()+15, session_id)
                self.respond(200, {'ticket': key})
                return
            if action == 'tool':
                ticket = tickets.pop(required_text(body, 'ticket', 32), None)
                if ticket is None or ticket[2] <= time.monotonic() or not store.is_codex_binding(ticket[0], ticket[3]):
                    raise Conflict('请确认项目 Hook 已启用、并绑定此 Codex 聊天。')
                team = store.teams.member_team(ticket[0])
                if not team:
                    raise Conflict('此成员当前没有 Team。')
                self.peer_request(ticket[1], team, ticket[0], body)
                return
            self.respond(404, {'error': 'Unknown project operation.'})


def required_text(body, key, limit):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{key} must be a non-empty string of at most {limit} characters.')
    value.encode('utf-8')
    return value


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
        codex_runner = CodexRunner(store, server.members_lock)
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
