"""Client runtime HTTP route handlers (/antigravity/*, /acp/*, /agy-cli/*, /desktop/*, /project/*)."""

import secrets
import time
import uuid

from server.global_access import acp_mcp_descriptor, agy_cli_wait_command
from server.http_utils import required_text
from teams import Conflict, display_worktree, team_mode


def handle_peer_request(handler, action, team, member, body):
    store = handler.server.store
    team_id = team['id']
    if action in ('send', 'dispatch'):
        name = required_text(body, 'to', 48)
        instruction = required_text(body, 'instruction' if action == 'dispatch' else 'text', 12000)
        request_id = body.get('request_id')
        if request_id is not None:
            request_id = required_text(body, 'request_id', 128)
        response = store.messages.send(team_id, member, name, instruction, request_id, mode=body.get('mode', 'auto'))
        handler.respond(202, response)
    elif action == 'roster':
        handler.server.expire_members()
        is_peer = team_mode(team) == 'peer'
        with store.connect() as db:
            roster = store.teams.roster(db, team, handler.server.members)
            if is_peer:
                rows = [
                    {'name': slot['name'], 'product': slot['product'],
                     'note': slot.get('note', ''), 'online': slot['online']}
                    for slot in roster
                ]
            else:
                fields = ('name', 'product', 'role', 'role_revision', 'worktree', 'responsibility', 'online')
                rows = [{key: (display_worktree(slot[key]) if key == 'worktree' else slot[key]) for key in fields}
                        for slot in roster]
        handler.respond(200, {'team': team['name'], 'mode': 'peer' if is_peer else 'structured', 'members': rows})
    elif action == 'whoami':
        with store.connect() as db:
            identity = store.teams.member_identity(db, team, member)
        if not identity:
            raise Conflict('当前会话对应的成员已移出 Team。')
        identity.pop('worktree_raw', None)
        handler.respond(200, identity)
    elif action == 'received':
        ok = store.messages.received(team_id, member, required_text(body, 'id', 128), body.get('revision'))
        handler.respond(200 if ok else 409, {'accepted': ok})
    else:
        handler.respond(404, {'error': 'Unknown member operation.'})


