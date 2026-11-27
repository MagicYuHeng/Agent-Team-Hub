import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from contextlib import closing
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from hub import HubServer, TaskStore
from integrations.antigravity.team_relay import handle as handle_antigravity_tool, open_result
from probes.desktop_member import DesktopMember, hub_token as desktop_hub_token
from project_agent import request as agent_request


TOKEN = 'isolated-team-hub-test-token-0123456'


class TeamHubTest(unittest.TestCase):
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

    def test_antigravity_separate_named_members(self):
        first, _ = self.team('Design')
        left = self.member(first, '策划', 'antigravity')
        right = self.member(first, '审稿', 'antigravity')
        routes = []
        for key in (left, right):
            code = self.api('/control/antigravity', {'team_id': first, 'member': key})['code']
            instance = str(uuid.uuid4())
            route = self.api('/antigravity/bind', {'code': code, 'instance': instance})['route']
            routes.append((instance, route))
        self.api('/antigravity/send', {'route': routes[1][1], 'to': '策划', 'text': '草案已审'}, 202)
        result = {}

        def hub_post(method, path, args, timeout):
            return 200, self.api(path, args)

        def respond(request_id, text, structured):
            result.update(text=text, structured=structured)

        handle_antigravity_tool('team_members', {'route': routes[0][1]}, 1, hub_post, respond)
        visible = json.loads(result['text'])
        self.assertEqual(visible, result['structured'])
        self.assertEqual({member['name'] for member in visible['members']}, {'策划', '审稿'})
        self.assertEqual(self.api('/antigravity/poll', {'instance': str(uuid.uuid4())}), {'bound': False})
        left_box = self.api('/antigravity/poll', {'instance': routes[0][0]})
        self.assertTrue(left_box['delivery']['prompt'].startswith('【来自 审稿】'))
        self.assertIsNone(self.api('/antigravity/poll', {'instance': routes[1][0]})['delivery'])

    @unittest.skipUnless(os.name == 'nt', 'Windows user environment fallback')
    def test_codex_mcp_authenticates_with_filtered_environment(self):
        import winreg

        args = {'team_id': 0, 'product': 'codex', 'paths': []}
        with (patch.dict(os.environ, clear=True),
              patch.object(winreg, 'QueryValueEx', return_value=(TOKEN, winreg.REG_SZ))):
            self.assertEqual(agent_request(self.server.server_port, 'context', args), {})

    def test_antigravity_reconnects_only_to_remembered_conversation(self):
        team, _ = self.team('Restore')
        seat = self.member(team, '收件人', 'antigravity')
        sender = self.desktop(self.member(team, '发送人', 'claude'))
        native_a, native_b = str(uuid.uuid4()), str(uuid.uuid4())
        original, reopened, unrelated = (str(uuid.uuid4()) for _ in range(3))

        def proof_for(native):
            def post(method, path, args, timeout):
                return 200, self.api(path, args)
            result = open_result({'antigravity.google/conversation_id': native}, post)
            proof = result['_meta']['team_hub_receive_proof']
            self.assertNotIn(proof, json.dumps(result['content']))
            self.assertNotIn(proof, json.dumps(result['structuredContent']))
            return proof

        self.assertNotIn('_meta', open_result({'conversation_id': native_a}, None))
        code = self.api('/control/antigravity', {'team_id': team, 'member': seat})['code']
        proof = proof_for(native_a)
        self.assertFalse(self.api('/antigravity/restore', {'instance': original, 'proof': proof})['bound'])
        bound = self.api('/antigravity/bind', {'instance': original, 'proof': proof, 'code': code})
        self.api('/desktop/send', sender | {'to': '收件人', 'text': '仅投递至原会话'}, 202)
        second_seat = self.member(team, '另一位', 'antigravity')
        second_code = self.api('/control/antigravity', {'team_id': team, 'member': second_seat})['code']
        self.api('/antigravity/bind', {'instance': unrelated, 'code': second_code,
                                      'proof': proof_for(native_a)}, 409)
        self.assertFalse(self.api('/antigravity/restore', {
            'instance': unrelated, 'proof': proof_for(native_b)})['bound'])
        self.assertFalse(self.api('/antigravity/restore', {
            'instance': unrelated, 'conversation_id': native_a})['bound'])
        self.assertEqual(self.api('/antigravity/poll', {'instance': unrelated}), {'bound': False})

        # Use a fresh store and proof to simulate an App/Hub restart.
        self.server.store = TaskStore(self.root / 'hub.db')
        self.server.antigravity_proofs.clear()
        proof = proof_for(native_a)
        restored = self.api('/antigravity/restore', {'instance': reopened, 'proof': proof})
        self.assertEqual(restored, {'bound': True, 'route': bound['route']})
        self.assertEqual(self.api('/antigravity/poll', {'instance': original}), {'bound': False})
        self.assertFalse(self.api('/antigravity/restore', {
            'instance': unrelated, 'proof': proof})['bound'])
        delivery = self.api('/antigravity/poll', {'instance': reopened})['delivery']
        self.assertIn('仅投递至原会话', delivery['prompt'])
        self.api('/antigravity/received', {'route': restored['route'],
                                         'id': delivery['id'], 'revision': delivery['revision']})
        self.assertIsNone(self.api('/antigravity/poll', {'instance': reopened})['delivery'])

    def test_project_hooks_and_codex_tool_identity(self):
        team, project = self.team('Review')
        other, _ = self.team('Other')
        source = self.member(team, '复核', 'codex', '复核改动与协助调试', model='gpt-6.2-sol')
        target = self.member(team, '开发', 'claude', model='deepseek-v4-flash')
        captain = self.member(team, 'Captain', 'antigravity', model='Gemini-4-Hydrogen')
        test_session = str(uuid.uuid4())
        claude_session = str(uuid.uuid4())
        ag_session = str(uuid.uuid4())
        self.store.bind_codex(source, test_session, project / 'simulated.jsonl')
        desktop = self.desktop(target, session_id=claude_session)
        ag_code = self.api('/control/antigravity', {'team_id': team, 'member': captain})['code']
        ag_proof = self.api('/antigravity/open', {'conversation_id': ag_session})['proof']
        self.api('/antigravity/bind', {'instance': str(uuid.uuid4()), 'code': ag_code, 'proof': ag_proof})
        self.member(other, '隔壁', 'claude')
        existing = project / '.claude/settings.local.json'
        existing.parent.mkdir()
        original = b'{"permissions": {"allow": ["Read"]}}\r\n'
        existing.write_bytes(original)
        base = {'team_id': team, 'action': 'install'}
        self.api('/control/project', base)
        self.api('/control/project', base)
        self.assertTrue((project / '.codex/config.toml').is_file())
        env = os.environ | {'TEAM_HUB_TOKEN': TOKEN, 'PYTHONIOENCODING': 'utf-8'}
        script = Path('project_agent.py').resolve()

        def hook(product, **event):
            payload = {'cwd': str(project), 'session_id': test_session} | event
            ran = subprocess.run([sys.executable, str(script), 'hook', product,
                                  str(team), str(self.server.server_port)],
                                 input=json.dumps(payload), capture_output=True, encoding='utf-8', env=env, check=True)
            return json.loads(ran.stdout) if ran.stdout else {}

        role = hook('codex', hook_event_name='SessionStart', source='compact')
        self.assertIn('reviewer+helper', role['hookSpecificOutput']['additionalContext'])
        self.assertIn('复核改动与协助调试', role['hookSpecificOutput']['additionalContext'])
        self.assertIn('gpt-6.2-sol', role['hookSpecificOutput']['additionalContext'])
        claude_ctx = hook('claude', session_id=claude_session, hook_event_name='SessionStart',
                          source='resume')['hookSpecificOutput']['additionalContext']
        self.assertIn('worker', claude_ctx)
        self.assertIn('deepseek-v4-flash', claude_ctx)
        self.assertIn('Claude Code 框架下的其他模型通道', claude_ctx)
        self.assertIn('项目内不限（注意避免多线写入冲突）', claude_ctx)
        self.assertIn('团队公共协作规则', claude_ctx)
        # Non-Claude hooks should NOT include the Claude-specific framework notice.
        self.assertNotIn('Claude Code 框架下的其他模型通道', role['hookSpecificOutput']['additionalContext'])
        # Unbound session receives empty context.
        self.assertEqual(hook('claude', session_id=str(uuid.uuid4()),
                              hook_event_name='SessionStart', source='startup'), {})
        antigravity = hook('antigravity', workspacePaths=[str(project)],
                           conversationId=ag_session, hookEventName='SessionStart')
        self.assertIn('captain', antigravity['injectSteps'][0]['systemMessage']['systemMessage'])
        self.assertIn('Gemini-4-Hydrogen', antigravity['injectSteps'][0]['systemMessage']['systemMessage'])
        # Same version on PreInvocation returns empty JSON object without re-injecting.
        self.assertEqual(hook('antigravity', workspacePaths=[str(project)],
                              conversationId=ag_session, hookEventName='PreInvocation'), {})
        # Updating member bumps version and triggers PreInvocation injection.
        self.member(team, 'Captain', 'antigravity', '更新统筹职责', member=captain,
                    role='captain', model='Gemini-4-Hydrogen')
        ag_refreshed = hook('antigravity', workspacePaths=[str(project)],
                            conversationId=ag_session, hookEventName='PreInvocation')
        self.assertIn('已更新（v2）', ag_refreshed['injectSteps'][0]['systemMessage']['systemMessage'])
        self.assertEqual(hook('codex', cwd=str(self.root), hook_event_name='SessionStart'), {})
        installed = json.loads((project / '.codex/hooks.json').read_text(encoding='utf-8'))
        command = installed['hooks']['SessionStart'][0]['hooks'][0]['command']
        self.assertNotIn('\\', command)
        launch = subprocess.run(command, shell=True, input=json.dumps({
            'cwd': str(project), 'session_id': test_session,
            'hook_event_name': 'SessionStart', 'source': 'resume',
        }), capture_output=True, encoding='utf-8', env=env, check=True)
        self.assertIn('reviewer+helper', json.loads(launch.stdout)['hookSpecificOutput']['additionalContext'])
        self.api('/project/tool', {'ticket': 'a' * 32, 'to': '开发', 'text': '错误凭据'}, 409)
        changed = hook('codex', hook_event_name='PreToolUse', tool_name='mcp__team_hub__team_send',
                       tool_input={'to': '开发', 'text': '已检查边界情况'})
        args = changed['hookSpecificOutput']['updatedInput']
        token = args.pop('team_hub_ticket')
        frame = {'id': 1, 'method': 'tools/call', 'params': {'name': 'team_send',
                 'arguments': args | {'team_hub_ticket': token}}}
        result = subprocess.run([sys.executable, str(script), 'mcp', str(self.server.server_port)],
                                input=json.dumps(frame) + '\n', capture_output=True, encoding='utf-8',
                                env=env, check=True)
        self.assertFalse(json.loads(result.stdout)['result']['isError'])
        self.assertIn('已检查边界情况', self.api('/desktop/poll', desktop)['delivery']['prompt'])
        self.api('/project/tool', {'ticket': token, 'to': '开发', 'text': '重放'}, 409)
        config = project / '.codex/hooks.json'
        data = json.loads(config.read_text(encoding='utf-8'))
        data['description'] = 'user edit during deploy'
        config.write_text(json.dumps(data), encoding='utf-8')
        self.api('/control/project', base | {'action': 'remove'})
        self.assertEqual(existing.read_bytes(), original)
        self.assertEqual(json.loads(config.read_text(encoding='utf-8')), {'description': 'user edit during deploy'})
        self.assertFalse((project / '.codex/config.toml').exists())

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

    def test_claude_desktop_auto_restores_only_remembered_session(self):
        team, _ = self.team('ClaudeRestore')
        seat = self.member(team, '主执行', 'claude', '负责主线实现')
        other_seat = self.member(team, '次执行', 'claude', '负责测试')
        sender = self.desktop(self.member(team, '协作者', 'claude'))
        session_a, session_b, session_c = (str(uuid.uuid4()) for _ in range(3))
        original_instance = str(uuid.uuid4())

        code = self.api('/control/rebind', {'member': seat})['code']
        self.api('/desktop/bind', {
            'member': seat, 'instance': original_instance, 'code': code, 'session_id': session_a})
        rev_after_bind = self.api('/control')['teams'][0]['revision']

        # The same native Claude session cannot bind a second member.
        other_code = self.api('/control/rebind', {'member': other_seat})['code']
        self.api('/desktop/bind', {
            'member': other_seat, 'instance': str(uuid.uuid4()), 'code': other_code, 'session_id': session_a}, 409)
        # Unrelated session does not restore.
        self.assertEqual(self.api('/desktop/restore', {
            'instance': str(uuid.uuid4()), 'session_id': session_b}), {'bound': False})

        # Queue a message while the Claude Desktop member is offline across Hub + Desktop restart.
        self.api('/desktop/send', sender | {'to': '主执行', 'text': '重启后自动投递消息'}, 202)
        self.server.store = TaskStore(self.root / 'hub.db')
        self.server.members.clear()

        delivered = []
        env = {
            'TEAM_HUB_TOKEN': TOKEN,
            'CLAUDE_CODE_MESSAGING_SOCKET': r'\\.\pipe\LOCAL\cc-msg-0123456789abcdef0123456789abcdef',
            'CLAUDE_CODE_MESSAGING_TOKEN': 'pipe-secret',
            'CLAUDE_CODE_SESSION_ID': session_a,
        }
        with (patch.dict(os.environ, env, clear=True),
              patch('probes.desktop_member.TRACE_LOG', self.root / 'desktop-delivery.log'),
              patch('probes.desktop_member.write_inbox', side_effect=lambda *a: delivered.append(a))):
            restarted = DesktopMember(self.server.server_port)
            # Start without calling bind_team_member: captain_request/try_restore recovers automatically.
            roster = restarted.captain_request('roster')
            self.assertEqual(restarted.name, seat)
            self.assertIn('主执行', [m['name'] for m in roster['members']])
            # Restoring the same native session does not bump roster revision.
            self.assertEqual(self.api('/control')['teams'][0]['revision'], rev_after_bind)
            # Old instance is rejected after new instance takes over.
            self.api('/desktop/poll', {'member': seat, 'instance': original_instance, 'ready': True}, 409)
            # Wait briefly for the auto-started polling thread to deliver and acknowledge the queued message.
            for _ in range(30):
                if delivered and self.api('/desktop/poll', {
                        'member': seat, 'instance': restarted.instance, 'ready': True})['delivery'] is None:
                    break
                threading.Event().wait(0.1)
            self.assertEqual(len(delivered), 1)
            self.assertIn('重启后自动投递消息', delivered[0][3])

            # Rebinding the member to session_c revokes session_a immediately.
            rebind_code = self.api('/control/rebind', {'member': seat})['code']
            self.api('/desktop/bind', {
                'member': seat, 'instance': str(uuid.uuid4()), 'code': rebind_code, 'session_id': session_c})
            self.assertEqual(self.api('/desktop/restore', {
                'instance': str(uuid.uuid4()), 'session_id': session_a}), {'bound': False})
            restarted.sync()
            self.assertIsNone(restarted.name)

        # Archiving the team clears claude_session_id so the session can join another team later.
        self.api('/control/team', {'action': 'archive', 'team_id': team})
        self.assertEqual(self.api('/desktop/restore', {
            'instance': str(uuid.uuid4()), 'session_id': session_c}), {'bound': False})
        new_team, _ = self.team('NextTeam')
        new_seat = self.member(new_team, '新席位', 'claude')
        new_code = self.api('/control/rebind', {'member': new_seat})['code']
        self.api('/desktop/bind', {
            'member': new_seat, 'instance': str(uuid.uuid4()), 'code': new_code, 'session_id': session_c})

    @unittest.skipUnless(os.name == 'nt', 'Windows user environment fallback')
    def test_claude_desktop_token_windows_fallback(self):
        import winreg

        with (patch.dict(os.environ, clear=True),
              patch.object(winreg, 'QueryValueEx', return_value=(TOKEN, winreg.REG_SZ))):
            self.assertEqual(desktop_hub_token(), TOKEN)

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

        # Roster exposes independent roles, models, worktrees, and role_revision.
        roster = self.api('/desktop/roster', cc_bind)['members']
        by_name = {m['name']: m for m in roster}
        self.assertEqual(by_name['codex-rev']['role'], 'reviewer')
        self.assertEqual(by_name['codex-help']['role'], 'helper')
        self.assertEqual(by_name['next-worker']['model'], 'deepseek-v4-flash')
        self.assertEqual(by_name['next-worker']['worktree'], 'next')
        self.assertEqual(by_name['Captain']['model'], 'Gemini-4-Hydrogen')

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
        self.assertIn('团队公共协作规则', ctx_rev['team_rules'])
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

        # Verify team_whoami across all three clients.
        cc_me = self.api('/desktop/whoami', cc_bind)
        self.assertEqual((cc_me['name'], cc_me['role'], cc_me['model'], cc_me['worktree']),
                         ('next-worker', 'worker', 'deepseek-v4-flash', 'next'))
        ag_out = {}
        handle_antigravity_tool('team_whoami', {'route': ag_route}, 7,
                               lambda m, p, a, timeout=4: (200, self.api(p, a)),
                               lambda rid, txt, struct, is_error=False: ag_out.update(text=txt, struct=struct))
        self.assertEqual(ag_out['struct']['model'], 'Gemini-4-Hydrogen')
        self.assertEqual(json.loads(ag_out['text'])['role'], 'captain')
        cx_ticket = self.api('/project/ticket', {
            'team_id': team, 'paths': [str(project)], 'session_id': s_rev, 'tool': 'whoami'})['ticket']
        cx_me = self.api('/project/tool', {'ticket': cx_ticket})
        self.assertEqual((cx_me['name'], cx_me['role'], cx_me['role_revision']),
                         ('codex-rev', 'reviewer+helper', 2))

    def test_multi_worktree_discovery_sync_and_removal(self):
        repo = self.root / 'multi-repo'
        repo.mkdir()
        git_env = os.environ | {
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        }
        subprocess.run(['git', '-C', str(repo), 'init', '-b', 'main'], check=True, capture_output=True)
        (repo / 'README.md').write_text('hello\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'init'], check=True, capture_output=True, env=git_env)
        wt_next = repo / 'next'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'next', str(wt_next)],
                       check=True, capture_output=True)
        (wt_next / '.ai').mkdir()
        (wt_next / '.ai' / 'CURRENT.md').write_text('# Next\n', encoding='utf-8')

        team = self.api('/control/team', {'action': 'create', 'name': 'WTTeam', 'workspace': str(repo)})['team_id']
        listed = self.api('/control/worktrees', {'team_id': team})['worktrees']
        self.assertEqual([w['relative'] for w in listed], ['', 'next'])
        self.assertTrue(listed[1]['has_ai'])
        self.assertEqual(listed[1]['branch'], 'next')

        # Install deploys consistently across root and child worktree.
        self.api('/control/project', {'team_id': team, 'action': 'install'})
        self.assertTrue((repo / '.codex' / 'hooks.json').is_file())
        self.assertTrue((wt_next / '.codex' / 'hooks.json').is_file())
        self.assertTrue((wt_next / '.mcp.json').is_file())
        synced = self.api('/control/worktrees', {'team_id': team})['worktrees']
        self.assertTrue(all(w['deployed'] and w['up_to_date'] for w in synced))

        # Add a second worktree and sync incrementally.
        wt_voice = repo / 'voice-pipeline'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'voice', str(wt_voice)],
                       check=True, capture_output=True)
        before_sync = {w['relative']: w for w in self.api('/control/worktrees', {'team_id': team})['worktrees']}
        self.assertFalse(before_sync['voice-pipeline']['deployed'])
        self.api('/control/project', {'team_id': team, 'action': 'sync'})
        after_sync = {w['relative']: w for w in self.api('/control/worktrees', {'team_id': team})['worktrees']}
        self.assertTrue(after_sync['voice-pipeline']['deployed'])
        self.assertTrue((wt_voice / '.claude' / 'settings.local.json').is_file())

        # Remove cleans all deployed worktrees cleanly.
        self.api('/control/project', {'team_id': team, 'action': 'remove'})
        for wt in (repo, wt_next, wt_voice):
            self.assertFalse((wt / '.codex' / 'hooks.json').exists())
            self.assertFalse((wt / '.mcp.json').exists())

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

    def test_docs_history_external_alerts_git_sync_ai_preview_and_message_controls(self):
        repo = self.root / 'docs-repo'
        repo.mkdir()
        git_env = os.environ | {
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        }
        subprocess.run(['git', '-C', str(repo), 'init', '-b', 'main'], check=True, capture_output=True)
        root_agents = '# Root\n\n<!-- SHARED-START -->\nshared v1\n<!-- SHARED-END -->\n\nRoot tail\n'
        (repo / 'AGENTS.md').write_text(root_agents, encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', 'AGENTS.md'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'init'], check=True, capture_output=True, env=git_env)
        wt_next = repo / 'next'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'next', str(wt_next)],
                       check=True, capture_output=True)
        (wt_next / 'AGENTS.md').write_text('# Next\n\n<!-- SHARED-START -->\nshared v1\n<!-- SHARED-END -->\n\nNext tail\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(wt_next), 'commit', '--only', '-m', 'next agents', '--', 'AGENTS.md'],
                       check=True, capture_output=True, env=git_env)
        (wt_next / '.ai' / 'handoffs' / 'next').mkdir(parents=True)
        (wt_next / '.ai' / 'CURRENT.md').write_text(
            '# Current\nbranch: next\nwork_commit: 12345678\n\n## Open memos\n- MEM-099\n', encoding='utf-8')
        (wt_next / '.ai' / 'handoffs' / 'next' / '20261125T000000Z__agent__1234567__sample.md').write_text(
            '# Sample Log\nagent: antigravity\nmodel: gemini-4-neon\nwork_commit: 12345678\n', encoding='utf-8')

        team = self.api('/control/team', {'action': 'create', 'name': 'DocsTeam', 'workspace': str(repo)})['team_id']
        worker = self.member(team, 'worker1', 'claude', '测试职责', role='worker', model='deepseek-v4-flash', worktree='')
        cc_bind = self.desktop(worker, session_id=str(uuid.uuid4()))

        # Root worktree displays clear unbounded label in roster & whoami.
        me = self.api('/desktop/whoami', cc_bind)
        self.assertEqual(me['worktree'], '项目内不限（注意避免多线写入冲突）')
        roster_me = self.api('/desktop/roster', cc_bind)['members'][0]
        self.assertEqual(roster_me['worktree'], '项目内不限（注意避免多线写入冲突）')

        # Deploy hooks -> writes .git/info/exclude so Hub files are ignored cleanly.
        self.api('/control/project', {'team_id': team, 'action': 'install'})
        exclude_text = (repo / '.git' / 'info' / 'exclude').read_text(encoding='utf-8')
        self.assertIn('# BEGIN Team Hub managed', exclude_text)
        status_next = subprocess.run(['git', '-C', str(wt_next), 'status', '--short'],
                                     capture_output=True, text=True, check=True).stdout
        self.assertNotIn('.codex/', status_next)
        self.assertNotIn('.mcp.json', status_next)

        # Operator test message & cancel queued message.
        sent = self.api('/control/message/send', {'team_id': team, 'to': 'worker1', 'text': '测试连通'}, 202)
        self.api('/control/message/cancel', {'team_id': team, 'id': sent['id']})
        self.api('/control/message/cancel', {'team_id': team, 'id': sent['id']}, 409)
        self.assertIsNone(self.api('/desktop/poll', cc_bind)['delivery'])

        # .ai/ read-only inspection returns CURRENT summary & recent handoff log.
        ai_info = self.api('/control/worktree-ai', {'team_id': team, 'worktree': 'next'})
        self.assertEqual(ai_info['current']['meta']['work_commit'], '12345678')
        self.assertEqual(ai_info['current']['open_memos'], ['MEM-099'])
        self.assertEqual(len(ai_info['logs']), 1)
        log_detail = self.api('/control/worktree-ai', {
            'team_id': team, 'worktree': 'next', 'log_path': ai_info['logs'][0]['path']})
        self.assertIn('# Sample Log', log_detail['selected_log']['content'])

        # Set Team git identity & baseline AGENTS.md via /control.
        self.api('/control/team', {
            'action': 'git_identity', 'team_id': team,
            'git_author_name': 'Aether Chen', 'git_author_email': 'aether@example.com',
        })
        self.assertEqual(self.api('/control')['alerts'], [])

        # External modification to next/AGENTS.md triggers alert in /control!
        (wt_next / 'AGENTS.md').write_text(
            '# Next\n\n<!-- SHARED-START -->\nexternal edit\n<!-- SHARED-END -->\n\nNext tail\n', encoding='utf-8')
        alerts = self.api('/control')['alerts']
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]['team_name'], alerts[0]['doc_key'], alerts[0]['target']),
                         ('DocsTeam', 'agents', 'next'))
        ext_doc = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'agents', 'worktree': 'next'})
        self.assertTrue(ext_doc['external_changed'])
        self.assertIsNotNone(ext_doc['external_diff'])
        self.assertGreaterEqual(ext_doc['external_diff']['added'], 1)
        self.assertGreaterEqual(ext_doc['external_diff']['removed'], 1)
        # Accept external change clears alert and saves external revision.
        self.api('/control/doc', {'team_id': team, 'action': 'accept_external', 'doc_key': 'agents', 'worktree': 'next'})
        self.assertEqual(self.api('/control')['alerts'], [])

        # Save AGENTS.md with shared-section multi-worktree sync + safe single-file git commit!
        (wt_next / 'unrelated.txt').write_text('dirty file must not be committed\n', encoding='utf-8')
        saved = self.api('/control/doc', {
            'team_id': team, 'action': 'save', 'doc_key': 'agents', 'worktree': '',
            'content': '# Root\n\n<!-- SHARED-START -->\nshared v2\n<!-- SHARED-END -->\n\nRoot tail\n',
            'sync_worktrees': ['next'], 'auto_commit': True, 'commit_worktrees': ['', 'next'],
        })
        self.assertEqual(saved['commits']['.']['status'], 'committed')
        self.assertEqual(saved['commits']['next']['status'], 'committed')
        # Verify next/AGENTS.md kept its local header & tail while updating shared block!
        next_text = (wt_next / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('# Next', next_text)
        self.assertIn('shared v2', next_text)
        self.assertIn('Next tail', next_text)
        # Verify unrelated.txt was NOT included in the AGENTS.md commit.
        committed_files = subprocess.run(['git', '-C', str(wt_next), 'show', '--name-only', '--format=', 'HEAD'],
                                         capture_output=True, text=True, check=True).stdout.strip().splitlines()
        self.assertEqual(committed_files, ['AGENTS.md'])

        # Role profile edit, history rollback, and reset to default (AGENTS.md rejects reset).
        self.api('/control/doc', {'team_id': team, 'action': 'reset', 'doc_key': 'agents'}, 409)
        self.api('/control/doc', {'team_id': team, 'action': 'save', 'doc_key': 'worker', 'content': 'custom worker v1'})
        self.api('/control/doc', {'team_id': team, 'action': 'save', 'doc_key': 'worker', 'content': 'custom worker v2'})
        doc_state = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'worker'})
        self.assertTrue(doc_state['is_override'])
        self.assertGreaterEqual(len(doc_state['revisions']), 2)
        first_rev_id = doc_state['revisions'][-1]['id']
        self.api('/control/doc', {'team_id': team, 'action': 'rollback', 'revision_id': first_rev_id})
        self.assertEqual(self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'worker'})['content'].strip(),
                         'custom worker v1')
        self.api('/control/doc', {'team_id': team, 'action': 'reset', 'doc_key': 'worker'})
        reset_state = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'worker'})
        self.assertFalse(reset_state['is_override'])
        self.assertIn('角色规范：Worker', reset_state['content'])

        # Removing project hooks also cleans .git/info/exclude.
        self.api('/control/project', {'team_id': team, 'action': 'remove'})
        self.assertNotIn('# BEGIN Team Hub managed', (repo / '.git' / 'info' / 'exclude').read_text(encoding='utf-8'))

    def test_agents_grouping_shared_block_similar_search_and_batch_ops(self):
        repo = self.root / 'agents-group-repo'
        repo.mkdir()
        git = lambda *args, cwd=repo: subprocess.run(
            ['git', '-C', str(cwd), *args], capture_output=True, text=True, check=True)
        git('init', '-b', 'main')
        git('config', 'user.name', 'Test User')
        git('config', 'user.email', 'test@example.com')
        (repo / 'AGENTS.md').write_text('# Container Root\n\n仅负责工作树路由。\n', encoding='utf-8')
        git('add', 'AGENTS.md')
        git('commit', '-m', 'init root')

        wt_next = repo / 'next'
        wt_stable = repo / 'stable'
        wt_gw = repo / 'gateway'
        git('worktree', 'add', '-b', 'next', str(wt_next))
        git('worktree', 'add', '-b', 'stable', str(wt_stable))
        git('worktree', 'add', '-b', 'gateway', str(wt_gw))

        (wt_next / 'AGENTS.md').write_text(
            '# Next\n\n- 本工作树密钥存放于./local/(位置A)，禁止提交。\n\n'
            '<!-- SHARED-START -->\n## 协作与交接\n\n- 共享规则 v1。\n<!-- SHARED-END -->\n\nNext tail\n',
            encoding='utf-8',
        )
        (wt_stable / 'AGENTS.md').write_text(
            '# Stable\n\n- 本工作树密钥存放于./local/(位置B)，禁止提交。\n'
            '- 本工作树密钥存放于./local/(位置B)，禁止提交。\n\n'
            '<!-- SHARED-START -->\n## 协作与交接\n\n- 共享规则 旧版。\n<!-- SHARED-END -->\n\nStable tail\n',
            encoding='utf-8',
        )
        (wt_gw / 'AGENTS.md').write_text('# Gateway\n\n仅用于本地假网关测试。\n', encoding='utf-8')
        for wt in (wt_next, wt_stable, wt_gw):
            git('add', 'AGENTS.md', cwd=wt)
            git('commit', '-m', 'init agents', cwd=wt)

        team = self.api('/control/team', {'action': 'create', 'name': 'GroupTeam', 'workspace': str(repo)})['team_id']
        self.api('/control/team', {
            'action': 'git_identity', 'team_id': team,
            'git_author_name': 'Aether Chen', 'git_author_email': 'aether@example.com',
        })

        # 1. Overview classifies root / shared / standalone and detects drift.
        ov = self.api('/control/doc', {'team_id': team, 'action': 'agents_overview'})
        by_rel = {w['relative']: w for w in ov['worktrees']}
        self.assertEqual(by_rel['']['group'], 'root')
        self.assertEqual(by_rel['next']['group'], 'shared')
        self.assertEqual(by_rel['stable']['group'], 'shared')
        self.assertEqual(by_rel['gateway']['group'], 'standalone')
        self.assertEqual(ov['shared_group']['canonical_worktree'], 'next')
        self.assertEqual(ov['shared_group']['drifted_count'], 1)
        self.assertIn('共享规则 旧版', by_rel['stable']['drift_diff'])
        self.assertEqual(by_rel['stable']['drift_inline']['added'], 1)
        self.assertEqual(by_rel['stable']['drift_inline']['removed'], 1)
        self.assertTrue(any(r['type'] == 'del' and any(s['type'] == 'emph' for s in r['segments'])
                            for r in by_rel['stable']['drift_inline']['rows']))

        # 2. Save shared block to next+stable, selectively committing ONLY next; refuse standalone gateway.
        self.api('/control/doc', {
            'team_id': team, 'action': 'save_shared',
            'content': '## 协作与交接\n\n- 共享规则 v2 统一版。',
            'target_worktrees': ['gateway'],
        }, 409)
        next_head_before = git('rev-parse', 'HEAD', cwd=wt_next).stdout.strip()
        stable_head_before = git('rev-parse', 'HEAD', cwd=wt_stable).stdout.strip()
        saved = self.api('/control/doc', {
            'team_id': team, 'action': 'save_shared',
            'content': '## 协作与交接\n\n- 共享规则 v2 统一版。',
            'target_worktrees': ['next', 'stable'],
            'commit_worktrees': ['next'],
        })
        self.assertIn('next', saved['commits'])
        self.assertEqual(saved['commits']['next']['status'], 'committed')
        self.assertNotIn('stable', saved['commits'])
        self.assertNotEqual(git('rev-parse', 'HEAD', cwd=wt_next).stdout.strip(), next_head_before)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt_stable).stdout.strip(), stable_head_before)
        # Headers & tails preserved in both worktrees.
        self.assertIn('Stable tail', (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn('共享规则 v2 统一版', (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertEqual(self.api('/control/doc', {'team_id': team, 'action': 'agents_overview'})['shared_group']['drifted_count'], 0)

        # 3. Local dual-track similar search finds (位置B) & duplicate line from (位置A) query.
        found = self.api('/control/doc', {
            'team_id': team, 'action': 'search_similar',
            'query': '本工作树密钥存放于./local/(位置A)，禁止提交。',
            'min_score': 0.55,
            'source_worktree': 'next',
        })
        matched_wts = [m['worktree'] for m in found['matches']]
        self.assertEqual(matched_wts.count('next'), 1)
        self.assertEqual(matched_wts.count('stable'), 2)
        self.assertNotIn('gateway', matched_wts)
        self.assertTrue(all(m['recommended'] for m in found['matches']))
        stable_match = next(m for m in found['matches'] if m['worktree'] == 'stable')
        self.assertTrue(any(seg['type'] == 'diff' for seg in stable_match['diff_segments']))

        # 4. Batch remove matched constraints and commit ONLY stable.
        rem = self.api('/control/doc', {
            'team_id': team, 'action': 'batch_remove',
            'items': [{
                'worktree': m['worktree'],
                'start_offset': m['start_offset'],
                'end_offset': m['end_offset'],
                'expected_text': m['text'],
            } for m in found['matches']],
            'commit_worktrees': ['stable'],
        })
        self.assertEqual(rem['removed_count'], 3)
        self.assertEqual(rem['commits']['stable']['status'], 'committed')
        self.assertNotIn('next', rem['commits'])
        self.assertNotIn('位置B', (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertNotIn('位置A', (wt_next / 'AGENTS.md').read_text(encoding='utf-8'))
        # Next had uncommitted removal -> commit it cleanly via git to test clean append commit next.
        git('commit', '--only', '-m', 'clean next', '--', 'AGENTS.md', cwd=wt_next)

        # 5. Batch append preview deduplication & section insertion with selective commit.
        dup_preview = self.api('/control/doc', {
            'team_id': team, 'action': 'preview_append',
            'text': '- 共享规则 v2 统一版。',
            'worktrees': ['next', 'stable'],
            'position': 'section',
            'section_heading': '协作与交接',
        })
        self.assertTrue(all(p['duplicate'] and not p['default_apply'] for p in dup_preview['previews']))

        new_clause = '- 新增全组验收前必须运行回归测试。'
        ok_preview = self.api('/control/doc', {
            'team_id': team, 'action': 'preview_append',
            'text': new_clause,
            'worktrees': ['next', 'stable'],
            'position': 'section',
            'section_heading': '协作与交接',
        })
        self.assertTrue(all((not p['duplicate']) and p['default_apply'] for p in ok_preview['previews']))
        appended = self.api('/control/doc', {
            'team_id': team, 'action': 'batch_append',
            'text': new_clause,
            'worktrees': ['next', 'stable'],
            'position': 'section',
            'section_heading': '协作与交接',
            'commit_worktrees': ['next'],
        })
        self.assertEqual(appended['commits']['next']['status'], 'committed')
        self.assertNotIn('stable', appended['commits'])
        self.assertIn(new_clause, (wt_next / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn(new_clause, (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))

        # 6. Independent-only read & save preserves shared block untouched without exposing SHARED markers.
        next_doc = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'agents', 'worktree': 'next'})
        self.assertTrue(next_doc['has_shared_markers'])
        self.assertNotIn('<!-- SHARED-START -->', next_doc['independent_content'])
        self.assertIn('# Next', next_doc['independent_content'])
        self.assertIn('Next tail', next_doc['independent_content'])
        self.api('/control/doc', {
            'team_id': team, 'action': 'save', 'doc_key': 'agents', 'worktree': 'next',
            'content': '# Next 精简独立块\n\n仅编辑独立内容。\n',
            'content_scope': 'independent',
        })
        rebuilt = (wt_next / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('# Next 精简独立块', rebuilt)
        self.assertIn('共享规则 v2 统一版', rebuilt)
        self.assertIn(new_clause, rebuilt)

        # 7. Batch replace matched clause across worktrees with selective single-file commit.
        found_rep = self.api('/control/doc', {
            'team_id': team, 'action': 'search_similar',
            'query': new_clause, 'min_score': 0.75,
        })
        self.assertEqual(len(found_rep['matches']), 2)
        rep_clause = '- 已升级为统一回归门禁检查。'
        rep_res = self.api('/control/doc', {
            'team_id': team, 'action': 'batch_replace',
            'items': [{
                'worktree': m['worktree'],
                'start_offset': m['start_offset'],
                'end_offset': m['end_offset'],
                'expected_text': m['text'],
            } for m in found_rep['matches']],
            'replacement': rep_clause,
            'commit_worktrees': ['stable'],
        })
        self.assertEqual(rep_res['replaced_count'], 2)
        self.assertEqual(rep_res['commits']['stable']['status'], 'committed')
        self.assertNotIn('next', rep_res['commits'])
        self.assertIn(rep_clause, (wt_next / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn(rep_clause, (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertNotIn(new_clause, (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))

        # 8. Clear git identity -> any write+commit must be blocked BEFORE touching disk (zero phantom external alert!).
        self.api('/control/team', {
            'action': 'git_identity', 'team_id': team,
            'git_author_name': '', 'git_author_email': '',
        })
        self.assertEqual(self.api('/control')['alerts'], [])
        before_next = (wt_next / 'AGENTS.md').read_text(encoding='utf-8')
        before_stable = (wt_stable / 'AGENTS.md').read_text(encoding='utf-8')
        self.api('/control/doc', {
            'team_id': team, 'action': 'save', 'doc_key': 'agents', 'worktree': 'next',
            'content': '# Blocked save\n', 'auto_commit': True, 'commit_worktrees': ['next'],
        }, 409)
        self.api('/control/doc', {
            'team_id': team, 'action': 'save_shared',
            'content': '## Blocked shared\n', 'target_worktrees': ['next', 'stable'], 'commit_worktrees': ['next'],
        }, 409)
        found_blk = self.api('/control/doc', {
            'team_id': team, 'action': 'search_similar', 'query': rep_clause, 'min_score': 0.75,
        })
        blk_items = [{
            'worktree': m['worktree'], 'start_offset': m['start_offset'],
            'end_offset': m['end_offset'], 'expected_text': m['text'],
        } for m in found_blk['matches']]
        self.api('/control/doc', {
            'team_id': team, 'action': 'batch_remove', 'items': blk_items, 'commit_worktrees': ['next'],
        }, 409)
        self.api('/control/doc', {
            'team_id': team, 'action': 'batch_replace', 'items': blk_items,
            'replacement': '- blocked replacement', 'commit_worktrees': ['stable'],
        }, 409)
        self.api('/control/doc', {
            'team_id': team, 'action': 'batch_append', 'text': '- blocked append',
            'worktrees': ['next'], 'position': 'end', 'commit_worktrees': ['next'],
        }, 409)
        self.assertEqual((wt_next / 'AGENTS.md').read_text(encoding='utf-8'), before_next)
        self.assertEqual((wt_stable / 'AGENTS.md').read_text(encoding='utf-8'), before_stable)
        self.assertEqual(self.api('/control')['alerts'], [])

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
            text=True, capture_output=True, env=env, check=True,
        )
        self.assertEqual(start_out.stdout.strip(), '')

        pre_out = subprocess.run(
            [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', 'global', str(port)],
            input=json.dumps({
                'cwd': str(unrelated_dir), 'session_id': thread_id,
                'hook_event_name': 'PreToolUse', 'tool_name': 'mcp__team_hub__team_send',
                'tool_input': {'to': '前端项目', 'text': '接口字段已对齐'},
            }),
            text=True, capture_output=True, env=env, check=True,
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
        self.assertEqual(list(codex_hooks['hooks'].keys()), ['PreToolUse'])

        res_rm = configure_global_access('remove', port, home_dir=fake_home)
        self.assertFalse(res_rm['global_access']['ready'])
        claude_after = json.loads((fake_home / '.claude.json').read_text(encoding='utf-8'))
        self.assertIn('existing', claude_after['mcpServers'])
        self.assertNotIn('team-hub', claude_after['mcpServers'])
        self.assertFalse((fake_home / '.codex' / 'hooks.json').exists())

    def test_agy_acp_binding_batch_poll_route_mcp_and_context(self):
        port = self.server.server_port

        # 1. Create a structured team and a peer team, each with AGY ACP members.
        ws = self.root / 'acp-structured'
        ws.mkdir()
        st_id = self.api('/control/team', {'action': 'create', 'name': 'ACP-Struct', 'workspace': str(ws)})['team_id']
        peer_id = self.api('/control/team', {'action': 'create', 'mode': 'peer', 'name': 'ACP-Peer'})['team_id']

        st_mem = self.api('/control/team', {
            'action': 'member', 'team_id': st_id, 'name': 'ACP-Worker',
            'product': 'agy_acp', 'role': 'worker', 'responsibility': '实现核心接口',
        })['member']
        p1 = self.api('/control/team', {
            'action': 'member', 'team_id': peer_id, 'name': 'ACP-前端',
            'product': 'agy_acp', 'note': 'DSH 前端',
        })['member']
        p2 = self.api('/control/team', {
            'action': 'member', 'team_id': peer_id, 'name': 'ACP-后端',
            'product': 'agy_acp', 'note': '联调后端',
        })['member']

        # 2. Bind both peer members and structured member with persistent dshSessionId.
        c_st = self.api('/control/acp', {'team_id': st_id, 'member': st_mem, 'label': 'Struct-Win'})['code']
        c_p1 = self.api('/control/acp', {'team_id': peer_id, 'member': p1, 'label': 'P1'})['code']
        c_p2 = self.api('/control/acp', {'team_id': peer_id, 'member': p2, 'label': 'P2'})['code']

        b_st = self.api('/acp/bind', {'session_id': 'dsh-sess-st', 'instance': 'inst-1', 'code': c_st})
        b_p1 = self.api('/acp/bind', {'session_id': 'dsh-sess-p1', 'instance': 'inst-1', 'code': c_p1})
        b_p2 = self.api('/acp/bind', {'session_id': 'dsh-sess-p2', 'instance': 'inst-1', 'code': c_p2})

        self.assertEqual(b_st['mode'], 'structured')
        self.assertEqual(b_p1['mode'], 'peer')
        self.assertEqual(b_p1['mcp']['type'], 'stdio')
        self.assertEqual(b_p1['mcp']['env']['TEAM_HUB_ROUTE'], b_p1['route'])

        # Same dshSessionId cannot bind another member.
        c_p2_dup = self.api('/control/acp', {'team_id': peer_id, 'member': p2})['code']
        self.api('/acp/bind', {'session_id': 'dsh-sess-p1', 'instance': 'inst-2', 'code': c_p2_dup}, 409)

        # 3. Restore across restart keeps route & mcp without bumping team revision.
        rev_before = {t['id']: t['revision'] for t in self.api('/control')['teams']}[peer_id]
        restored = self.api('/acp/restore', {'session_id': 'dsh-sess-p1', 'instance': 'inst-restarted'})
        self.assertTrue(restored['bound'])
        self.assertEqual(restored['route'], b_p1['route'])
        self.assertEqual(restored['mcp']['env']['TEAM_HUB_ROUTE'], b_p1['route'])
        rev_after = {t['id']: t['revision'] for t in self.api('/control')['teams']}[peer_id]
        self.assertEqual(rev_before, rev_after)
        self.assertFalse(self.api('/acp/restore', {'session_id': 'unknown-sess', 'instance': 'inst-x'})['bound'])

        # 4. Launch real stdio route-mcp process using dynamic mcp descriptor and call whoami/members/send!
        mcp_desc = b_p1['mcp']
        mcp_env = os.environ | {'TEAM_HUB_TOKEN': TOKEN} | mcp_desc['env']
        frames = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'team_whoami', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'team_members', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {
                'name': 'team_send', 'arguments': {'to': 'ACP-后端', 'text': '你好，来自 route-mcp！'},
            }},
        ]
        proc = subprocess.run(
            [mcp_desc['command'], *mcp_desc['args']],
            input='\n'.join(json.dumps(f) for f in frames) + '\n',
            text=True, capture_output=True, env=mcp_env, check=True, encoding='utf-8',
        )
        replies = {r['id']: r['result'] for r in (json.loads(line) for line in proc.stdout.splitlines() if line.strip())}
        self.assertEqual({t['name'] for t in replies[2]['tools']}, {'team_send', 'team_members', 'team_whoami'})
        who = json.loads(replies[3]['content'][0]['text'])
        self.assertEqual(who['name'], 'ACP-前端')
        self.assertEqual(who['mode'], 'peer')
        self.assertNotIn('role', who)
        ros = json.loads(replies[4]['content'][0]['text'])
        self.assertEqual({m['name'] for m in ros['members']}, {'ACP-前端', 'ACP-后端'})
        self.assertFalse(replies[5]['isError'])

        # 5. Batch poll delivers queued message to p2, returns bound=False for unknown route, and handles received receipt.
        batch = self.api('/acp/poll', {'routes': [b_p1['route'], b_p2['route'], 'bogus-route']})
        self.assertTrue(batch['results'][b_p1['route']]['bound'])
        self.assertIsNone(batch['results'][b_p1['route']]['delivery'])
        self.assertFalse(batch['results']['bogus-route']['bound'])
        deliv = batch['results'][b_p2['route']]['delivery']
        self.assertIsNotNone(deliv)
        self.assertIn('【来自 ACP-前端】', deliv['prompt'])
        self.assertIn('当前团队组织有变化', deliv['prompt'])
        self.assertTrue(self.api('/acp/received', {
            'route': b_p2['route'], 'id': deliv['id'], 'revision': deliv['revision'],
        })['accepted'])
        self.assertIsNone(self.api('/acp/poll', {'route': b_p2['route']})['delivery'])

        # 6. /acp/context: peer returns {}, structured returns full once, {} when unchanged, refresh on role update.
        self.assertEqual(self.api('/acp/context', {'route': b_p1['route'], 'event': 'startup'}), {})
        ctx1 = self.api('/acp/context', {'route': b_st['route'], 'event': 'prompt', 'paths': [str(ws)]})
        self.assertEqual(ctx1['mode'], 'full')
        self.assertIn('ACP-Worker', ctx1['text'])
        self.assertEqual(self.api('/acp/context', {'route': b_st['route'], 'event': 'prompt', 'paths': [str(ws)]}), {})
        self.api('/control/team', {
            'action': 'member', 'team_id': st_id, 'member': st_mem,
            'name': 'ACP-Worker', 'product': 'agy_acp', 'role': 'reviewer', 'responsibility': '负责代码复核',
        })
        ctx2 = self.api('/acp/context', {'route': b_st['route'], 'event': 'prompt', 'paths': [str(ws)]})
        self.assertEqual(ctx2['mode'], 'refresh')
        self.assertIn('reviewer', ctx2['text'])

        # 7. Unbind cleans session & route.
        self.assertTrue(self.api('/acp/unbind', {'route': b_p1['route']})['unbound'])
        self.assertFalse(self.api('/acp/restore', {'session_id': 'dsh-sess-p1', 'instance': 'inst-z'})['bound'])
        self.api('/acp/roster', {'route': b_p1['route']}, 409)

    def test_agy_cli_shared_plugin_wait_inbox_and_hook_fallback(self):
        port = self.server.server_port
        repo_root = Path(__file__).resolve().parents[1]
        ws = self.root / 'cli-structured'
        ws.mkdir()
        team_id = self.api('/control/team', {'action': 'create', 'name': 'CLI-Team', 'workspace': str(ws)})['team_id']
        self.api('/control/project', {'team_id': team_id, 'action': 'install'})

        cli_mem = self.api('/control/team', {
            'action': 'member', 'team_id': team_id, 'name': 'CLI-终端',
            'product': 'agy_cli', 'role': 'worker', 'responsibility': '终端执行任务',
        })['member']
        app_mem = self.api('/control/team', {
            'action': 'member', 'team_id': team_id, 'name': 'App-主控',
            'product': 'antigravity', 'role': 'captain', 'responsibility': '统筹',
        })['member']

        # Bind App member
        app_code = self.api('/control/antigravity', {'team_id': team_id, 'member': app_mem})['code']
        app_inst = str(uuid.uuid4())
        app_route = self.api('/antigravity/bind', {'instance': app_inst, 'code': app_code})['route']

        # Bind CLI member via shared team_relay.handle('team_cli_bind') with conversation_id metadata
        cli_code = self.api('/control/agy-cli', {'team_id': team_id, 'member': cli_mem, 'label': '终端A'})['code']
        cli_conv = str(uuid.uuid4())

        def hub_http(method, path, payload=None, timeout=4):
            req = Request(
                f'http://127.0.0.1:{port}{path}',
                data=json.dumps(payload).encode('utf-8') if payload is not None else None,
                headers={'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'},
                method=method,
            )
            try:
                with urlopen(req, timeout=timeout) as resp:
                    return resp.status, json.load(resp)
            except HTTPError as err:
                return err.code, json.loads(err.read().decode('utf-8'))

        captured = {}
        def respond(req_id, text, data, is_error=False):
            captured[req_id] = (text, data, is_error)

        handle_antigravity_tool(
            'team_cli_bind', {'code': cli_code}, 1, hub_http, respond,
            meta={'antigravity.google/conversation_id': cli_conv},
        )
        _, bind_data, bind_err = captured[1]
        self.assertFalse(bind_err)
        self.assertTrue(bind_data['session_id_bound'])
        cli_route = bind_data['route']
        self.assertIn('wait-inbox', bind_data['wait_command'])

        # Shared team_whoami / team_members / team_send work seamlessly with cli_route
        handle_antigravity_tool('team_whoami', {'route': cli_route}, 2, hub_http, respond)
        self.assertFalse(captured[2][2])
        self.assertEqual(captured[2][1]['name'], 'CLI-终端')
        self.assertEqual(captured[2][1]['product'], 'agy_cli')

        handle_antigravity_tool('team_send', {'route': cli_route, 'to': 'App-主控', 'text': 'CLI 已上线'}, 3, hub_http, respond)
        self.assertFalse(captured[3][2])
        app_poll = self.api('/antigravity/poll', {'instance': app_inst})
        self.assertIn('CLI 已上线', app_poll['delivery']['prompt'])

        # Restore across restart does not bump team revision
        rev_before = {t['id']: t['revision'] for t in self.api('/control')['teams']}[team_id]
        handle_antigravity_tool(
            'team_cli_restore', {}, 4, hub_http, respond,
            meta={'antigravity.google/conversation_id': cli_conv},
        )
        self.assertTrue(captured[4][1]['bound'])
        self.assertEqual(captured[4][1]['route'], cli_route)
        rev_after = {t['id']: t['revision'] for t in self.api('/control')['teams']}[team_id]
        self.assertEqual(rev_before, rev_after)

        # Plan A: wait-inbox blocks until message arrives, prints prompt, acks received, and exits 0
        env = os.environ | {'TEAM_HUB_TOKEN': TOKEN}
        waiter = subprocess.Popen(
            [sys.executable, str(repo_root / 'project_agent.py'), 'wait-inbox', str(port), cli_route, '15'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env,
        )
        try:
            self.api('/antigravity/send', {'route': app_route, 'to': 'CLI-终端', 'text': 'CLI-WAKE-01', 'request_id': str(uuid.uuid4())}, 202)
            out, err = waiter.communicate(timeout=10)
            self.assertEqual(waiter.returncode, 0, err)
            self.assertIn('【来自 App-主控】', out)
            self.assertIn('CLI-WAKE-01', out)
        finally:
            if waiter.poll() is None:
                waiter.kill()
        self.assertIsNone(self.api('/agy-cli/poll', {'route': cli_route})['delivery'])

        # Plan C fallback: Hook PreInvocation delivers pending message & role context even without wait-inbox
        self.api('/antigravity/send', {'route': app_route, 'to': 'CLI-终端', 'text': 'CLI-HOOK-FALLBACK-02', 'request_id': str(uuid.uuid4())}, 202)
        hook_proc = subprocess.run(
            [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'antigravity', str(team_id), str(port)],
            input=json.dumps({
                'conversationId': cli_conv, 'workspacePaths': [str(ws)], 'hookEventName': 'PreInvocation',
            }),
            text=True, capture_output=True, encoding='utf-8', env=env, check=True,
        )
        hook_res = json.loads(hook_proc.stdout)
        injected = hook_res['injectSteps'][0]['systemMessage']['systemMessage']
        self.assertIn('CLI-终端', injected)
        self.assertIn('CLI-HOOK-FALLBACK-02', injected)
        self.assertIsNone(self.api('/agy-cli/poll', {'route': cli_route})['delivery'])


if __name__ == '__main__':
    unittest.main()
