"""Deploy Team Hub hooks/MCP across a workspace and its Git worktrees, and resolve dynamic member context."""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

from teams import Conflict, PRODUCT_ROLES, team_mode


SOURCE = Path(__file__).resolve().parent
PROFILE = '.team-hub/project.json'
CODEX_CONFIG = '.codex/config.toml'
CLAUDE_PROJECT = '.mcp.json'
AGENT = SOURCE / 'project_agent.py'
CODEX_BLOCK = ('# BEGIN Team Hub project MCP', '# END Team Hub project MCP')
EXCLUDE_BLOCK = ('# BEGIN Team Hub managed', '# END Team Hub managed')
EXCLUDE_PATTERNS = (
    '.team-hub/',
    '.agents/hooks.json',
    '.claude/settings.local.json',
    '.mcp.json',
    '.codex/hooks.json',
    '.codex/config.toml',
)
HOOK_SCHEMA_VERSION = 4
MANAGED_FILES = (
    PROFILE,
    '.claude/settings.local.json',
    '.codex/hooks.json',
    '.agents/hooks.json',
    CLAUDE_PROJECT,
    CODEX_CONFIG,
)


def write_file(path, content):
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(data):
    parsed = json.loads(data.decode('utf-8-sig')) if data else {}
    if not isinstance(parsed, dict):
        raise ValueError('项目配置应为 JSON 对象。')
    return parsed


def encode_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def project_file(root, relative):
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root):
        raise Conflict(f'配置路径指向工作区外或经过链接：{relative}')
    return path


def command(args):
    if os.name == 'nt':
        # Use forward slashes so Windows Git Bash (used by Claude hooks), cmd, and PowerShell all run cleanly.
        normalized = [arg.replace('\\', '/') if isinstance(arg, str) else arg for arg in args]
        return subprocess.list2cmdline(normalized)
    return shlex.join(args)


def is_managed_hook(cmd, product, team_id):
    if not isinstance(cmd, str):
        return False
    marker = f'project_agent.py'
    tail = f'hook {product} {team_id} '
    return marker in cmd and tail in cmd


def register_hooks(settings, events, runner, installing, product=None, team_id=None):
    hooks = settings.setdefault('hooks', {})
    # When uninstalling or syncing, clean all events this product manages.
    for event, match in events.items():
        groups = hooks.get(event, [])
        groups = [group for group in groups if not (
            len(group.get('hooks', [])) == 1
            and (group['hooks'][0].get('command') == runner
                 or (product and team_id is not None
                     and is_managed_hook(group['hooks'][0].get('command'), product, team_id))))]
        if installing:
            handler = {'type': 'command', 'command': runner, 'timeout': 4}
            groups.append({'hooks': [handler]} | ({'matcher': match} if match else {}))
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop('hooks', None)


def mcp_config(data, entry, installing, allow_replace=False):
    original = data.decode('utf-8-sig') if data else ''
    start, end = CODEX_BLOCK
    if (start in original) != (end in original) or original.count(start) > 1 or original.count(end) > 1:
        raise Conflict('Codex 的 Team Hub 配置标记不完整，请检查 .codex/config.toml。')
    text = original
    block = (f'{start}\n[mcp_servers.team_hub]\n'
             f'command = {json.dumps(entry[0], ensure_ascii=False)}\n'
             f'args = {json.dumps(entry[1:], ensure_ascii=False)}\n{end}')
    if start in original:
        before, rest = original.split(start, 1)
        if end not in rest:
            raise Conflict('Codex 的 Team Hub 配置标记顺序有误。')
        inside, after = rest.split(end, 1)
        if start + inside + end != block and not allow_replace:
            raise Conflict('Codex 的 Team Hub 配置段已被修改，需先检查。')
        text = before + after.lstrip('\r\n')
    parsed = tomllib.loads(text)
    if installing:
        if 'team_hub' in parsed.get('mcp_servers', {}):
            raise Conflict('Codex 已经定义了名为 team_hub 的 MCP 服务。')
        text = text.rstrip() + ('\n\n' if text.strip() else '') + block + '\n'
        tomllib.loads(text)
    return text.encode('utf-8') if text.strip() else None


