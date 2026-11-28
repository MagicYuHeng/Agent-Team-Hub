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

class ClientsAndHooksTest(HubTestCase):
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

    def test_codex_mcp_authenticates_with_filtered_environment(self):
        args = {'team_id': 0, 'product': 'codex', 'paths': []}
        if os.name == 'nt':
            import winreg

            with (patch.dict(os.environ, clear=True),
                  patch.object(winreg, 'QueryValueEx', return_value=(TOKEN, winreg.REG_SZ))):
                self.assertEqual(agent_request(self.server.server_port, 'context', args), {})
        else:
            with patch.dict(os.environ, clear=True):
                res = agent_request(self.server.server_port, 'context', args)
                self.assertIn('error', res)
            with patch.dict(os.environ, {'TEAM_HUB_TOKEN': TOKEN}, clear=True):
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
        self.assertNotIn('gpt-6.2-sol', role['hookSpecificOutput']['additionalContext'])
        claude_ctx = hook('claude', session_id=claude_session, hook_event_name='SessionStart',
                          source='resume')['hookSpecificOutput']['additionalContext']
        self.assertIn('worker', claude_ctx)
        self.assertNotIn('deepseek-v4-flash', claude_ctx)
        self.assertNotIn('Claude Code 框架下的其他模型通道', claude_ctx)
        self.assertIn('项目内不限（注意避免多线写入冲突）', claude_ctx)
        self.assertIn('小队协作约定', claude_ctx)
        # Non-Claude hooks should also not include model notices.
        self.assertNotIn('Claude Code 框架下的其他模型通道', role['hookSpecificOutput']['additionalContext'])
        # Unbound session receives empty context.
        self.assertEqual(hook('claude', session_id=str(uuid.uuid4()),
                              hook_event_name='SessionStart', source='startup'), {})
        antigravity = hook('antigravity', workspacePaths=[str(project)],
                           conversationId=ag_session, hookEventName='SessionStart')
        self.assertIn('captain', antigravity['injectSteps'][0]['systemMessage']['systemMessage'])
        self.assertNotIn('Gemini-4-Hydrogen', antigravity['injectSteps'][0]['systemMessage']['systemMessage'])
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

    def test_claude_desktop_token_windows_fallback(self):
        if os.name == 'nt':
            import winreg

            with (patch.dict(os.environ, clear=True),
                  patch.object(winreg, 'QueryValueEx', return_value=(TOKEN, winreg.REG_SZ))):
                self.assertEqual(desktop_hub_token(), TOKEN)
        else:
            with patch.dict(os.environ, clear=True):
                self.assertEqual(desktop_hub_token(), '')
            with patch.dict(os.environ, {'TEAM_HUB_TOKEN': TOKEN}, clear=True):
                self.assertEqual(desktop_hub_token(), TOKEN)

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

        # Plan A: wait-inbox defaults to persistent silent wait (no 5-minute idle exit), wakes on real message,
        # tolerates brief Hub restarts, and exits cleanly when Hub remains down past disconnect_grace.
        env = os.environ | {'TEAM_HUB_TOKEN': TOKEN}
        waiter = subprocess.Popen(
            [sys.executable, str(repo_root / 'project_agent.py'), 'wait-inbox', str(port), cli_route],
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
                waiter.wait()

        # Verify transient Hub disconnect recovers silently without waking agent, and sustained outage exits 1.
        import project_agent
        transient_seq = iter([
            {'error': project_agent.TRANSIENT_HUB_ERROR},
            {'error': project_agent.TRANSIENT_HUB_ERROR},
            {'bound': True, 'delivery': None},
            {'bound': True, 'delivery': {'id': 'm-transient', 'revision': 1, 'prompt': '【来自 App-主控】\nRECOVERED-OK'}},
            {'accepted': True},
        ])
        with patch.object(project_agent, 'request_path', side_effect=lambda *a, **kw: next(transient_seq)):
            self.assertEqual(
                project_agent.wait_inbox(port, cli_route, timeout=0, disconnect_grace=5.0, poll_interval=0.05),
                0,
            )
        with patch.object(project_agent, 'request_path', return_value={'error': project_agent.TRANSIENT_HUB_ERROR}):
            self.assertEqual(
                project_agent.wait_inbox(port, cli_route, timeout=0, disconnect_grace=0.08, poll_interval=0.05),
                1,
            )
        # Verify explicit positive timeout still exits 0 when idle window elapses.
        with patch.object(project_agent, 'request_path', return_value={'bound': True, 'delivery': None}):
            self.assertEqual(
                project_agent.wait_inbox(port, cli_route, timeout=1, disconnect_grace=5.0, poll_interval=0.05),
                0,
            )
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

    def test_codex_bind_and_reservation_interleaving_under_members_lock(self):
        from unittest import mock
        import codex_native

        team_id = self.api('/control/team', {'action': 'create', 'name': 'Lock-Interleave', 'mode': 'peer'})['team_id']
        created = self.api('/control/team', {
            'action': 'member', 'team_id': team_id, 'name': 'Codex-A', 'product': 'codex',
        })
        member = created['member']

        # Create two fake Codex session rollout files under isolated CODEX_HOME.
        codex_home = self.root / 'codex-home-lock'
        sess_dir = codex_home / 'sessions' / '2026' / '11' / '27'
        sess_dir.mkdir(parents=True)
        thread_1 = str(uuid.uuid4())
        thread_2 = str(uuid.uuid4())
        for tid in (thread_1, thread_2):
            p = sess_dir / f'rollout-2026-11-27T00-00-00-{tid}.jsonl'
            p.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': tid}}) + '\n', encoding='utf-8')

        old_codex_home = os.environ.get('CODEX_HOME')
        os.environ['CODEX_HOME'] = str(codex_home)
        try:
            self.api('/control/codex-bind', {'member': member, 'thread_id': thread_1})
            self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-A', 'text': 'LOCK-CHECK-01'}, 202)

            entered_queue = threading.Event()
            release_queue = threading.Event()

            def controlled_queue_prompt(tid, prompt):
                entered_queue.set()
                release_queue.wait(timeout=5)
                return 0

            runner = codex_native.CodexRunner(self.store, self.server.members_lock)
            binding = self.store.codex_bindings()[0]
            with mock.patch.object(codex_native, 'queue_prompt', side_effect=controlled_queue_prompt):
                worker = threading.Thread(target=runner.deliver_peer, args=(binding,))
                worker.start()
                self.assertTrue(entered_queue.wait(timeout=3))
                # While deliver_peer has reserved the message as 'submitting' and released members_lock,
                # /control/codex-bind must still acquire members_lock without deadlocking and reject rebind (409).
                err = self.api('/control/codex-bind', {'member': member, 'thread_id': thread_2}, 409)
                self.assertIn('正在接收消息', err['error'])
                release_queue.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())

            # Once submitting completes to accepted, rebinding to thread_2 succeeds cleanly.
            ok = self.api('/control/codex-bind', {'member': member, 'thread_id': thread_2})
            self.assertEqual(ok['thread_id'], thread_2)
        finally:
            runner.executor.shutdown(wait=True)
            if old_codex_home is None:
                os.environ.pop('CODEX_HOME', None)
            else:
                os.environ['CODEX_HOME'] = old_codex_home

    def test_codex_hook_post_tool_use_and_stop_steer_mutex_and_offline_timeout(self):
        import time as _time
        from unittest import mock
        import codex_native

        team_id, ws = self.team('Codex-Steer-Team')
        member = self.member(team_id, 'Codex-Worker', 'codex', '负责同轮引导测试')
        self.api('/control/project', {'team_id': team_id, 'action': 'install'})

        codex_hooks = json.loads((ws / '.codex' / 'hooks.json').read_text(encoding='utf-8'))
        self.assertIn('PostToolUse', codex_hooks['hooks'])
        self.assertIn('Stop', codex_hooks['hooks'])
        self.assertEqual(codex_hooks['hooks']['PostToolUse'][0]['hooks'][0]['timeout'], 2)
        self.assertEqual(codex_hooks['hooks']['Stop'][0]['hooks'][0]['timeout'], 2)

        codex_home = self.root / 'codex-home-steer'
        sess_dir = codex_home / 'sessions' / '2026' / '11' / '27'
        sess_dir.mkdir(parents=True)
        thread_id = str(uuid.uuid4())
        rollout = sess_dir / f'rollout-2026-11-27T00-00-00-{thread_id}.jsonl'
        rollout.write_text(
            json.dumps({'type': 'session_meta', 'payload': {'id': thread_id}}) + '\n'
            + json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't1'}}) + '\n',
            encoding='utf-8',
        )

        repo_root = Path(__file__).resolve().parents[1]
        port = self.server.server_port
        env = os.environ | {'TEAM_HUB_TOKEN': TOKEN}
        old_codex_home = os.environ.get('CODEX_HOME')
        os.environ['CODEX_HOME'] = str(codex_home)
        runner = codex_native.CodexRunner(self.store, self.server.members_lock, server=self.server)
        self.addCleanup(runner.executor.shutdown, wait=True)
        try:
            self.api('/control/codex-bind', {'member': member, 'thread_id': thread_id})

            # 1. UserPromptSubmit injects natural Codex hook receipt hint and marks session active.
            ctx_proc = subprocess.run(
                [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', str(team_id), str(port)],
                input=json.dumps({'cwd': str(ws), 'session_id': thread_id, 'hook_event_name': 'UserPromptSubmit'}),
                text=True, capture_output=True, encoding='utf-8', env=env, check=True,
            )
            ctx_out = json.loads(ctx_proc.stdout)
            self.assertIn('协作收件说明', ctx_out['hookSpecificOutput']['additionalContext'])

            # 2. Enqueue a message while Codex turn is busy + Hook observed -> CodexRunner defers to Hook.
            msg_err = self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-Worker', 'text': 'BROKEN-PIPE-MSG'}, 202)
            queue_calls = []
            binding = self.store.codex_bindings()[0]
            with mock.patch.object(codex_native, 'queue_prompt', side_effect=lambda t, p: (queue_calls.append(p), 0)[1]):
                runner.deliver_peer(binding)
            self.assertEqual(queue_calls, [])

            # 2b. Simulated HTTP write failure during /project/inbox settles as 'unconfirmed'
            #     WITHOUT advancing notice_revision or roster.seen_revision (preserving org-change reminder).
            from server.routes_clients import handle_project_request
            with self.store.connect() as db:
                initial_seen = db.execute('SELECT seen_revision FROM roster WHERE team_id=? AND member=?',
                                          (team_id, member)).fetchone()[0]
                team_rev = db.execute('SELECT revision FROM teams WHERE id=?', (team_id,)).fetchone()[0]
            self.assertLess(initial_seen, team_rev)
            fake_handler = mock.Mock()
            fake_handler.server = self.server
            fake_handler.path = '/project/inbox'
            fake_handler.read_json.return_value = {
                'session_id': thread_id, 'event': 'PostToolUse', 'team_id': team_id, 'paths': [str(ws)],
            }
            fake_handler.respond.side_effect = BrokenPipeError('client timed out')
            with self.assertRaises(BrokenPipeError):
                handle_project_request(fake_handler)
            with self.store.connect() as db:
                err_row = db.execute('SELECT delivered, notice_revision FROM team_messages WHERE id=?', (msg_err['id'],)).fetchone()
                seen_after_err = db.execute('SELECT seen_revision FROM roster WHERE team_id=? AND member=?',
                                            (team_id, member)).fetchone()[0]
            self.assertEqual(err_row['delivered'], 'unconfirmed')
            self.assertIsNone(err_row['notice_revision'])
            self.assertEqual(seen_after_err, initial_seen)

            # 3. Next PostToolUse handoff succeeds, STILL carries the unconsumed org-change reminder,
            #    and truthfully records 'accepted' (no text-based auto-upgrade to 'delivered').
            msg1 = self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-Worker', 'text': 'STEER-MSG-01'}, 202)
            post_proc = subprocess.run(
                [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', str(team_id), str(port)],
                input=json.dumps({'cwd': str(ws), 'session_id': thread_id, 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash'}),
                text=True, capture_output=True, encoding='utf-8', env=env, check=True,
            )
            post_out = json.loads(post_proc.stdout)
            self.assertEqual(post_out['hookSpecificOutput']['hookEventName'], 'PostToolUse')
            self.assertIn('【来自 管理台（操作者）】', post_out['hookSpecificOutput']['additionalContext'])
            self.assertIn('当前团队组织有变化', post_out['hookSpecificOutput']['additionalContext'])
            self.assertIn('STEER-MSG-01', post_out['hookSpecificOutput']['additionalContext'])

            # Wait briefly for handler post-write finalize commit to settle from 'submitting' -> 'accepted'.
            st = None
            for _ in range(40):
                with self.store.connect() as db:
                    row = db.execute('SELECT delivered, notice_revision FROM team_messages WHERE id=?', (msg1['id'],)).fetchone()
                    seen_after_ok = db.execute('SELECT seen_revision FROM roster WHERE team_id=? AND member=?',
                                               (team_id, member)).fetchone()[0]
                st = row['delivered']
                if st != 'submitting':
                    break
                _time.sleep(0.025)
            self.assertEqual(st, 'accepted')
            self.assertEqual(row['notice_revision'], team_rev)
            self.assertEqual(seen_after_ok, team_rev)
            with mock.patch.object(codex_native, 'queue_prompt', side_effect=lambda t, p: (queue_calls.append(p), 0)[1]):
                runner.deliver_peer(binding)
            self.assertEqual(queue_calls, [])

            # Text appearing in rollout (e.g. quoted in <example>) must NOT heuristically upgrade accepted -> delivered.
            with rollout.open('a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'type': 'response_item',
                    'payload': {
                        'type': 'message',
                        'role': 'user',
                        'content': [{'type': 'input_text', 'text': f'<example>\n{post_out["hookSpecificOutput"]["additionalContext"]}\n</example>'}],
                    },
                }) + '\n')
            runner.tick()
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT delivered FROM team_messages WHERE id=?', (msg1['id'],)).fetchone()[0], 'accepted')

            # 4. Stop fallback blocks once to deliver unread message, then allows when stop_hook_active=True.
            msg2 = self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-Worker', 'text': 'STOP-MSG-02'}, 202)
            stop1 = subprocess.run(
                [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', str(team_id), str(port)],
                input=json.dumps({'cwd': str(ws), 'session_id': thread_id, 'hook_event_name': 'Stop', 'stop_hook_active': False}),
                text=True, capture_output=True, encoding='utf-8', env=env, check=True,
            )
            stop1_out = json.loads(stop1.stdout)
            self.assertEqual(stop1_out['decision'], 'block')
            self.assertIn('STOP-MSG-02', stop1_out['reason'])
            st2 = None
            for _ in range(40):
                with self.store.connect() as db:
                    st2 = db.execute('SELECT delivered FROM team_messages WHERE id=?', (msg2['id'],)).fetchone()[0]
                if st2 != 'submitting':
                    break
                _time.sleep(0.025)
            self.assertEqual(st2, 'accepted')

            stop2 = subprocess.run(
                [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', str(team_id), str(port)],
                input=json.dumps({'cwd': str(ws), 'session_id': thread_id, 'hook_event_name': 'Stop', 'stop_hook_active': True}),
                text=True, capture_output=True, encoding='utf-8', env=env, check=True,
            )
            self.assertEqual(stop2.stdout.strip(), '')

            # 5. Once rollout marks task_complete (idle), CodexRunner immediately delivers next message via codex queue.
            with rollout.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}}) + '\n')
            self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-Worker', 'text': 'IDLE-QUEUE-03'}, 202)
            with mock.patch.object(codex_native, 'queue_prompt', side_effect=lambda t, p: (queue_calls.append(p), 0)[1]):
                runner.deliver_peer(binding)
            self.assertEqual(len(queue_calls), 1)
            self.assertIn('IDLE-QUEUE-03', queue_calls[0])

            # 6. Offline Hub port fails fast without erroring or stalling Codex.
            t0 = _time.monotonic()
            offline = subprocess.run(
                [sys.executable, str(repo_root / 'project_agent.py'), 'hook', 'codex', 'global', '1'],
                input=json.dumps({'cwd': str(ws), 'session_id': thread_id, 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash'}),
                text=True, capture_output=True, encoding='utf-8', env=env, check=True,
            )
            self.assertEqual(offline.stdout.strip(), '')
            self.assertLess(_time.monotonic() - t0, 2.5)
        finally:
            if old_codex_home is None:
                os.environ.pop('CODEX_HOME', None)
            else:
                os.environ['CODEX_HOME'] = old_codex_home

    def test_codex_cancel_submitting_uses_cancel_requested_without_false_cancel(self):
        from unittest import mock
        import codex_native

        team_id = self.api('/control/team', {'action': 'create', 'name': 'Cancel-Safe', 'mode': 'peer'})['team_id']
        member = self.api('/control/team', {'action': 'member', 'team_id': team_id, 'name': 'Codex-C', 'product': 'codex'})['member']

        codex_home = self.root / 'codex-home-cancel'
        sess_dir = codex_home / 'sessions' / '2026' / '11' / '27'
        sess_dir.mkdir(parents=True)
        tid = str(uuid.uuid4())
        (sess_dir / f'rollout-2026-11-27T00-00-00-{tid}.jsonl').write_text(
            json.dumps({'type': 'session_meta', 'payload': {'id': tid}}) + '\n', encoding='utf-8',
        )

        old_codex_home = os.environ.get('CODEX_HOME')
        os.environ['CODEX_HOME'] = str(codex_home)
        runner = codex_native.CodexRunner(self.store, self.server.members_lock)
        self.addCleanup(runner.executor.shutdown, wait=True)
        try:
            self.api('/control/codex-bind', {'member': member, 'thread_id': tid})
            binding = self.store.codex_bindings()[0]

            # Case A: Cancel during submitting, but external CLI succeeds -> truthfully settles as 'accepted'.
            m_ok = self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-C', 'text': 'CANCEL-RACE-OK'}, 202)['id']
            entered, release = threading.Event(), threading.Event()
            with mock.patch.object(codex_native, 'queue_prompt', side_effect=lambda *_: (entered.set(), release.wait(5), 0)[2]):
                th = threading.Thread(target=runner.deliver_peer, args=(binding,))
                th.start()
                self.assertTrue(entered.wait(3))
                res = self.api('/control/message/cancel', {'team_id': team_id, 'id': m_ok})
                self.assertFalse(res['canceled'])
                self.assertTrue(res['cancel_requested'])
                self.assertEqual(res['status'], 'cancel_requested')
                release.set()
                th.join(5)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT delivered FROM team_messages WHERE id=?', (m_ok,)).fetchone()[0], 'accepted')

            # Case B: Cancel during submitting, external CLI fails -> settles as 'canceled' and does NOT retry.
            m_fail = self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-C', 'text': 'CANCEL-RACE-FAIL'}, 202)['id']
            entered2, release2 = threading.Event(), threading.Event()
            with mock.patch.object(codex_native, 'queue_prompt', side_effect=lambda *_: (entered2.set(), release2.wait(5), 1)[2]):
                th2 = threading.Thread(target=runner.deliver_peer, args=(binding,))
                th2.start()
                self.assertTrue(entered2.wait(3))
                res2 = self.api('/control/message/cancel', {'team_id': team_id, 'id': m_fail})
                self.assertEqual(res2['status'], 'cancel_requested')
                release2.set()
                th2.join(5)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT delivered FROM team_messages WHERE id=?', (m_fail,)).fetchone()[0], 'canceled')
        finally:
            if old_codex_home is None:
                os.environ.pop('CODEX_HOME', None)
            else:
                os.environ['CODEX_HOME'] = old_codex_home

    def test_codex_runner_parallel_per_member_delivery_and_rollout_upgrade(self):
        from unittest import mock
        import codex_native

        team_id = self.api('/control/team', {'action': 'create', 'name': 'Parallel-Codex', 'mode': 'peer'})['team_id']
        m_a = self.api('/control/team', {'action': 'member', 'team_id': team_id, 'name': 'Codex-1', 'product': 'codex'})['member']
        m_b = self.api('/control/team', {'action': 'member', 'team_id': team_id, 'name': 'Codex-2', 'product': 'codex'})['member']

        codex_home = self.root / 'codex-home-par'
        sess_dir = codex_home / 'sessions' / '2026' / '11' / '27'
        sess_dir.mkdir(parents=True)
        tid_a, tid_b = str(uuid.uuid4()), str(uuid.uuid4())
        rollout_a = sess_dir / f'rollout-2026-11-27T00-00-00-{tid_a}.jsonl'
        rollout_b = sess_dir / f'rollout-2026-11-27T00-00-00-{tid_b}.jsonl'
        for tid, path in ((tid_a, rollout_a), (tid_b, rollout_b)):
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': tid}}) + '\n', encoding='utf-8')

        old_codex_home = os.environ.get('CODEX_HOME')
        os.environ['CODEX_HOME'] = str(codex_home)
        runner = codex_native.CodexRunner(self.store, self.server.members_lock, server=self.server)
        self.addCleanup(runner.executor.shutdown, wait=True)
        try:
            self.api('/control/codex-bind', {'member': m_a, 'thread_id': tid_a})
            self.api('/control/codex-bind', {'member': m_b, 'thread_id': tid_b})
            self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-1', 'text': 'SLOW-A'}, 202)
            msg_b = self.api('/control/message/send', {'team_id': team_id, 'to': 'Codex-2', 'text': 'FAST-B-UNIQUE-99'}, 202)['id']

            a_started = threading.Event()
            a_release = threading.Event()
            b_done = threading.Event()
            captured_b = []

            def mock_queue(tid, prompt):
                if tid == tid_a:
                    a_started.set()
                    a_release.wait(timeout=5)
                    return 0
                if tid == tid_b:
                    captured_b.append(prompt)
                    b_done.set()
                    return 0
                return 0

            with mock.patch.object(codex_native, 'queue_prompt', side_effect=mock_queue):
                runner.tick()
                self.assertTrue(a_started.wait(timeout=3))
                # Member B completes without waiting for blocked Member A.
                self.assertTrue(b_done.wait(timeout=3))
                a_release.set()
                # Wait until both member slots finish cleanly.
                deadline = threading.Event()
                for _ in range(50):
                    with runner._state_lock:
                        if not runner._in_flight:
                            break
                    deadline.wait(0.05)

            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT delivered FROM team_messages WHERE id=?', (msg_b,)).fetchone()[0], 'accepted')

            # Without a stable host-supplied correlation ID, rollout text alone never upgrades 'accepted' -> 'delivered'.
            with rollout_b.open('a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'type': 'response_item',
                    'payload': {
                        'type': 'message',
                        'role': 'user',
                        'content': [{'type': 'input_text', 'text': captured_b[0]}],
                    },
                }) + '\n')
            runner.tick()
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT delivered FROM team_messages WHERE id=?', (msg_b,)).fetchone()[0], 'accepted')
        finally:
            if old_codex_home is None:
                os.environ.pop('CODEX_HOME', None)
            else:
                os.environ['CODEX_HOME'] = old_codex_home

if __name__ == '__main__':
    unittest.main()
