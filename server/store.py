"""SQLite TaskStore coordinating tasks, member controls, Codex bindings, and sub-stores."""

import sqlite3
import uuid
from contextlib import contextmanager

from docs_store import DocStore
from messages import Messages
from server.global_access import global_access_status
from teams import Conflict, Teams, team_mode
from workspace import Projects, load_team_rules


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
            db.execute("UPDATE team_messages SET delivered='unconfirmed' WHERE delivered IN ('submitting','cancel_requested')")
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
            if db.execute("SELECT 1 FROM team_messages WHERE recipient=? AND delivered IN ('submitting','cancel_requested')",
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
