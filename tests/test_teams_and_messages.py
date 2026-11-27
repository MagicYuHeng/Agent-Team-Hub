import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hub import HubServer, TaskStore
from integrations.antigravity.team_relay import handle as handle_antigravity_tool, open_result
from probes.desktop_member import DesktopMember, hub_token as desktop_hub_token
from project_agent import request as agent_request

try:
    from tests._helpers import HubTestCase, TOKEN
except ImportError:
    from _helpers import HubTestCase, TOKEN

class TeamsAndMessagesTest(HubTestCase):
    def test_fresh_hub_and_peer_messages(self):
        ctrl = self.api('/control')
        self.assertEqual(ctrl['teams'], [])
        self.assertEqual(ctrl['alerts'], [])
        self.assertIn('global_access', ctrl)
        first, _ = self.team('Audio')
        second, _ = self.team('Documentation')
        alice = self.member(first, '执行甲', 'claude', '负责音频分支')
        bob = self.member(first, '执行乙', 'claude', '负责多轨混音')
        outsider = self.member(second, '其他成员', 'claude')
        a, b, c = map(self.desktop, (alice, bob, outsider))

        roster = self.api('/desktop/roster', a)['members']
        self.assertEqual({(m['name'], m['role'], m['responsibility']) for m in roster},
                         {('执行甲', 'worker', '负责音频分支'), ('执行乙', 'worker', '负责多轨混音')})
        self.api('/desktop/send', a | {'to': '其他成员', 'text': '跨组测试'}, 409)
        self.api('/desktop/send', c | {'to': '执行乙', 'text': '跨组测试'}, 409)
        self.api('/desktop/send', a | {'to': '执行乙', 'text': '初次正文', 'from': '冒充成员',
                                     'request_id': 'repeated'}, 202)
        self.api('/desktop/send', a | {'to': '执行乙', 'text': '初次正文', 'request_id': 'repeated'}, 202)
        one = self.api('/desktop/poll', b)['delivery']
        self.assertEqual(one['prompt'], '【来自 执行甲】\n当前团队组织有变化\n\n初次正文')
        self.assertEqual(self.api('/desktop/poll', b)['delivery'], one)
        self.api('/desktop/received', c | {'id': one['id'], 'revision': one['revision']}, 409)
        self.api('/desktop/received', b | {'id': one['id'], 'revision': one['revision']})
        self.assertIsNone(self.api('/desktop/poll', b)['delivery'])

        self.api('/desktop/send', b | {'to': '执行甲', 'text': '互发确认'}, 202)
        self.assertIn('互发确认', self.api('/desktop/poll', a)['delivery']['prompt'])
        self.api('/desktop/send', a | {'to': '执行乙', 'text': '后续正文'}, 202)
        two = self.api('/desktop/poll', b)['delivery']
        self.assertEqual(two['prompt'], '【来自 执行甲】\n后续正文')
        self.member(first, '音频执行', 'claude', '负责实时音频分支', member=alice)
        self.api('/desktop/received', b | {'id': two['id'], 'revision': two['revision']})
        self.api('/desktop/send', a | {'to': '执行乙', 'text': '改名之后'}, 202)
        three = self.api('/desktop/poll', b)['delivery']
        self.assertEqual(three['prompt'], '【来自 音频执行】\n当前团队组织有变化\n\n改名之后')
        self.assertIn('负责实时音频分支', [m['responsibility'] for m in self.api('/desktop/roster', b)['members']])

    def test_migration_preserves_custom_team_and_historical_tasks(self):
        path = self.root / 'legacy.db'
        with closing(sqlite3.connect(path)) as old:
            old.executescript("""
                CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, captain TEXT, active INTEGER, archived INTEGER);
                CREATE TABLE roster(team_id INTEGER, member TEXT, role TEXT, alias TEXT,
                                    PRIMARY KEY(team_id,member));
                CREATE TABLE member_controls(name TEXT PRIMARY KEY, paused INTEGER DEFAULT 0, instance TEXT, rebind_code TEXT);
                CREATE TABLE tasks(task_id TEXT UNIQUE,request_id TEXT UNIQUE,instruction TEXT,status TEXT,result TEXT,
                                   session_id TEXT,acknowledged INTEGER,recipient TEXT,worker_instance TEXT,team_id INTEGER);
                CREATE TABLE antigravity_windows(team_id INTEGER PRIMARY KEY,instance TEXT,route TEXT,label TEXT,
                                                 code TEXT,pending_label TEXT,last_seen REAL);
                INSERT INTO teams VALUES(1,'默认团队','antigravity',0,0),(2,'Personal','antigravity',1,0);
                INSERT INTO roster VALUES(1,'claude','worker',NULL),(1,'ds-main-worker','worker',NULL),
                    (1,'ds-voice-worker','worker',NULL),(2,'custom','reviewer','用户自定义');
                INSERT INTO member_controls(name) VALUES('custom');
                INSERT INTO tasks VALUES('id','request','old task','completed','old result',NULL,1,'claude',NULL,1);
                INSERT INTO antigravity_windows VALUES(2,'window','route','主窗口',NULL,NULL,0);
            """)
        store = TaskStore(path)
        teams = store.console_state({})['teams']
        self.assertEqual([team['name'] for team in teams], ['Personal'])
        self.assertEqual({member['name'] for member in teams[0]['members']}, {'用户自定义', '主窗口'})
        self.assertEqual(store.teams.antigravity_window(route='route')['name'], 'Personal')
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT result FROM tasks').fetchone()[0], 'old result')
        self.assertEqual(len(TaskStore(path).teams.list()[0]['members']), 2)

    def test_member_independent_roles_models_and_dynamic_refresh(self):
        team, project = self.team('MultiRole')
        (project / '.ai').mkdir()
        (project / '.ai' / 'CURRENT.md').write_text('# Current\n', encoding='utf-8')
        cx_rev = self.member(team, 'codex-rev', 'codex', '只读审查代码',
                             role='reviewer', model='gpt-6.2-sol', worktree='next')
        cx_help = self.member(team, 'codex-help', 'codex', '专项排障协助',
                              role='helper', model='gpt-6.2-sol', worktree='voice-pipeline')
        cc_work = self.member(team, 'next-worker', 'claude', '负责主分支开发',
                              role='worker', model='deepseek-v4-flash', worktree='next')
        ag_cap = self.member(team, 'Captain', 'antigravity', '负责整体统筹',
                             role='captain', model='Gemini-4-Hydrogen')

        s_rev, s_help, s_unbound = (str(uuid.uuid4()) for _ in range(3))
        s_cc, s_ag = str(uuid.uuid4()), str(uuid.uuid4())
        self.store.bind_codex(cx_rev, s_rev, project / 'r.jsonl')
        self.store.bind_codex(cx_help, s_help, project / 'h.jsonl')
        cc_bind = self.desktop(cc_work, session_id=s_cc)
        ag_code = self.api('/control/antigravity', {'team_id': team, 'member': ag_cap})['code']
        ag_proof = self.api('/antigravity/open', {'conversation_id': s_ag})['proof']
        ag_route = self.api('/antigravity/bind', {
            'instance': str(uuid.uuid4()), 'code': ag_code, 'proof': ag_proof})['route']
        self.api('/control/project', {'team_id': team, 'action': 'install'})

        # Console /control preserves model labels for human operators, while agent-facing roster omits model.
        ctrl_members = {m['name']: m for m in self.api('/control')['teams'][0]['members']}
        self.assertEqual(ctrl_members['next-worker']['model'], 'deepseek-v4-flash')
        self.assertEqual(ctrl_members['Captain']['model'], 'Gemini-4-Hydrogen')

        roster = self.api('/desktop/roster', cc_bind)['members']
        by_name = {m['name']: m for m in roster}
        self.assertEqual(by_name['codex-rev']['role'], 'reviewer')
        self.assertEqual(by_name['codex-help']['role'], 'helper')
        self.assertEqual(by_name['next-worker']['worktree'], 'next')
        self.assertNotIn('model', by_name['next-worker'])
        self.assertNotIn('model', by_name['Captain'])

        # Dynamic hook context per session + UserPromptSubmit version-gated refresh.
        ctx_rev = self.api('/project/context', {
            'team_id': team, 'product': 'codex', 'session_id': s_rev, 'event': 'compact', 'paths': [str(project)]})
        ctx_help = self.api('/project/context', {
            'team_id': team, 'product': 'codex', 'session_id': s_help, 'event': 'compact', 'paths': [str(project)]})
        self.assertEqual(ctx_rev['role'], 'reviewer')
        self.assertEqual(ctx_help['role'], 'helper')
        self.assertTrue(ctx_rev['has_ai'])
        self.assertIn('Reviewer', ctx_rev['profile'])
        self.assertIn('Helper', ctx_help['profile'])
        self.assertIn('小队协作约定', ctx_rev['team_rules'])
        # Unbound third Codex chat gets empty context.
        self.assertEqual(self.api('/project/context', {
            'team_id': team, 'product': 'codex', 'session_id': s_unbound,
            'event': 'startup', 'paths': [str(project)]}), {})
        # Same version on UserPromptSubmit stays silent (protects prefix cache).
        self.assertEqual(self.api('/project/context', {
            'team_id': team, 'product': 'codex', 'session_id': s_rev,
            'event': 'UserPromptSubmit', 'paths': [str(project)]}), {})
        # Updating role bumps role_revision and triggers refresh on next UserPromptSubmit.
        self.member(team, 'codex-rev', 'codex', '切换为审查兼协作者', member=cx_rev,
                    role='reviewer+helper', model='gpt-6.2-sol', worktree='next')
        refreshed = self.api('/project/context', {
            'team_id': team, 'product': 'codex', 'session_id': s_rev,
            'event': 'UserPromptSubmit', 'paths': [str(project)]})
        self.assertEqual(refreshed['mode'], 'refresh')
        self.assertEqual(refreshed['role'], 'reviewer+helper')
        self.assertEqual(refreshed['role_revision'], 2)
        # Subsequent prompt with unchanged revision is silent again.
        self.assertEqual(self.api('/project/context', {
            'team_id': team, 'product': 'codex', 'session_id': s_rev,
            'event': 'UserPromptSubmit', 'paths': [str(project)]}), {})
        # Updating only the console model label does NOT bump role_revision or re-inject hook context.
        self.member(team, 'codex-rev', 'codex', '切换为审查兼协作者', member=cx_rev,
                    role='reviewer+helper', model='gpt-6.3-label-only', worktree='next')
        self.assertEqual(self.api('/project/context', {
            'team_id': team, 'product': 'codex', 'session_id': s_rev,
            'event': 'UserPromptSubmit', 'paths': [str(project)]}), {})
        ctrl_after = {m['name']: m for m in self.api('/control')['teams'][0]['members']}
        self.assertEqual(ctrl_after['codex-rev']['model'], 'gpt-6.3-label-only')
        self.assertEqual(ctrl_after['codex-rev']['role_revision'], 2)

        # Verify team_whoami across all three clients (omits model field).
        cc_me = self.api('/desktop/whoami', cc_bind)
        self.assertEqual((cc_me['name'], cc_me['role'], cc_me['worktree']),
                         ('next-worker', 'worker', 'next'))
        self.assertNotIn('model', cc_me)
        ag_out = {}
        handle_antigravity_tool('team_whoami', {'route': ag_route}, 7,
                               lambda m, p, a, timeout=4: (200, self.api(p, a)),
                               lambda rid, txt, struct, is_error=False: ag_out.update(text=txt, struct=struct))
        self.assertNotIn('model', ag_out['struct'])
        self.assertEqual(json.loads(ag_out['text'])['role'], 'captain')
        cx_ticket = self.api('/project/ticket', {
            'team_id': team, 'paths': [str(project)], 'session_id': s_rev, 'tool': 'whoami'})['ticket']
        cx_me = self.api('/project/tool', {'ticket': cx_ticket})
        self.assertEqual((cx_me['name'], cx_me['role'], cx_me['role_revision']),
                         ('codex-rev', 'reviewer+helper', 2))

    def test_role_migration_preserves_effective_product_roles(self):
        path = self.root / 'pre-role-rev.db'
        with closing(sqlite3.connect(path)) as old:
            old.executescript("""
                CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, captain TEXT, active INTEGER, archived INTEGER,
                                    workspace TEXT, roles TEXT, revision INTEGER DEFAULT 0);
                CREATE TABLE roster(team_id INTEGER, member TEXT, role TEXT, alias TEXT, product TEXT,
                                    responsibility TEXT DEFAULT '', seen_revision INTEGER DEFAULT 0,
                                    PRIMARY KEY(team_id,member));
                CREATE TABLE member_controls(name TEXT PRIMARY KEY, paused INTEGER DEFAULT 0, instance TEXT, rebind_code TEXT);
                CREATE TABLE tasks(task_id TEXT UNIQUE,request_id TEXT UNIQUE,instruction TEXT,status TEXT,result TEXT,
                                   session_id TEXT,acknowledged INTEGER,recipient TEXT,worker_instance TEXT,team_id INTEGER);
                INSERT INTO teams VALUES(2,'Companion','ag1',1,0,NULL,'{"codex":"helper","claude":"reviewer"}',5);
                INSERT INTO roster VALUES(2,'m1','worker','成员A','codex','职责A',5),
                                         (2,'m2','worker','成员B','claude','职责B',5);
                INSERT INTO member_controls(name) VALUES('m1'),('m2');
            """)
        store = TaskStore(path)
        team = store.teams.list()[0]
        by_name = {m['name']: m for m in team['members']}
        self.assertEqual(by_name['成员A']['role'], 'helper')
        self.assertEqual(by_name['成员B']['role'], 'reviewer')
        self.assertEqual(by_name['成员A']['role_revision'], 1)

    def test_peer_team_cross_project_and_global_access(self):
        from workspace import configure_global_access, global_access_status

        # 1. Existing structured team defaults to mode='structured'; peer team needs no workspace.
        structured_ws = self.root / 'structured-proj'
        structured_ws.mkdir()
        st = self.api('/control/team', {
            'action': 'create', 'name': 'Structured-Team', 'workspace': str(structured_ws),
        })
        self.assertEqual(st['mode'], 'structured')

        peer = self.api('/control/team', {'action': 'create', 'mode': 'peer', 'name': '跨项目联调'})
        peer_id = peer['team_id']
        self.assertEqual(peer['mode'], 'peer')

        teams_by_id = {t['id']: t for t in self.api('/control')['teams']}
        self.assertEqual(teams_by_id[st['team_id']]['mode'], 'structured')
        self.assertEqual(teams_by_id[peer_id]['mode'], 'peer')
        self.assertEqual(teams_by_id[peer_id]['workspace'], '')
        self.assertEqual(teams_by_id[peer_id]['worktrees'], [])

        # Peer team blocks workspace/hook/git_identity/docs operations.
        self.api('/control/project', {'team_id': peer_id, 'action': 'install'}, 409)
        self.api('/control/team', {
            'action': 'workspace', 'team_id': peer_id, 'workspace': str(structured_ws), 'roles': {},
        }, 409)
        self.api('/control/team', {
            'action': 'git_identity', 'team_id': peer_id,
            'git_author_name': 'A', 'git_author_email': 'a@example.com',
        }, 409)
        self.api('/control/doc', {'team_id': peer_id, 'action': 'agents_overview'}, 409)

        # 2. Peer members accept optional note (including empty) and omit role/worktree in roster & whoami.
        ag = self.api('/control/team', {
            'action': 'member', 'team_id': peer_id, 'name': '前端项目',
            'product': 'antigravity', 'note': 'Web 客户端',
        })['member']
        cdx = self.api('/control/team', {
            'action': 'member', 'team_id': peer_id, 'name': '后端网关',
            'product': 'codex', 'note': '',
        })['member']
        code = self.api('/control/antigravity', {'team_id': peer_id, 'member': ag, 'label': 'AG'})['code']
        ag_inst = str(uuid.uuid4())
        route = self.api('/antigravity/bind', {'instance': ag_inst, 'code': code})['route']

        thread_id = str(uuid.uuid4())
        rollout = self.root / f'{thread_id}.jsonl'
        rollout.write_text('{}\n', encoding='utf-8')
        self.store.bind_codex(cdx, thread_id, rollout)

        roster = self.api('/antigravity/roster', {'route': route})
        self.assertEqual(roster['mode'], 'peer')
        by_name = {m['name']: m for m in roster['members']}
        self.assertEqual(set(by_name.keys()), {'前端项目', '后端网关'})
        self.assertEqual(by_name['前端项目']['note'], 'Web 客户端')
        self.assertEqual(by_name['后端网关']['note'], '')
        for m in roster['members']:
            self.assertNotIn('role', m)
            self.assertNotIn('worktree', m)
            self.assertNotIn('role_revision', m)

        me = self.api('/antigravity/whoami', {'route': route})
        self.assertEqual(me['mode'], 'peer')
        self.assertEqual(me['name'], '前端项目')
        self.assertEqual(me['note'], 'Web 客户端')
        self.assertNotIn('role', me)
        self.assertNotIn('worktree', me)

        # 3. Codex global PreToolUse ticket works from any unrelated directory with zero identity injection.
        unrelated_dir = self.root / 'unrelated-backend'
        unrelated_dir.mkdir()
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ | {'TEAM_HUB_TOKEN': TOKEN}
        port = self.server.server_port
        start_out = subprocess.run(
            [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', 'global', str(port)],
            input=json.dumps({'cwd': str(unrelated_dir), 'session_id': thread_id, 'hook_event_name': 'SessionStart'}),
            text=True, capture_output=True, encoding='utf-8', env=env, check=True,
        )
        self.assertEqual(start_out.stdout.strip(), '')

        pre_out = subprocess.run(
            [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', 'global', str(port)],
            input=json.dumps({
                'cwd': str(unrelated_dir), 'session_id': thread_id,
                'hook_event_name': 'PreToolUse', 'tool_name': 'mcp__team_hub__team_send',
                'tool_input': {'to': '前端项目', 'text': '接口字段已对齐'},
            }),
            text=True, capture_output=True, encoding='utf-8', env=env, check=True,
        )
        parsed = json.loads(pre_out.stdout)
        ticket = parsed['hookSpecificOutput']['updatedInput']['team_hub_ticket']
        sent = self.api('/project/tool', {'ticket': ticket, 'to': '前端项目', 'text': '接口字段已对齐'}, 202)
        self.assertEqual(sent['status'], 'queued')

        # 4. Delivery preserves one-time roster-change notice.
        poll1 = self.api('/antigravity/poll', {'instance': ag_inst})
        self.assertEqual(poll1['mode'], 'peer')
        self.assertIn('【来自 后端网关】', poll1['delivery']['prompt'])
        self.assertIn('当前团队组织有变化', poll1['delivery']['prompt'])
        self.api('/antigravity/received', {
            'route': route, 'id': poll1['delivery']['id'], 'revision': poll1['delivery']['revision'],
        })

        # Second message without roster change should not repeat the notice.
        self.api('/control/message/send', {'team_id': peer_id, 'to': '前端项目', 'text': '第二条消息'}, 202)
        poll2 = self.api('/antigravity/poll', {'instance': ag_inst})
        self.assertNotIn('当前团队组织有变化', poll2['delivery']['prompt'])

        # 5. User-level global access install & remove preserves existing user configs and injects zero identity hooks.
        fake_home = self.root / 'fake-home'
        fake_home.mkdir()
        (fake_home / '.claude.json').write_text(
            json.dumps({'mcpServers': {'existing': {'command': 'echo'}}}), encoding='utf-8',
        )
        (fake_home / '.codex').mkdir()
        (fake_home / '.codex' / 'config.toml').write_text(
            '[mcp_servers.other]\ncommand = "echo"\n', encoding='utf-8',
        )

        st0 = global_access_status(port, home_dir=fake_home)
        self.assertFalse(st0['ready'])

        res_in = configure_global_access('install', port, home_dir=fake_home)
        self.assertTrue(res_in['global_access']['ready'])
        claude_cfg = json.loads((fake_home / '.claude.json').read_text(encoding='utf-8'))
        self.assertIn('existing', claude_cfg['mcpServers'])
        self.assertIn('team-hub', claude_cfg['mcpServers'])
        codex_hooks = json.loads((fake_home / '.codex' / 'hooks.json').read_text(encoding='utf-8'))
        self.assertEqual(set(codex_hooks['hooks'].keys()), {'PreToolUse', 'PostToolUse', 'Stop'})
        self.assertNotIn('SessionStart', codex_hooks['hooks'])
        self.assertNotIn('UserPromptSubmit', codex_hooks['hooks'])
        self.assertTrue(res_in['global_access']['codex_steer_hook'])

        res_rm = configure_global_access('remove', port, home_dir=fake_home)
        self.assertFalse(res_rm['global_access']['ready'])
        claude_after = json.loads((fake_home / '.claude.json').read_text(encoding='utf-8'))
        self.assertIn('existing', claude_after['mcpServers'])
        self.assertNotIn('team-hub', claude_after['mcpServers'])
        self.assertFalse((fake_home / '.codex' / 'hooks.json').exists())

if __name__ == '__main__':
    unittest.main()
