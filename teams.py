"""Team configuration, custom rosters, and scoped native window bindings."""

import json
import re
import secrets
import time
from pathlib import Path


ANTIGRAVITY = 'antigravity'
PRODUCT_ROLES = {
    'antigravity': 'captain',
    'claude': 'worker',
    'codex': 'reviewer+helper',
    'agy_acp': 'worker',
    'agy_cli': 'worker',
}
ROLES = ('captain', 'worker', 'reviewer', 'helper', 'reviewer+helper')
TEAM_MODES = ('structured', 'peer')
PEER_ROLE = 'peer'
UNBOUNDED_WORKTREE_LABEL = '项目内不限（注意避免多线写入冲突）'


def team_mode(row):
    try:
        value = row['mode']
    except (KeyError, IndexError, TypeError):
        value = None
    return value if value in TEAM_MODES else 'structured'


def display_worktree(value):
    return value if value else UNBOUNDED_WORKTREE_LABEL


class Conflict(ValueError):
    pass


def short_text(value, limit, label):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or not value.strip().isprintable():
        raise ValueError(f'请输入 1 到 {limit} 个可见字符作为{label}。')
    value = value.strip()
    value.encode('utf-8')
    return value


def optional_text(value, limit, label):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError(f'{label}格式不正确。')
    cleaned = value.strip()
    if not cleaned:
        return ''
    if len(cleaned) > limit or not cleaned.isprintable():
        raise ValueError(f'{label}长度不能超过 {limit} 个可见字符。')
    cleaned.encode('utf-8')
    return cleaned


def normalize_worktree(value):
    cleaned = optional_text(value, 200, '负责工作树').replace('\\', '/').strip('/')
    if not cleaned or cleaned == '.':
        return ''
    parts = [part for part in cleaned.split('/') if part]
    if any(part in ('.', '..') or ':' in part for part in parts):
        raise ValueError('负责工作树必须是工作区内的相对路径。')
    return '/'.join(parts)