def sync_git_exclude(root, installing):
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            capture_output=True, text=True, encoding='utf-8', timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    exclude_path = Path(proc.stdout.strip()) / 'info' / 'exclude'
    original = exclude_path.read_text(encoding='utf-8', errors='replace') if exclude_path.is_file() else ''
    start, end = EXCLUDE_BLOCK
    text = original
    if start in text and end in text:
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        text = before.rstrip('\r\n') + ('\n' if before.strip() and after.strip() else '') + after.lstrip('\r\n')
    if installing:
        block = '\n'.join([start, *EXCLUDE_PATTERNS, end]) + '\n'
        text = text.rstrip('\r\n') + ('\n\n' if text.strip() else '') + block
    elif text and not text.endswith('\n'):
        text += '\n'
    if text != original:
        write_file(exclude_path, text.encode('utf-8'))
    return True


def discover_worktrees(workspace):
    if not workspace:
        return []
    root = Path(workspace)
    if not root.is_dir():
        return []
    root = root.resolve()
    found = {}

    def record(wt_path, branch=''):
        try:
            resolved = wt_path.resolve()
        except OSError:
            return
        if not resolved.is_dir() or not resolved.is_relative_to(root):
            return
        rel = '' if resolved == root else resolved.relative_to(root).as_posix()
        found[rel] = {
            'path': str(resolved),
            'relative': rel,
            'branch': branch,
            'is_root': rel == '',
            'has_ai': (resolved / '.ai' / 'CURRENT.md').is_file(),
        }

    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'worktree', 'list', '--porcelain'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None

    if proc and proc.returncode == 0 and proc.stdout.strip():
        blocks = proc.stdout.strip().split('\n\n')
        for block in blocks:
            wt_line = None
            branch = ''
            skip = False
            for raw in block.splitlines():
                line = raw.strip()
                if line.startswith('worktree '):
                    wt_line = line[9:].strip()
                elif line.startswith('branch '):
                    ref = line[7:].strip()
                    branch = ref.removeprefix('refs/heads/')
                elif line == 'detached':
                    branch = '(detached)'
                elif line in ('bare',) or line.startswith('prunable'):
                    skip = True
            if wt_line and not skip:
                record(Path(wt_line), branch)

    if '' not in found:
        record(root, '')

    return sorted(found.values(), key=lambda item: (0 if item['is_root'] else 1, item['relative'].casefold()))


def _read_role_file(workspace, filename):
    candidates = []
    if workspace:
        candidates.append(Path(workspace) / '.team-hub' / 'roles' / filename)
    candidates.append(SOURCE / 'roles' / filename)
    for path in candidates:
        if path.is_file():
            try:
                return path.read_text(encoding='utf-8').strip()
            except OSError:
                continue
    return ''


def load_role_bundle(workspace, role):
    parts = [part.strip() for part in (role or '').split('+') if part.strip()]
    sections = []
    for part in parts:
        text = _read_role_file(workspace, f'{part}.md')
        if text:
            sections.append(text)
    if len(parts) > 1:
        sections.append('组合分工说明：默认以首位角色立场为主，收到明确协助或攻坚委托时按对应角色执行。')
    return '\n\n'.join(sections)


def load_team_rules(workspace):
    return _read_role_file(workspace, 'team_rules.md')


def _rel_with_prefix(prefix, relative):
    return f'{prefix}/{relative}' if prefix else relative


def _detect_has_ai(root, paths, member_worktree=''):
    for raw in paths or ():
        if isinstance(raw, str) and raw and Path(raw).is_absolute():
            try:
                current = Path(raw).resolve()
            except OSError:
                continue
            if not current.is_relative_to(root):
                continue
            cursor = current if current.is_dir() else current.parent
            while cursor.is_relative_to(root):
                if (cursor / '.ai' / 'CURRENT.md').is_file():
                    return True
                if cursor == root:
                    break
                cursor = cursor.parent
    if member_worktree:
        candidate = root / member_worktree / '.ai' / 'CURRENT.md'
        if candidate.is_file():
            return True
    return (root / '.ai' / 'CURRENT.md').is_file()