def handle_antigravity_request(handler):
    action = handler.path.split('/')[-1]
    body = handler.read_json()
    store = handler.server.store
    with handler.server.members_lock:
        if action == 'open':
            conversation_id = str(uuid.UUID(required_text(body, 'conversation_id', 36)))
            proof = secrets.token_urlsafe(24)
            handler.server.antigravity_proofs[proof] = (conversation_id, time.monotonic() + 300)
            handler.respond(200, {'proof': proof})
            return
        if action in ('bind', 'poll', 'restore'):
            instance = str(uuid.UUID(required_text(body, 'instance', 36)))
            if action in ('bind', 'restore'):
                key = body.get('proof')
                proof = handler.server.antigravity_proofs.get(key) if isinstance(key, str) else None
                conversation_id = proof[0] if proof and proof[1] > time.monotonic() else None
                if action == 'restore':
                    result = (store.teams.restore_antigravity(instance, conversation_id) if conversation_id else
                              {'bound': False})
                    if result['bound']:
                        handler.server.antigravity_proofs.pop(key, None)
                    handler.respond(200, result)
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
                    handler.server.antigravity_proofs.pop(key, None)
                handler.respond(200, result)
                return
            team = store.teams.antigravity_window(instance=instance)
            if not team:
                handler.respond(200, {'bound': False})
                return
            is_peer = team_mode(team) == 'peer'
            handler.respond(200, {'bound': True, 'team': team['name'], 'mode': 'peer' if is_peer else 'structured',
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
            handler.respond(200, {'acknowledged': True})
        else:
            handle_peer_request(handler, action, team, team['member'], body)


def _acp_poll_payload(handler, route=None, instance=None):
    store = handler.server.store
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


def handle_acp_request(handler):
    action = handler.path.split('/')[-1]
    body = handler.read_json()
    store = handler.server.store
    port = handler.server.server_port
    with handler.server.members_lock:
        if action == 'bind':
            session_id = required_text(body, 'session_id', 128)
            instance = required_text(body, 'instance', 64)
            code = required_text(body, 'code', 10).strip().lower()
            label = body.get('label')
            result = store.teams.bind_acp(session_id, instance, code, label)
            result['mcp'] = acp_mcp_descriptor(port, result['route'])
            handler.respond(200, result)
            return
        if action == 'restore':
            session_id = required_text(body, 'session_id', 128)
            instance = required_text(body, 'instance', 64)
            result = store.teams.restore_acp(session_id, instance)
            if result.get('bound') and result.get('route'):
                result['mcp'] = acp_mcp_descriptor(port, result['route'])
            handler.respond(200, result)
            return
        if action == 'unbind':
            route = required_text(body, 'route', 48)
            handler.respond(200, store.teams.unbind_acp(route))
            return
        if action == 'poll':
            if isinstance(body.get('routes'), list):
                routes = body['routes']
                if len(routes) > 50 or any(not isinstance(r, str) or not 1 <= len(r) <= 48 for r in routes):
                    raise ValueError('routes 必须是最多 50 条有效 route 字符串的列表。')
                handler.respond(200, {'results': {r: _acp_poll_payload(handler, route=r) for r in routes}})
                return
            if body.get('route'):
                handler.respond(200, _acp_poll_payload(handler, route=required_text(body, 'route', 48)))
                return
            if body.get('instance'):
                handler.respond(200, _acp_poll_payload(handler, instance=required_text(body, 'instance', 64)))
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
            handler.respond(200, ctx)
            return
        handle_peer_request(handler, action, team, team['member'], body)


def handle_agy_cli_request(handler):
    action = handler.path.split('/')[-1]
    body = handler.read_json()
    store = handler.server.store
    port = handler.server.server_port
    with handler.server.members_lock:
        if action == 'bind':
            code = required_text(body, 'code', 10).strip().lower()
            session_id = body.get('session_id')
            if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 128):
                raise ValueError('session_id 格式不正确。')
            result = store.teams.bind_agy_cli(code, session_id=session_id, label=body.get('label'))
            result['wait_command'] = agy_cli_wait_command(port, result['route'])
            result['instruction'] = '请立即在后台运行 wait_command 启动静默收件监听（不要设置 NotificationTimeoutSeconds 等周期提醒超时）；监听会持续待机，仅在收到队友消息或绑定失效时退出并唤醒，处理回复后重新挂起同一条 wait_command。'
            handler.respond(200, result)
            return
        if action == 'restore':
            session_id = body.get('session_id')
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
                handler.respond(200, {'bound': False})
                return
            result = store.teams.restore_agy_cli(session_id)
            if result.get('bound') and result.get('route'):
                result['wait_command'] = agy_cli_wait_command(port, result['route'])
                result['instruction'] = '已自动恢复绑定。请立即在后台运行 wait_command 恢复静默收件监听（不要设置 NotificationTimeoutSeconds 等周期提醒超时）；仅在收到队友消息或绑定失效时退出并唤醒，处理回复后重新挂起同一条 wait_command。'
            handler.respond(200, result)
            return
        if action == 'poll':
            route = body.get('route')
            session_id = body.get('session_id')
            team = (store.teams.agy_cli_member(route=route, touch=True) if isinstance(route, str) and route
                    else store.teams.agy_cli_member(session_id=session_id, touch=True) if isinstance(session_id, str) and session_id
                    else None)
            if not team:
                handler.respond(200, {'bound': False})
                return
            is_peer = team_mode(team) == 'peer'
            delivery = store.messages.next(team['id'], team['member'])
            if delivery and body.get('accept') is True:
                store.messages.received(team['id'], team['member'], delivery['id'], delivery['revision'])
            handler.respond(200, {
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
        handle_peer_request(handler, action, team, team['member'], body)


def handle_desktop_request(handler):
    action = handler.path.split('/')[-1]
    body = handler.read_json()
    instance = str(uuid.UUID(required_text(body, 'instance', 36)))
    server = handler.server
    store = server.store
    if action == 'restore':
        session_id = str(uuid.UUID(required_text(body, 'session_id', 36)))
        with server.members_lock:
            server.expire_members()
            if any(owner == instance for owner, _, _ in server.members.values()):
                raise Conflict('这个会话已经绑定另一个成员。')
            restored = store.teams.restore_desktop(instance, session_id)
            if not restored.get('bound'):
                handler.respond(200, {'bound': False})
                return
            member = restored['member']
            previous = server.members.get(member)
            if previous and previous[0] != instance:
                store.interrupt_member(member, previous[0])
            server.members[member] = (instance, time.monotonic(), True)
            handler.respond(200, restored)
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
            handler.respond(200, {'bound': True, 'member': member, 'team': team['name']})
            return
        if not binding or binding[0] != instance:
            raise Conflict('当前 Desktop 绑定已失效。')
        server.members[member] = (instance, time.monotonic(), True)
        if action == 'poll':
            task = store.claim(member, instance) if body.get('ready') is True else None
            message = store.inbox(team['id']) if team['captain'] == member else None
            handler.respond(200, {'task': dict(task) if task else None, 'message': message,
                                  'delivery': store.messages.next(team['id'], member)})
        elif action == 'complete':
            task_id = required_text(body, 'task_id', 36)
            status = body.get('status')
            if status not in ('completed', 'interrupted'):
                raise ValueError('Expected completed or interrupted.')
            result = required_text(body, 'result', 12000)
            if not store.finish_member(member, instance, task_id, status, result):
                raise Conflict('这个旧任务不属于此会话。')
            handler.respond(200, {'accepted': True})
        elif action == 'ack':
            if team['captain'] != member or not store.acknowledge(
                    body.get('task_id'), body.get('message_id'), team['id']):
                raise Conflict('该旧结果不属于此窗口。')
            handler.respond(200, {'acknowledged': True})
        else:
            handle_peer_request(handler, action, team, member, body)


def handle_project_request(handler):
    action = handler.path.split('/')[-1]
    body = handler.read_json()
    server = handler.server
    store = server.store
    with server.members_lock:
        if action == 'context':
            product = body.get('product')
            session_id = body.get('session_id')
            event = body.get('event')
            if product == 'codex' and isinstance(session_id, str) and session_id:
                server.note_codex_hook(session_id, event or 'UserPromptSubmit')
            handler.respond(200, store.projects.context(body.get('team_id'), product,
                                                        session_id, event,
                                                        body.get('paths')))
            return
        if action == 'inbox':
            session_id = body.get('session_id')
            event = body.get('event')
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 64:
                handler.respond(200, {'delivery': None})
                return
            if event not in ('PostToolUse', 'Stop'):
                handler.respond(200, {'delivery': None})
                return
            stop_active = bool(body.get('stop_hook_active'))
            with store.connect() as db:
                if body.get('scope') != 'global':
                    if not store.projects.deployed_team(db, body.get('team_id'), body.get('paths')):
                        handler.respond(200, {'delivery': None})
                        return
                row = db.execute('SELECT member FROM codex_bindings WHERE thread_id=?', (session_id,)).fetchone()
                if not row:
                    handler.respond(200, {'delivery': None})
                    return
                member = row[0]
                team = store.teams.member_team(member, db)
                if not team:
                    handler.respond(200, {'delivery': None})
                    return
                if team_mode(team) != 'peer':
                    db.execute('''INSERT INTO project_hook_events VALUES (?,?,?,?)
                                  ON CONFLICT(team_id,product) DO UPDATE SET last_seen=excluded.last_seen,event=excluded.event''',
                               (team['id'], 'codex', time.time(), event))
            if event == 'Stop' and stop_active:
                server.note_codex_hook(session_id, 'Stop', stop_active=True)
                handler.respond(200, {'delivery': None})
                return
            delivery = store.messages.claim_codex_hook(team['id'], member)
            if delivery:
                server.note_codex_hook(session_id, event, stop_active=False)
                try:
                    handler.respond(200, {'delivery': delivery})
                except OSError:
                    store.messages.finalize_codex_hook(team['id'], member, delivery['id'], delivery['revision'], 'unconfirmed')
                    raise
                store.messages.finalize_codex_hook(team['id'], member, delivery['id'], delivery['revision'], 'accepted')
            else:
                server.note_codex_hook(session_id, event, stop_active=(event == 'Stop'))
                handler.respond(200, {'delivery': None})
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
            server.note_codex_hook(session_id, 'PreToolUse')
            key = uuid.uuid4().hex
            tickets[key] = (row[0], tool, time.monotonic() + 15, session_id)
            handler.respond(200, {'ticket': key})
            return
        if action == 'tool':
            ticket = tickets.pop(required_text(body, 'ticket', 32), None)
            if ticket is None or ticket[2] <= time.monotonic() or not store.is_codex_binding(ticket[0], ticket[3]):
                raise Conflict('请确认项目 Hook 已启用、并绑定此 Codex 聊天。')
            team = store.teams.member_team(ticket[0])
            if not team:
                raise Conflict('此成员当前没有 Team。')
            handle_peer_request(handler, ticket[1], team, ticket[0], body)
            return
        handler.respond(404, {'error': 'Unknown project operation.'})