class Teams:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, captain TEXT NOT NULL DEFAULT 'antigravity',
                    active INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                    workspace TEXT, roles TEXT, revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_team ON teams (active) WHERE active = 1;
                CREATE TABLE IF NOT EXISTS roster (
                    team_id INTEGER NOT NULL, member TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'worker',
                    alias TEXT, product TEXT, responsibility TEXT NOT NULL DEFAULT '',
                    seen_revision INTEGER NOT NULL DEFAULT 0,
                    role_revision INTEGER NOT NULL DEFAULT 1,
                    model TEXT NOT NULL DEFAULT '',
                    worktree TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (team_id, member)
                );
                CREATE TABLE IF NOT EXISTS antigravity_members (
                    team_id INTEGER NOT NULL, member TEXT NOT NULL,
                    instance TEXT UNIQUE, route TEXT UNIQUE, label TEXT, code TEXT UNIQUE,
                    pending_label TEXT, last_seen REAL, PRIMARY KEY (team_id, member)
                );
                CREATE TABLE IF NOT EXISTS acp_members (
                    team_id INTEGER NOT NULL, member TEXT NOT NULL,
                    session_id TEXT UNIQUE, instance TEXT, route TEXT UNIQUE,
                    label TEXT, code TEXT UNIQUE, pending_label TEXT, last_seen REAL,
                    PRIMARY KEY (team_id, member)
                );
                CREATE TABLE IF NOT EXISTS agy_cli_members (
                    team_id INTEGER NOT NULL, member TEXT NOT NULL,
                    session_id TEXT UNIQUE, route TEXT UNIQUE,
                    label TEXT, code TEXT UNIQUE, pending_label TEXT, last_seen REAL,
                    PRIMARY KEY (team_id, member)
                );
            """)
            db.execute('BEGIN IMMEDIATE')
            self.add_columns(db, 'antigravity_members', {'conversation_id': 'TEXT'})
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS antigravity_native_conversation
                          ON antigravity_members(conversation_id) WHERE conversation_id IS NOT NULL''')
            self.add_columns(db, 'teams', {'workspace': 'TEXT', 'roles': 'TEXT',
                                          'revision': 'INTEGER NOT NULL DEFAULT 0',
                                          'git_author_name': "TEXT NOT NULL DEFAULT ''",
                                          'git_author_email': "TEXT NOT NULL DEFAULT ''",
                                          'mode': "TEXT NOT NULL DEFAULT 'structured'"})
            self.add_columns(db, 'member_controls', {
                'instance': 'TEXT', 'rebind_code': 'TEXT', 'claude_session_id': 'TEXT'})
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS claude_native_session
                          ON member_controls(claude_session_id) WHERE claude_session_id IS NOT NULL''')
            previous = self.add_columns(db, 'roster', {
                'alias': 'TEXT', 'product': 'TEXT', 'responsibility': "TEXT NOT NULL DEFAULT ''",
                'seen_revision': 'INTEGER NOT NULL DEFAULT 0',
                'role_revision': 'INTEGER NOT NULL DEFAULT 1',
                'model': "TEXT NOT NULL DEFAULT ''",
                'worktree': "TEXT NOT NULL DEFAULT ''"})
            if 'product' not in previous:
                db.execute("UPDATE roster SET product='codex' WHERE member IN (SELECT member FROM codex_bindings)")
                db.execute("""UPDATE roster SET product='claude' WHERE member='claude' OR member IN
                              (SELECT name FROM member_controls WHERE instance IS NOT NULL)""")
                self.archive_test_team(db)
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='antigravity_windows'").fetchone():
                    self.migrate_windows(db)
            if 'role_revision' not in previous:
                self.migrate_member_roles(db)

    def migrate_member_roles(self, db):
        for team in db.execute('SELECT * FROM teams WHERE archived=0').fetchall():
            roles = self.view(team)['roles']
            for seat in db.execute('SELECT member,product,role FROM roster WHERE team_id=?', (team['id'],)).fetchall():
                effective = roles.get(seat['product'], seat['role'])
                if effective in ROLES and effective != seat['role']:
                    db.execute('UPDATE roster SET role=? WHERE team_id=? AND member=?',
                               (effective, team['id'], seat['member']))

    @staticmethod
    def add_columns(db, table, definitions):
        existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, definition in definitions.items():
            if name not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        return existing

    @staticmethod
    def archive_test_team(db):
        team = db.execute('SELECT * FROM teams WHERE id=1').fetchone()
        if not team or team['name'] != '默认团队' or team['captain'] != ANTIGRAVITY:
            return
        seats = db.execute('SELECT member,role,alias FROM roster WHERE team_id=1').fetchall()
        test_names = {'claude', 'ds-main-worker', 'ds-voice-worker'}
        if any(row['member'] not in test_names or row['role'] != 'worker' or
               row['alias'] not in (None, row['member']) for row in seats):
            return
        if db.execute("""SELECT 1 FROM tasks WHERE team_id=1
                         AND (status IN ('queued','running') OR (result IS NOT NULL AND acknowledged=0))""").fetchone():
            return
        db.execute('UPDATE teams SET archived=1,active=0 WHERE id=1')

    def migrate_windows(self, db):
        for row in db.execute("""SELECT w.*,t.name,t.captain FROM antigravity_windows w JOIN teams t
                                 ON t.id=w.team_id WHERE t.archived=0""").fetchall():
            name = (row['label'] or row['pending_label'] or row['name'])[:48]
            if self.lookup(db, row['team_id'], name):
                name = f"Antigravity · {row['team_id']}"
            member = self.new_member(db)
            db.execute("""INSERT INTO roster (team_id,member,alias,role,product)
                          VALUES (?,?,?,'captain','antigravity')""", (row['team_id'], member, name))
            db.execute("""INSERT INTO antigravity_members
                          (team_id,member,instance,route,label,code,pending_label,last_seen)
                          VALUES (?,?,?,?,?,?,?,?)""",
                       (row['team_id'], member, row['instance'], row['route'], row['label'],
                        row['code'], row['pending_label'], row['last_seen']))
            if row['captain'] == ANTIGRAVITY:
                db.execute('UPDATE teams SET captain=? WHERE id=?', (member, row['team_id']))

    @staticmethod
    def view(row):
        roles = row['roles']
        mode = team_mode(row)
        return dict(row) | {
            'mode': mode,
            'roles': PRODUCT_ROLES | (roles if isinstance(roles, dict) else json.loads(roles or '{}')),
        }

    @staticmethod
    def get(db, team_id):
        return db.execute('SELECT * FROM teams WHERE id=? AND archived=0', (team_id,)).fetchone()

    @staticmethod
    def bump(db, team_id):
        db.execute('UPDATE teams SET revision=revision+1 WHERE id=?', (team_id,))

    @staticmethod
    def lookup(db, team_id, alias):
        return next((key for key, name in db.execute(
            'SELECT member,COALESCE(alias,member) FROM roster WHERE team_id=?', (team_id,))
            if name.casefold() == alias.casefold()), None)

    def active(self):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM teams WHERE archived=0 ORDER BY active DESC,id LIMIT 1').fetchone()
            return self.view(row) if row else None

    def list(self):
        with self.store.connect() as db:
            return [self.view(team) | {'members': self.roster(db, team)}
                    for team in db.execute('SELECT * FROM teams WHERE archived=0 ORDER BY id').fetchall()]

    def roster(self, db, team, connected=()):
        mode = team_mode(team)
        now_cutoff = time.time() - 35
        return [dict(row) | {
            'role': PEER_ROLE if mode == 'peer' else (
                row['role'] if row['role'] in ROLES else self.view(team)['roles'].get(row['product'], 'worker')
            ),
            'note': row['responsibility'] if mode == 'peer' else '',
            'online': bool(row['window_online'] or row['acp_online'] or row['cli_online'] or row['member'] in connected) if not row['codex_bound'] else None
        } for row in db.execute("""
            SELECT r.member,COALESCE(r.alias,r.member) AS name,r.product,r.role,r.role_revision,
                   r.model,r.worktree,r.responsibility,
                   w.label AS window_label,w.instance IS NOT NULL AS window_bound,
                   COALESCE(w.last_seen>=?,0) AS window_online,
                   a.label AS acp_label,a.session_id IS NOT NULL AS acp_bound,
                   COALESCE(a.last_seen>=?,0) AS acp_online,
                   cli.label AS cli_label,cli.route IS NOT NULL AS cli_bound,
                   COALESCE(cli.last_seen>=?,0) AS cli_online,
                   cb.thread_id IS NOT NULL AS codex_bound
            FROM roster r LEFT JOIN antigravity_members w ON w.team_id=r.team_id AND w.member=r.member
            LEFT JOIN acp_members a ON a.team_id=r.team_id AND a.member=r.member
            LEFT JOIN agy_cli_members cli ON cli.team_id=r.team_id AND cli.member=r.member
            LEFT JOIN codex_bindings cb ON cb.member=r.member
            WHERE r.team_id=? ORDER BY name COLLATE NOCASE
        """, (now_cutoff, now_cutoff, now_cutoff, team['id']))]

    def member_identity(self, db, team, member):
        row = db.execute("""
            SELECT r.member,COALESCE(r.alias,r.member) AS name,r.product,r.role,r.role_revision,
                   r.model,r.worktree,r.responsibility
            FROM roster r WHERE r.team_id=? AND r.member=?
        """, (team['id'], member)).fetchone()
        if row is None:
            return None
        mode = team_mode(team)
        if mode == 'peer':
            return {
                'team': team['name'],
                'mode': 'peer',
                'member': row['member'],
                'name': row['name'],
                'product': row['product'],
                'note': row['responsibility'],
            }
        return {
            'team': team['name'],
            'mode': 'structured',
            'member': row['member'],
            'name': row['name'],
            'product': row['product'],
            'role': row['role'],
            'role_revision': row['role_revision'],
            'model': row['model'],
            'worktree': display_worktree(row['worktree']),
            'worktree_raw': row['worktree'],
            'responsibility': row['responsibility'],
        }

    def member_team(self, member, db=None):
        if db is None:
            with self.store.connect() as db:
                return self.member_team(member, db)
        rows = db.execute("""SELECT t.* FROM roster r JOIN teams t ON t.id=r.team_id
                             WHERE r.member=? AND t.archived=0""", (member,)).fetchall()
        if len(rows) > 1:
            raise Conflict('这个旧席位关联了多个 Team。请在目标 Team 创建独立成员后再绑定。')
        return self.view(rows[0]) if rows else None

    def prepare_antigravity(self, team_id, member, label):
        if not isinstance(label, str) or len(label.strip()) > 80:
            raise ValueError('请输入不超过 80 字的窗口备注。')
        with self.store.connect() as db:
            seat = db.execute("""SELECT COALESCE(r.alias,r.member) FROM roster r JOIN teams t ON t.id=r.team_id
                                 WHERE r.team_id=? AND r.member=? AND r.product='antigravity' AND t.archived=0""",
                              (team_id, member)).fetchone()
            if not seat:
                raise Conflict('请先为此 Team 创建 Antigravity 成员。')
            code = secrets.token_hex(5)
            db.execute('''INSERT INTO antigravity_members(team_id,member,code,pending_label) VALUES (?,?,?,?)
                          ON CONFLICT(team_id,member) DO UPDATE SET code=excluded.code,pending_label=excluded.pending_label''',
                       (team_id, member, code, label.strip() or seat[0]))
            return code

    def bind_antigravity(self, instance, code, conversation_id=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.id,t.name,w.member,w.pending_label,r.alias FROM antigravity_members w
                                JOIN teams t ON t.id=w.team_id JOIN roster r ON r.team_id=t.id AND r.member=w.member
                                WHERE w.code=? AND t.archived=0''', (code,)).fetchone()
            if row is None:
                raise Conflict('绑定代码无效或已使用。')
            self.check_antigravity_identity(db, row, instance, conversation_id)
            route = secrets.token_urlsafe(18)
            db.execute('''UPDATE antigravity_members SET instance=?,route=?,label=pending_label,
                          code=NULL,pending_label=NULL,last_seen=?,conversation_id=? WHERE team_id=? AND member=?''',
                       (instance, route, time.time(), conversation_id, row['id'], row['member']))
            self.bump(db, row['id'])
            return {'team': row['name'], 'name': row['alias'], 'label': row['pending_label'], 'route': route}

    @staticmethod
    def check_antigravity_identity(db, row, instance, conversation_id):
        other = db.execute('''SELECT 1 FROM antigravity_members WHERE (instance=? OR conversation_id=?)
                              AND (team_id<>? OR member<>?)''',
                           (instance, conversation_id, row['id'], row['member'])).fetchone()
        if other:
            raise Conflict('该原聊天或侧栏已绑定另一位成员。')

    def restore_antigravity(self, instance, conversation_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.id,w.member,w.route FROM antigravity_members w
                                JOIN teams t ON t.id=w.team_id JOIN roster r ON r.team_id=t.id AND r.member=w.member
                                WHERE w.conversation_id=? AND w.route IS NOT NULL AND t.archived=0''',
                             (conversation_id,)).fetchone()
            if row is None:
                return {'bound': False}
            self.check_antigravity_identity(db, row, instance, conversation_id)
            db.execute('UPDATE antigravity_members SET instance=?,last_seen=? WHERE team_id=? AND member=?',
                       (instance, time.time(), row['id'], row['member']))
            return {'bound': True, 'route': row['route']}

    def antigravity_window(self, *, instance=None, route=None):
        with self.store.connect() as db:
            row = db.execute('''SELECT t.*,w.member,w.label,w.route,w.conversation_id,r.alias,r.responsibility
                                FROM antigravity_members w JOIN teams t ON t.id=w.team_id
                                JOIN roster r ON r.team_id=t.id AND r.member=w.member
                                WHERE t.archived=0 AND (w.instance=? OR w.route=?)''', (instance, route)).fetchone()
            if row is None:
                return None
            if instance is not None:
                now = time.time()
                db.execute('UPDATE antigravity_members SET last_seen=? WHERE instance=? AND last_seen<?',
                           (now, instance, now-8))
            return self.view(row)

    def prepare_acp(self, team_id, member, label=''):
        if not isinstance(label, str) or len(label.strip()) > 80:
            raise ValueError('请输入不超过 80 字的会话备注。')
        with self.store.connect() as db:
            seat = db.execute("""SELECT COALESCE(r.alias,r.member) FROM roster r JOIN teams t ON t.id=r.team_id
                                 WHERE r.team_id=? AND r.member=? AND r.product='agy_acp' AND t.archived=0""",
                              (team_id, member)).fetchone()
            if not seat:
                raise Conflict('请先为此 Team 创建 AGY ACP 成员。')
            code = secrets.token_hex(5)
            db.execute('''INSERT INTO acp_members(team_id,member,code,pending_label) VALUES (?,?,?,?)
                          ON CONFLICT(team_id,member) DO UPDATE SET code=excluded.code,pending_label=excluded.pending_label''',
                       (team_id, member, code, label.strip() or seat[0]))
            return code

    def bind_acp(self, session_id, instance, code, label=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.id,t.name,t.mode,a.member,a.pending_label,COALESCE(r.alias,r.member) AS alias
                                FROM acp_members a
                                JOIN teams t ON t.id=a.team_id
                                JOIN roster r ON r.team_id=t.id AND r.member=a.member
                                WHERE a.code=? AND t.archived=0''', (code,)).fetchone()
            if row is None:
                raise Conflict('绑定代码无效或已使用。')
            other = db.execute('''SELECT 1 FROM acp_members WHERE session_id=?
                                  AND (team_id<>? OR member<>?)''',
                               (session_id, row['id'], row['member'])).fetchone()
            if other:
                raise Conflict('该原生会话已绑定另一位成员。')
            route = secrets.token_urlsafe(18)
            final_label = (label.strip() if isinstance(label, str) and label.strip()
                           else (row['pending_label'] or row['alias']))[:80]
            db.execute('''UPDATE acp_members SET session_id=?,instance=?,route=?,label=?,
                          code=NULL,pending_label=NULL,last_seen=? WHERE team_id=? AND member=?''',
                       (session_id, instance, route, final_label, time.time(), row['id'], row['member']))
            self.bump(db, row['id'])
            return {
                'team': row['name'],
                'mode': team_mode(row),
                'member': row['member'],
                'name': row['alias'],
                'label': final_label,
                'route': route,
            }

    def restore_acp(self, session_id, instance):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.id,t.name,t.mode,a.member,a.route,a.label,COALESCE(r.alias,r.member) AS alias
                                FROM acp_members a
                                JOIN teams t ON t.id=a.team_id
                                JOIN roster r ON r.team_id=t.id AND r.member=a.member
                                WHERE a.session_id=? AND a.route IS NOT NULL AND t.archived=0''',
                             (session_id,)).fetchone()
            if row is None:
                return {'bound': False}
            db.execute('UPDATE acp_members SET instance=?,last_seen=? WHERE team_id=? AND member=?',
                       (instance, time.time(), row['id'], row['member']))
            return {
                'bound': True,
                'team': row['name'],
                'mode': team_mode(row),
                'member': row['member'],
                'name': row['alias'],
                'label': row['label'] or row['alias'],
                'route': row['route'],
            }

    def unbind_acp(self, route):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT a.team_id,a.member FROM acp_members a
                                JOIN teams t ON t.id=a.team_id
                                WHERE a.route=? AND t.archived=0''', (route,)).fetchone()
            if row is None:
                return {'unbound': False}
            db.execute('''UPDATE acp_members SET session_id=NULL,instance=NULL,route=NULL,last_seen=NULL
                          WHERE team_id=? AND member=?''', (row['team_id'], row['member']))
            self.bump(db, row['team_id'])
            return {'unbound': True}

    def acp_member(self, *, route=None, instance=None, touch=True):
        with self.store.connect() as db:
            if route is not None:
                row = db.execute('''SELECT t.*,a.member,a.label,a.route,a.session_id,COALESCE(r.alias,r.member) AS alias,r.responsibility
                                    FROM acp_members a JOIN teams t ON t.id=a.team_id
                                    JOIN roster r ON r.team_id=t.id AND r.member=a.member
                                    WHERE t.archived=0 AND a.route=?''', (route,)).fetchone()
            elif instance is not None:
                row = db.execute('''SELECT t.*,a.member,a.label,a.route,a.session_id,COALESCE(r.alias,r.member) AS alias,r.responsibility
                                    FROM acp_members a JOIN teams t ON t.id=a.team_id
                                    JOIN roster r ON r.team_id=t.id AND r.member=a.member
                                    WHERE t.archived=0 AND a.instance=?''', (instance,)).fetchone()
            else:
                return None
            if row is None:
                return None
            if touch:
                now = time.time()
                db.execute('UPDATE acp_members SET last_seen=? WHERE team_id=? AND member=? AND (last_seen IS NULL OR last_seen<?)',
                           (now, row['id'], row['member'], now - 8))
            return self.view(row)

    def prepare_agy_cli(self, team_id, member, label=''):
        if not isinstance(label, str) or len(label.strip()) > 80:
            raise ValueError('请输入不超过 80 字的会话备注。')
        with self.store.connect() as db:
            seat = db.execute("""SELECT COALESCE(r.alias,r.member) FROM roster r JOIN teams t ON t.id=r.team_id
                                 WHERE r.team_id=? AND r.member=? AND r.product='agy_cli' AND t.archived=0""",
                              (team_id, member)).fetchone()
            if not seat:
                raise Conflict('请先为此 Team 创建 AGY CLI 成员。')
            code = secrets.token_hex(5)
            db.execute('''INSERT INTO agy_cli_members(team_id,member,code,pending_label) VALUES (?,?,?,?)
                          ON CONFLICT(team_id,member) DO UPDATE SET code=excluded.code,pending_label=excluded.pending_label''',
                       (team_id, member, code, label.strip() or seat[0]))
            return code

    def bind_agy_cli(self, code, session_id=None, label=None):
        clean_sid = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.id,t.name,t.mode,c.member,c.pending_label,COALESCE(r.alias,r.member) AS alias
                                FROM agy_cli_members c
                                JOIN teams t ON t.id=c.team_id
                                JOIN roster r ON r.team_id=t.id AND r.member=c.member
                                WHERE c.code=? AND t.archived=0''', (code,)).fetchone()
            if row is None:
                raise Conflict('绑定代码无效或已使用。')
            if clean_sid:
                other = db.execute('''SELECT 1 FROM agy_cli_members WHERE session_id=?
                                      AND (team_id<>? OR member<>?)''',
                                   (clean_sid, row['id'], row['member'])).fetchone()
                if other:
                    raise Conflict('该 AGY CLI 原会话已绑定另一位成员。')
            route = secrets.token_urlsafe(18)
            final_label = (label.strip() if isinstance(label, str) and label.strip()
                           else (row['pending_label'] or row['alias']))[:80]
            db.execute('''UPDATE agy_cli_members SET session_id=?,route=?,label=?,
                          code=NULL,pending_label=NULL,last_seen=? WHERE team_id=? AND member=?''',
                       (clean_sid, route, final_label, time.time(), row['id'], row['member']))
            self.bump(db, row['id'])
            return {
                'team': row['name'],
                'mode': team_mode(row),
                'member': row['member'],
                'name': row['alias'],
                'label': final_label,
                'route': route,
                'session_id_bound': bool(clean_sid),
            }

    def restore_agy_cli(self, session_id):
        clean_sid = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        if not clean_sid:
            return {'bound': False}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.id,t.name,t.mode,c.member,c.route,c.label,COALESCE(r.alias,r.member) AS alias
                                FROM agy_cli_members c
                                JOIN teams t ON t.id=c.team_id
                                JOIN roster r ON r.team_id=t.id AND r.member=c.member
                                WHERE c.session_id=? AND c.route IS NOT NULL AND t.archived=0''',
                             (clean_sid,)).fetchone()
            if row is None:
                return {'bound': False}
            db.execute('UPDATE agy_cli_members SET last_seen=? WHERE team_id=? AND member=?',
                       (time.time(), row['id'], row['member']))
            return {
                'bound': True,
                'team': row['name'],
                'mode': team_mode(row),
                'member': row['member'],
                'name': row['alias'],
                'label': row['label'] or row['alias'],
                'route': row['route'],
            }

    def agy_cli_member(self, *, route=None, session_id=None, touch=True):
        with self.store.connect() as db:
            if route is not None:
                row = db.execute('''SELECT t.*,c.member,c.label,c.route,c.session_id,COALESCE(r.alias,r.member) AS alias,r.responsibility
                                    FROM agy_cli_members c JOIN teams t ON t.id=c.team_id
                                    JOIN roster r ON r.team_id=t.id AND r.member=c.member
                                    WHERE t.archived=0 AND c.route=?''', (route,)).fetchone()
            elif session_id is not None:
                row = db.execute('''SELECT t.*,c.member,c.label,c.route,c.session_id,COALESCE(r.alias,r.member) AS alias,r.responsibility
                                    FROM agy_cli_members c JOIN teams t ON t.id=c.team_id
                                    JOIN roster r ON r.team_id=t.id AND r.member=c.member
                                    WHERE t.archived=0 AND c.session_id=?''', (session_id,)).fetchone()
            else:
                return None
            if row is None:
                return None
            if touch:
                now = time.time()
                db.execute('UPDATE agy_cli_members SET last_seen=? WHERE team_id=? AND member=? AND (last_seen IS NULL OR last_seen<?)',
                           (now, row['id'], row['member'], now - 8))
            return self.view(row)

    def edit(self, action, body):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if action == 'create':
                mode = body.get('mode', 'structured') or 'structured'
                if mode not in TEAM_MODES:
                    raise ValueError('请选择有效的 Team 类型。')
                name = short_text(body.get('name'), 60, 'Team 名称')
                if mode == 'peer':
                    team_id = db.execute(
                        "INSERT INTO teams (name,workspace,roles,mode) VALUES (?,'','{}','peer')",
                        (name,),
                    ).lastrowid
                    return {'team_id': team_id, 'mode': 'peer'}
                raw = short_text(body.get('workspace'), 1000, '工作区路径')
                path = Path(raw)
                if not path.is_absolute() or not path.is_dir():
                    raise ValueError('请输入存在的工作区绝对路径。')
                workspace = str(path.resolve())
                selected_roles = body.get('roles', {})
                if not isinstance(selected_roles, dict) or any(
                        key not in PRODUCT_ROLES or role not in ROLES for key, role in selected_roles.items()):
                    raise ValueError('请选择每个产品的分工。')
                team_id = db.execute(
                    "INSERT INTO teams (name,workspace,roles,mode) VALUES (?,?,?,'structured')",
                    (name, workspace, json.dumps(selected_roles)),
                ).lastrowid
                return {'team_id': team_id, 'mode': 'structured'}
            team_id = body.get('team_id')
            if type(team_id) is not int:
                raise ValueError('请选择 Team。')
            team = self.get(db, team_id)
            if not team:
                raise Conflict('该 Team 不可用。')
            mode = team_mode(team)
            member = body.get('member')
            if action == 'rename':
                name = short_text(body.get('name'), 60, 'Team 名称')
                if name != team['name']:
                    db.execute('UPDATE teams SET name=? WHERE id=?', (name, team_id))
                    self.bump(db, team_id)
            elif action == 'workspace':
                if mode == 'peer':
                    raise Conflict('无分工 Team 不使用工作区配置。')
                raw = short_text(body.get('workspace'), 1000, '工作区路径')
                path = Path(raw)
                if not path.is_absolute() or not path.is_dir():
                    raise ValueError('请输入存在的工作区绝对路径。')
                workspace = str(path.resolve())
                selected_roles = body.get('roles', {})
                if not isinstance(selected_roles, dict) or any(
                        key not in PRODUCT_ROLES or role not in ROLES for key, role in selected_roles.items()):
                    raise ValueError('请选择每个产品的分工。')
                if team['workspace'] != workspace and db.execute(
                        'SELECT 1 FROM project_deployments WHERE team_id=?', (team_id,)).fetchone():
                    raise Conflict('请先撤销当前工作区的 Hook 配置，再更换工作区。')
                if workspace != team['workspace'] or self.view(team)['roles'] != PRODUCT_ROLES | selected_roles:
                    db.execute('UPDATE teams SET workspace=?,roles=? WHERE id=?',
                               (workspace, json.dumps(selected_roles), team_id))
                    self.bump(db, team_id)
            elif action == 'archive':
                if db.execute("""SELECT 1 FROM tasks WHERE team_id=?
                    AND (status IN ('queued','running') OR (result IS NOT NULL AND acknowledged=0))""",
                              (team_id,)).fetchone():
                    raise Conflict('这个 Team 仍有旧任务在执行或等待接收。')
                if db.execute('SELECT 1 FROM project_deployments WHERE team_id=?', (team_id,)).fetchone():
                    raise Conflict('请先撤销这个 Team 的项目 Hook。')
                db.execute('UPDATE teams SET archived=1,active=0 WHERE id=?', (team_id,))
                db.execute('UPDATE antigravity_members SET conversation_id=NULL WHERE team_id=?', (team_id,))
                db.execute('UPDATE acp_members SET session_id=NULL,instance=NULL,route=NULL,code=NULL,last_seen=NULL WHERE team_id=?', (team_id,))
                db.execute('UPDATE agy_cli_members SET session_id=NULL,route=NULL,code=NULL,last_seen=NULL WHERE team_id=?', (team_id,))
                db.execute('''UPDATE member_controls SET claude_session_id=NULL,instance=NULL,rebind_code=NULL
                              WHERE name IN (SELECT member FROM roster WHERE team_id=?)''', (team_id,))
            elif action == 'git_identity':
                if mode == 'peer':
                    raise Conflict('无分工 Team 不使用 Git 提交身份配置。')
                git_name = optional_text(body.get('git_author_name'), 80, 'Git 作者名')
                git_email = optional_text(body.get('git_author_email'), 160, 'Git 邮箱')
                if bool(git_name) != bool(git_email):
                    raise ValueError('请同时填写 Git 作者名和邮箱，或同时留空。')
                db.execute('UPDATE teams SET git_author_name=?,git_author_email=? WHERE id=?',
                           (git_name, git_email, team_id))
            elif action == 'member':
                return self.save_member(db, team, body)
            elif action == 'remove_member':
                if db.execute("""SELECT 1 FROM tasks WHERE team_id=? AND recipient=?
                                  AND status IN ('queued','running')""", (team_id, member)).fetchone():
                    raise Conflict('该成员还有旧任务在执行。')
                if db.execute("""SELECT 1 FROM team_messages WHERE team_id=? AND recipient=?
                                  AND delivered='submitting'""", (team_id, member)).fetchone():
                    raise Conflict('该原会话正在接收消息，请稍后移除。')
                if not db.execute('DELETE FROM roster WHERE team_id=? AND member=?', (team_id, member)).rowcount:
                    raise Conflict('该成员已移出这个 Team。')
                db.execute('DELETE FROM antigravity_members WHERE team_id=? AND member=?', (team_id, member))
                db.execute('DELETE FROM acp_members WHERE team_id=? AND member=?', (team_id, member))
                db.execute('DELETE FROM agy_cli_members WHERE team_id=? AND member=?', (team_id, member))
                db.execute("""UPDATE team_messages SET delivered='canceled'
                              WHERE team_id=? AND recipient=? AND delivered IS NULL""", (team_id, member))
                if self.member_team(member, db) is None:
                    db.execute('DELETE FROM codex_bindings WHERE member=?', (member,))
                    db.execute('DELETE FROM member_controls WHERE name=?', (member,))
                self.bump(db, team_id)
            else:
                raise ValueError('未知 Team 操作。')
        return {'updated': True}

    def save_member(self, db, team, body):
        team_id, member = team['id'], body.get('member')
        mode = team_mode(team)
        alias = short_text(body.get('name'), 48, '成员名')
        product = body.get('product')
        if product not in PRODUCT_ROLES:
            raise ValueError('请选择 Agent 产品。')
        old = db.execute('SELECT * FROM roster WHERE team_id=? AND member=?', (team_id, member)).fetchone()
        if member and not old:
            raise Conflict('该成员已移出这个 Team。')
        if mode == 'peer':
            raw_note = body.get('note') if 'note' in body else body.get('responsibility')
            responsibility = optional_text(raw_note, 160, '备注')
            role = PEER_ROLE
            model = ''
            worktree = ''
        else:
            responsibility = short_text(body.get('responsibility'), 160, '主要职责')
            raw_role = body.get('role')
            if raw_role is None or raw_role == '':
                role = old['role'] if old else self.view(team)['roles'][product]
            else:
                if raw_role not in ROLES:
                    raise ValueError('请选择有效的成员职位。')
                role = raw_role
            model = optional_text(body.get('model'), 64, '模型标签') if 'model' in body else (old['model'] if old else '')
            worktree = normalize_worktree(body.get('worktree')) if 'worktree' in body else (old['worktree'] if old else '')
        existing = self.lookup(db, team_id, alias)
        if existing and existing != member:
            raise Conflict('这个名称已在该 Team 使用。')
        if old and old['product'] and old['product'] != product:
            bound = db.execute("""SELECT 1 FROM member_controls m WHERE name=? AND
                                  (instance IS NOT NULL OR claude_session_id IS NOT NULL
                                   OR EXISTS (SELECT 1 FROM codex_bindings WHERE member=m.name)
                                   OR EXISTS (SELECT 1 FROM antigravity_members WHERE member=m.name AND instance IS NOT NULL)
                                   OR EXISTS (SELECT 1 FROM acp_members WHERE member=m.name AND session_id IS NOT NULL)
                                   OR EXISTS (SELECT 1 FROM agy_cli_members WHERE member=m.name AND route IS NOT NULL))""",
                               (member,)).fetchone()
            if bound:
                raise Conflict('这个成员已有原会话绑定。请选择相同产品，或创建新成员。')
            db.execute('DELETE FROM antigravity_members WHERE team_id=? AND member=?', (team_id, member))
            db.execute('DELETE FROM acp_members WHERE team_id=? AND member=?', (team_id, member))
            db.execute('DELETE FROM agy_cli_members WHERE team_id=? AND member=?', (team_id, member))
            db.execute('UPDATE member_controls SET rebind_code=NULL WHERE name=?', (member,))
        if not old:
            member = self.new_member(db)
        identity_changed = (
            not old
            or (old['role'], old['responsibility'], old['model'], old['worktree'])
            != (role, responsibility, model, worktree)
        )
        any_changed = identity_changed or old['alias'] != alias or old['product'] != product
        if any_changed:
            next_role_rev = 1 if not old else (old['role_revision'] + 1 if identity_changed else old['role_revision'])
            self.bump(db, team_id)
            db.execute('''INSERT INTO roster
                (team_id,member,role,alias,product,responsibility,seen_revision,role_revision,model,worktree)
                VALUES (?,?,?,?,?,?,(SELECT revision FROM teams WHERE id=?),?,?,?)
                ON CONFLICT(team_id,member) DO UPDATE SET
                    role=excluded.role,alias=excluded.alias,product=excluded.product,
                    responsibility=excluded.responsibility,role_revision=excluded.role_revision,
                    model=excluded.model,worktree=excluded.worktree''',
                       (team_id, member, role, alias, product, responsibility, team_id,
                        next_role_rev, model, worktree))
        return {'updated': True, 'member': member}

    @staticmethod
    def new_member(db):
        key = 'member-' + secrets.token_hex(8)
        db.execute('INSERT INTO member_controls(name) VALUES (?)', (key,))
        return key

    def bind(self, member, instance, code, session_id=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            team = self.member_team(member, db)
            if not team:
                raise Conflict('请先在管理台创建此成员。')
            slot = db.execute('SELECT product FROM roster WHERE team_id=? AND member=?', (team['id'], member)).fetchone()
            if slot['product'] not in (None, 'claude'):
                raise Conflict('请选择 Claude Code 成员。')
            if session_id and db.execute(
                    'SELECT 1 FROM member_controls WHERE claude_session_id=? AND name<>?',
                    (session_id, member)).fetchone():
                raise Conflict('这个 Claude 聊天已绑定另一位成员。')
            row = db.execute(
                'SELECT instance,rebind_code,claude_session_id FROM member_controls WHERE name=?',
                (member,)).fetchone()
            if row['rebind_code']:
                if not isinstance(code, str) or not secrets.compare_digest(code, row['rebind_code']):
                    raise Conflict('请输入管理台生成的绑定代码。')
                db.execute(
                    'UPDATE member_controls SET instance=?,rebind_code=NULL,claude_session_id=? WHERE name=?',
                    (instance, session_id, member))
                changed = row['instance'] != instance or row['claude_session_id'] != session_id
            elif row['instance'] == instance or (session_id and row['claude_session_id'] == session_id):
                db.execute(
                    'UPDATE member_controls SET instance=?,claude_session_id=COALESCE(?,claude_session_id) WHERE name=?',
                    (instance, session_id, member))
                changed = False
            else:
                raise Conflict('请从管理台生成新的绑定代码。')
            if changed:
                db.execute("UPDATE roster SET product='claude' WHERE team_id=? AND member=?", (team['id'], member))
                self.bump(db, team['id'])

    def restore_desktop(self, instance, session_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT m.name AS member,t.id AS team_id,t.name AS team,r.product
                                FROM member_controls m JOIN roster r ON r.member=m.name
                                JOIN teams t ON t.id=r.team_id
                                WHERE m.claude_session_id=? AND t.archived=0''', (session_id,)).fetchone()
            if row is None:
                return {'bound': False}
            if row['product'] not in (None, 'claude') or db.execute(
                    'SELECT 1 FROM codex_bindings WHERE member=?', (row['member'],)).fetchone():
                return {'bound': False}
            if db.execute('SELECT 1 FROM member_controls WHERE instance=? AND name<>?',
                          (instance, row['member'])).fetchone():
                raise Conflict('这个会话已经绑定另一个成员。')
            db.execute('UPDATE member_controls SET instance=? WHERE name=?', (instance, row['member']))
            return {'bound': True, 'member': row['member'], 'team': row['team']}

    def prepare_binding(self, member):
        code = secrets.token_hex(4)
        with self.store.connect() as db:
            team = self.member_team(member, db)
            if not team or not db.execute("""SELECT 1 FROM roster WHERE team_id=? AND member=?
                                             AND (product IS NULL OR product='claude')""",
                                         (team['id'], member)).fetchone():
                raise Conflict('请选择 Claude Code 成员。')
            if db.execute("SELECT 1 FROM tasks WHERE recipient=? AND status='running'", (member,)).fetchone():
                raise Conflict('该成员还有旧任务在执行。')
            db.execute('UPDATE member_controls SET rebind_code=? WHERE name=?', (code, member))
        return code