class Projects:
    def __init__(self, store):
        self.store = store
        self.injected = {}
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS project_deployments (
                    team_id INTEGER PRIMARY KEY, workspace TEXT NOT NULL UNIQUE, port INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_hook_events (
                    team_id INTEGER NOT NULL, product TEXT NOT NULL,
                    last_seen REAL NOT NULL, event TEXT NOT NULL, PRIMARY KEY (team_id, product)
                );
                CREATE TABLE IF NOT EXISTS project_files (
                    team_id INTEGER NOT NULL, relative TEXT NOT NULL, before BLOB, installed BLOB,
                    PRIMARY KEY(team_id,relative)
                );
            """)

    @staticmethod
    def status(db, team_id):
        return {'deployed': bool(db.execute('SELECT 1 FROM project_deployments WHERE team_id=?',
                                            (team_id,)).fetchone()),
                'observed': {row['product']: row['event'] for row in db.execute(
                    'SELECT product,event FROM project_hook_events WHERE team_id=?', (team_id,))}}

    def worktree_status(self, db, team):
        workspace = team.get('workspace') if isinstance(team, dict) else team['workspace']
        if not workspace:
            return []
        team_id = team['id']
        worktrees = discover_worktrees(workspace)
        rows = {row['relative']: row['installed'] for row in db.execute(
            'SELECT relative,installed FROM project_files WHERE team_id=?', (team_id,))}
        for item in worktrees:
            key = _rel_with_prefix(item['relative'], PROFILE)
            installed_profile = rows.get(key)
            profile_path = Path(item['path']) / PROFILE
            version = 0
            if profile_path.is_file():
                try:
                    data = read_json(profile_path.read_bytes())
                    if data.get('team_id') == team_id:
                        version = int(data.get('version', 1))
                except (OSError, ValueError):
                    version = 0
            item['deployed'] = bool(installed_profile is not None and version > 0)
            item['up_to_date'] = bool(item['deployed'] and version >= HOOK_SCHEMA_VERSION)
        return worktrees

    def observed(self, team_id, product):
        with self.store.connect() as db:
            return db.execute('SELECT 1 FROM project_hook_events WHERE team_id=? AND product=?',
                              (team_id, product)).fetchone() is not None

    def deployed_team(self, db, team_id, paths):
        if type(team_id) is not int or not isinstance(paths, list) or len(paths) > 20:
            return None
        row = db.execute('SELECT workspace FROM project_deployments WHERE team_id=?', (team_id,)).fetchone()
        if row and any(isinstance(path, str) and path and Path(path).is_absolute()
                       and Path(path).resolve().is_relative_to(Path(row['workspace'])) for path in paths):
            return self.store.teams.get(db, team_id)
        return None

    def _bound_member(self, db, team_id, product, session):
        if not isinstance(session, str) or not session.strip():
            return None
        session = session.strip()
        if product == 'codex':
            row = db.execute('''SELECT r.member FROM codex_bindings c JOIN roster r
                ON r.member=c.member WHERE c.thread_id=? AND r.team_id=?''',
                             (session, team_id)).fetchone()
        elif product == 'claude':
            row = db.execute('''SELECT r.member FROM member_controls m JOIN roster r
                ON r.member=m.name WHERE m.claude_session_id=? AND r.team_id=?''',
                             (session, team_id)).fetchone()
        elif product == 'antigravity':
            row = db.execute('''SELECT member FROM antigravity_members
                WHERE team_id=? AND conversation_id=?''',
                             (team_id, session)).fetchone()
            if not row:
                row = db.execute('''SELECT member FROM agy_cli_members
                    WHERE team_id=? AND session_id=?''',
                                 (team_id, session)).fetchone()
        else:
            row = None
        return row['member'] if row else None

    def context(self, team_id, product, session, event, paths):
        if type(team_id) is not int or product not in PRODUCT_ROLES:
            raise ValueError('无效的项目 Hook 请求。')
        with self.store.connect() as db:
            cli_delivery = None
            if product == 'antigravity' and isinstance(session, str) and session.strip():
                cli_row = self.store.teams.agy_cli_member(session_id=session.strip(), touch=True)
                if cli_row:
                    pending = self.store.messages.next_in_connection(db, cli_row['id'], cli_row['member'])
                    if pending:
                        self.store.messages.received_in_connection(
                            db, cli_row['id'], cli_row['member'], pending['id'], pending['revision'], 'accepted',
                        )
                        cli_delivery = pending['prompt']
            team = self.deployed_team(db, team_id, paths)
            if not team or team_mode(team) == 'peer':
                return {'cli_delivery': cli_delivery} if cli_delivery else {}
            if event in ('startup', 'resume', 'compact', 'clear', 'fork',
                         'SessionStart', 'UserPromptSubmit', 'PreInvocation'):
                db.execute('''INSERT INTO project_hook_events VALUES (?,?,?,?)
                    ON CONFLICT(team_id,product) DO UPDATE SET last_seen=excluded.last_seen,event=excluded.event''',
                           (team_id, product, time.time(), event))
            member = self._bound_member(db, team_id, product, session)
            if not member:
                return {'cli_delivery': cli_delivery} if cli_delivery else {}
            identity = self.store.teams.member_identity(db, team, member)
            if not identity:
                return {'cli_delivery': cli_delivery} if cli_delivery else {}
        cache_key = (team_id, product, session.strip())
        version_sig = (member, identity['role_revision'], identity['name'])
        if event in ('UserPromptSubmit', 'PreInvocation'):
            if self.injected.get(cache_key) == version_sig:
                return {'cli_delivery': cli_delivery} if cli_delivery else {}
            mode = 'refresh' if cache_key in self.injected else 'full'
        else:
            mode = 'full'
        self.injected[cache_key] = version_sig
        root = Path(team['workspace']).resolve()
        out = identity | {
            'profile': load_role_bundle(root, identity['role']),
            'team_rules': load_team_rules(root),
            'has_ai': _detect_has_ai(root, paths, identity.get('worktree_raw', '')),
            'mode': mode,
        }
        if cli_delivery:
            out['cli_delivery'] = cli_delivery
        return out

    def acp_context(self, team, member, session_id, event='prompt', paths=None):
        if not team or team_mode(team) == 'peer':
            return {}
        team_id = team['id']
        event = event if event in ('startup', 'prompt', 'refresh') else 'prompt'
        with self.store.connect() as db:
            db.execute('''INSERT INTO project_hook_events VALUES (?,?,?,?)
                ON CONFLICT(team_id,product) DO UPDATE SET last_seen=excluded.last_seen,event=excluded.event''',
                       (team_id, 'agy_acp', time.time(), event))
            identity = self.store.teams.member_identity(db, team, member)
            if not identity or 'role' not in identity:
                return {}
        cache_key = (team_id, 'agy_acp', str(session_id or member))
        version_sig = (member, identity['role_revision'], identity['name'])
        if event == 'prompt':
            if self.injected.get(cache_key) == version_sig:
                return {}
            mode = 'refresh' if cache_key in self.injected else 'full'
        else:
            mode = 'full'
        self.injected[cache_key] = version_sig
        workspace = team.get('workspace') or ''
        root = Path(workspace).resolve() if workspace and Path(workspace).is_dir() else None
        payload = identity | {
            'profile': load_role_bundle(root, identity['role']) if root else '',
            'team_rules': load_team_rules(root) if root else '',
            'has_ai': _detect_has_ai(root, paths or [], identity.get('worktree_raw', '')) if root else False,
            'mode': mode,
        }
        from project_agent import format_context
        return {
            'mode': mode,
            'role_revision': identity['role_revision'],
            'text': format_context(payload),
        }

    def change(self, team_id, action, port):
        if type(team_id) is not int or action not in ('install', 'sync', 'remove'):
            raise ValueError('请选择 Team 和操作。')
        installing = action in ('install', 'sync')
        syncing = action == 'sync'
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if team and team_mode(team) == 'peer':
                raise Conflict('无分工 Team 不部署工作区 Hook。')
            if not team or not team['workspace']:
                raise Conflict('请先保存工作区。')
            root = Path(team['workspace'])
            if not root.is_dir() or root.resolve() != root:
                raise Conflict('工作区路径已移动或更改，请更新管理台。')
            previous = db.execute('SELECT * FROM project_deployments WHERE team_id=?', (team_id,)).fetchone()
            if action == 'install' and previous:
                return {'updated': True}
            if action == 'remove' and not previous:
                return {'updated': True}
            if db.execute('SELECT 1 FROM project_deployments WHERE workspace=? AND team_id<>?',
                          (str(root), team_id)).fetchone():
                raise Conflict('该目录已部署另一个 Team 的 Hook，请选择独立的工作树。')
            if previous:
                port = previous['port']
            saved_rows = {
                row['relative']: (row['before'], row['installed'])
                for row in db.execute('SELECT relative,before,installed FROM project_files WHERE team_id=?', (team_id,))
            }
            if installing:
                targets = discover_worktrees(root)
                target_prefixes = [item['relative'] for item in targets]
            else:
                prefixes = {''}
                for rel in saved_rows:
                    for managed in MANAGED_FILES:
                        if rel == managed:
                            prefixes.add('')
                        elif rel.endswith('/' + managed):
                            prefixes.add(rel[:-(len(managed) + 1)])
                target_prefixes = sorted(prefixes, key=lambda p: (0 if p == '' else 1, p.casefold()))

            written = []
            try:
                active_keys = set()
                for prefix in target_prefixes:
                    wt_root = (root / prefix) if prefix else root
                    if not wt_root.is_dir():
                        continue
                    wt_root = wt_root.resolve()
                    changes = self.changes(wt_root, team, port, installing, allow_replace=(syncing or not installing))
                    for path, before, after in changes:
                        relative = path.relative_to(root).as_posix()
                        active_keys.add(relative)
                        saved = saved_rows.get(relative)
                        if not installing and saved and before == saved[1]:
                            after = saved[0]
                        if before != after:
                            if (path.read_bytes() if path.is_file() else None) != before:
                                raise Conflict(f'文件在部署期间被修改：{path}')
                            write_file(path, after)
                            written.append((path, before, after))
                        if installing:
                            if saved is not None:
                                db.execute('UPDATE project_files SET installed=? WHERE team_id=? AND relative=?',
                                           (after, team_id, relative))
                            elif before != after or after is not None:
                                db.execute('INSERT INTO project_files VALUES (?,?,?,?)',
                                           (team_id, relative, before, after))
                if installing:
                    if syncing:
                        for old_rel in set(saved_rows) - active_keys:
                            if not (root / old_rel).exists():
                                db.execute('DELETE FROM project_files WHERE team_id=? AND relative=?',
                                           (team_id, old_rel))
                    db.execute('INSERT OR IGNORE INTO project_deployments VALUES (?,?,?)',
                               (team_id, str(root), port))
                    sync_git_exclude(root, True)
                else:
                    db.execute('DELETE FROM project_deployments WHERE team_id=?', (team_id,))
                    db.execute('DELETE FROM project_hook_events WHERE team_id=?', (team_id,))
                    db.execute('DELETE FROM project_files WHERE team_id=?', (team_id,))
                    sync_git_exclude(root, False)
                db.commit()
            except (OSError, ValueError, sqlite3.Error):
                for path, before, after in reversed(written):
                    if (path.read_bytes() if path.is_file() else None) == after:
                        write_file(path, before)
                raise
        return {'updated': True}

    def changes(self, root, team, port, installing, allow_replace=False):
        entry = [sys.executable, str(AGENT)]
        launch = [*entry, 'mcp', str(port)]
        hook = lambda name: command([*entry, 'hook', name, str(team['id']), str(port)])
        current = {}
        for relative in MANAGED_FILES:
            path = project_file(root, relative)
            current[relative] = path.read_bytes() if path.is_file() else None
        profile = read_json(current[PROFILE])
        if profile and profile.get('team_id') != team['id']:
            raise Conflict(f'该工作树已配置另一个 Team：{root}')
        new = {PROFILE: encode_json({
            'team_id': team['id'], 'port': port, 'version': HOOK_SCHEMA_VERSION
        }) if installing else None}
        for path, product, events in (
            ('.claude/settings.local.json', 'claude', {
                'SessionStart': 'startup|resume|compact|clear|fork',
                'UserPromptSubmit': None,
            }),
            ('.codex/hooks.json', 'codex', {
                'SessionStart': 'startup|resume|compact|clear|fork',
                'UserPromptSubmit': None,
                'PreToolUse': '^mcp__team_hub__team_(send|members|whoami)$',
            }),
        ):
            settings = read_json(current[path])
            register_hooks(settings, events, hook(product), installing, product=product, team_id=team['id'])
            new[path] = encode_json(settings) if settings else None
        path = '.agents/hooks.json'
        settings = read_json(current[path])
        ag_handler = [{'type': 'command', 'command': hook('antigravity'), 'timeout': 4}]
        managed = {'SessionStart': ag_handler, 'PreInvocation': ag_handler}
        if 'team-hub' in settings and settings['team-hub'] != managed:
            existing_ag = settings['team-hub']
            ag_cmds = [
                h.get('command') for group in (existing_ag.values() if isinstance(existing_ag, dict) else [])
                for h in (group if isinstance(group, list) else []) if isinstance(h, dict)
            ]
            if not (allow_replace and ag_cmds and all(is_managed_hook(c, 'antigravity', team['id']) for c in ag_cmds)):
                raise Conflict(f'Antigravity 已有同名 Hook 或配置段已被修改：{root}')
        if installing:
            settings['team-hub'] = managed
        else:
            settings.pop('team-hub', None)
        new[path] = encode_json(settings) if settings else None
        settings = read_json(current[CLAUDE_PROJECT])
        servers = settings.setdefault('mcpServers', {})
        desktop = SOURCE / 'probes' / 'desktop_inbox.py'
        managed = {'type': 'stdio', 'command': sys.executable, 'args': [str(desktop), str(port)]}
        if 'team-hub' in servers and servers['team-hub'] != managed:
            existing_mcp = servers['team-hub']
            is_old_hub = (isinstance(existing_mcp, dict)
                          and isinstance(existing_mcp.get('args'), list)
                          and any('desktop_inbox.py' in str(a) for a in existing_mcp['args']))
            if not (allow_replace and is_old_hub):
                raise Conflict(f'Claude Code 已有同名 MCP 服务或配置段已被修改：{root}')
        if installing:
            existing = any(key != 'team-hub' and isinstance(value, dict) and port == 58767
                           and len(value.get('args', [])) == 1
                           and Path(value['args'][0]) == desktop
                           for key, value in servers.items())
            if not existing:
                servers['team-hub'] = managed
        else:
            servers.pop('team-hub', None)
        if not servers:
            settings.pop('mcpServers', None)
        new[CLAUDE_PROJECT] = encode_json(settings) if settings else None
        new[CODEX_CONFIG] = mcp_config(current[CODEX_CONFIG], launch, installing, allow_replace=allow_replace)
        return [(project_file(root, path), current[path], new[path]) for path in MANAGED_FILES]


def _backup_once(path):
    if not path.is_file():
        return
    bak = path.with_name(path.name + '.before-team-hub.bak')
    if not bak.exists():
        try:
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass


def acp_mcp_descriptor(port, route):
    return {
        'name': 'team-hub',
        'type': 'stdio',
        'command': sys.executable,
        'args': [str(AGENT), 'route-mcp', str(port)],
        'env': {'TEAM_HUB_ROUTE': route},
    }


def agy_cli_wait_command(port, route):
    return command([sys.executable, str(AGENT), 'wait-inbox', str(port), route])


def _expected_global_entries(port):
    desktop = SOURCE / 'probes' / 'desktop_inbox.py'
    claude_entry = {'type': 'stdio', 'command': sys.executable, 'args': [str(desktop), str(port)]}
    codex_launch = [sys.executable, str(AGENT), 'mcp', str(port)]
    codex_hook_cmd = command([sys.executable, str(AGENT), 'hook', 'codex', 'global', str(port)])
    return claude_entry, codex_launch, codex_hook_cmd


def global_access_status(port=58767, home_dir=None):
    try:
        home = Path(home_dir) if home_dir is not None else Path.home()
    except RuntimeError:
        return {
            'claude_mcp': False,
            'codex_mcp': False,
            'codex_ticket_hook': False,
            'antigravity_plugin': False,
            'ready': False,
        }
    claude_entry, codex_launch, codex_hook_cmd = _expected_global_entries(port)

    claude_path = home / '.claude.json'
    claude_mcp = False
    if claude_path.is_file():
        try:
            data = read_json(claude_path.read_bytes())
            servers = data.get('mcpServers') or {}
            entry = servers.get('team-hub')
            claude_mcp = isinstance(entry, dict) and entry == claude_entry
        except (OSError, ValueError):
            claude_mcp = False

    codex_cfg_path = home / '.codex' / 'config.toml'
    codex_mcp = False
    if codex_cfg_path.is_file():
        try:
            parsed = tomllib.loads(codex_cfg_path.read_text(encoding='utf-8-sig'))
            entry = (parsed.get('mcp_servers') or {}).get('team_hub')
            codex_mcp = (
                isinstance(entry, dict)
                and entry.get('command') == codex_launch[0]
                and entry.get('args') == codex_launch[1:]
            )
        except (OSError, ValueError):
            codex_mcp = False

    codex_hooks_path = home / '.codex' / 'hooks.json'
    codex_ticket_hook = False
    if codex_hooks_path.is_file():
        try:
            hdata = read_json(codex_hooks_path.read_bytes())
            groups = (hdata.get('hooks') or {}).get('PreToolUse') or []
            codex_ticket_hook = any(
                isinstance(g, dict)
                and len(g.get('hooks', [])) == 1
                and g['hooks'][0].get('command') == codex_hook_cmd
                for g in groups
            )
        except (OSError, ValueError):
            codex_ticket_hook = False

    ag_plugin = home / '.gemini' / 'config' / 'plugins' / 'mcp-aux-relay-plugin' / 'plugin.json'
    return {
        'claude_mcp': claude_mcp,
        'codex_mcp': codex_mcp,
        'codex_ticket_hook': codex_ticket_hook,
        'antigravity_plugin': ag_plugin.is_file(),
        'ready': bool(claude_mcp and codex_mcp and codex_ticket_hook),
    }


def configure_global_access(action, port=58767, home_dir=None):
    if action not in ('install', 'remove'):
        raise ValueError('请选择有效的全局接入操作。')
    installing = action == 'install'
    home = Path(home_dir) if home_dir is not None else Path.home()
    claude_entry, codex_launch, codex_hook_cmd = _expected_global_entries(port)

    # 1. Claude user-level ~/.claude.json (MCP only, zero hooks)
    claude_path = home / '.claude.json'
    claude_raw = claude_path.read_bytes() if claude_path.is_file() else None
    claude_data = read_json(claude_raw)
    servers = claude_data.setdefault('mcpServers', {})
    if 'team-hub' in servers and servers['team-hub'] != claude_entry:
        existing_mcp = servers['team-hub']
        is_hub = (isinstance(existing_mcp, dict)
                  and isinstance(existing_mcp.get('args'), list)
                  and any('desktop_inbox.py' in str(a) for a in existing_mcp['args']))
        if not is_hub:
            raise Conflict('用户级 ~/.claude.json 已有其他同名 team-hub MCP 配置，请先检查。')
    if installing:
        servers['team-hub'] = claude_entry
    else:
        servers.pop('team-hub', None)
    claude_after = encode_json(claude_data) if (claude_raw is not None or installing) else None

    # 2. Codex user-level ~/.codex/config.toml (MCP only)
    codex_cfg_path = home / '.codex' / 'config.toml'
    codex_cfg_raw = codex_cfg_path.read_bytes() if codex_cfg_path.is_file() else None
    codex_cfg_after = mcp_config(codex_cfg_raw, codex_launch, installing, allow_replace=True)

    # 3. Codex user-level ~/.codex/hooks.json (PreToolUse ticket only, zero SessionStart/UserPromptSubmit)
    codex_hooks_path = home / '.codex' / 'hooks.json'
    codex_hooks_raw = codex_hooks_path.read_bytes() if codex_hooks_path.is_file() else None
    codex_hooks_data = read_json(codex_hooks_raw)
    register_hooks(
        codex_hooks_data,
        {'PreToolUse': '^mcp__team_hub__team_(send|members|whoami)$'},
        codex_hook_cmd,
        installing,
        product='codex',
        team_id='global',
    )
    codex_hooks_after = encode_json(codex_hooks_data) if codex_hooks_data else None

    plan = [
        (claude_path, claude_raw, claude_after),
        (codex_cfg_path, codex_cfg_raw, codex_cfg_after),
        (codex_hooks_path, codex_hooks_raw, codex_hooks_after),
    ]
    written = []
    try:
        for path, before, after in plan:
            if before != after:
                _backup_once(path)
                write_file(path, after)
                written.append((path, before, after))
    except (OSError, ValueError):
        for path, before, after in reversed(written):
            if (path.read_bytes() if path.is_file() else None) == after:
                write_file(path, before)
        raise

    return {'updated': True, 'global_access': global_access_status(port, home_dir=home)}
