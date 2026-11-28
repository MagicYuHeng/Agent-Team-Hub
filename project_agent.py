"""Project hook adapters and native Codex Team Hub tools."""

import http.client
import json
import os
import sys
import time
import uuid


def hub_token():
    token = os.environ.get('TEAM_HUB_TOKEN')
    if token or os.name != 'nt':
        return token

    # Codex can start stdio MCP servers with a filtered environment.
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_QUERY_VALUE) as key:
            token, _ = winreg.QueryValueEx(key, 'TEAM_HUB_TOKEN')
    except OSError:
        return None
    return token if isinstance(token, str) else None


def request_path(port, path, body, timeout=4):
    token = hub_token()
    if not token:
        return {'error': '找不到 TEAM_HUB_TOKEN。请先为当前用户配置与本机 Hub 一致的令牌。'}
    raw = json.dumps(body).encode('utf-8')
    connection = http.client.HTTPConnection('127.0.0.1', int(port), timeout=timeout)
    try:
        connection.request('POST', path, raw,
                           {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        return json.load(connection.getresponse())
    except (OSError, ValueError, http.client.HTTPException):
        return {'error': '暂时连不上本机 Team Hub。'}
    finally:
        connection.close()


def request(port, action, body, timeout=4):
    return request_path(port, f'/project/{action}', body, timeout=timeout)


def format_context(result):
    header = (f"【Team Hub 分工已更新（v{result.get('role_revision', 1)}）】\n"
              if result.get('mode') == 'refresh' else '')
    lines = [
        f"你当前在「{result['team']}」小队，成员名：{result['name']}，分工：{result['role']}（v{result.get('role_revision', 1)}）。"
    ]
    wt_display = result.get('worktree') or '项目内不限（注意避免多线写入冲突）'
    if result.get('worktree_raw'):
        lines.append(
            f"主要工作树：{wt_display}（优先在此工作树内推进；如需改动其他工作树，先和相关同事或用户打声招呼）。"
        )
    else:
        lines.append(f"主要工作树：{wt_display}。")
    if result.get('responsibility'):
        lines.append(f"主要负责：{result['responsibility']}。")
    if result.get('product') == 'codex':
        lines.append('协作收件说明：工作途中经由本机 Hook 传来的「【来自 …】」消息是当前 Team 内其他成员发来的正常同步，请顺手一并处理。')
    sections = [header + '\n'.join(lines)]
    if result.get('profile'):
        sections.append(result['profile'])
    if result.get('team_rules'):
        sections.append(result['team_rules'])
    if result.get('has_ai'):
        sections.append(
            '阶段进度存档提醒：当前工作树包含 `.ai/CURRENT.md`，修改代码前请先核对当前交接状态，完成阶段工作提交时按项目约定同步归档。'
        )
    return '\n\n'.join(sections)


def run_hook(product, team_scope, port):
    payload = json.load(sys.stdin)
    event = payload.get('hook_event_name', payload.get('hookEventName', 'SessionStart'))
    is_global = str(team_scope) == 'global'
    if product == 'codex' and event in ('PostToolUse', 'Stop'):
        session_id = payload.get('session_id')
        if not session_id:
            return None
        body = {
            'session_id': session_id,
            'event': event,
            'stop_hook_active': bool(payload.get('stop_hook_active')),
        }
        if is_global:
            body['scope'] = 'global'
        else:
            body['team_id'] = int(team_scope)
            body['paths'] = [payload.get('cwd')]
        result = request(port, 'inbox', body, timeout=0.45)
        delivery = result.get('delivery') if isinstance(result, dict) else None
        prompt = delivery.get('prompt') if isinstance(delivery, dict) else None
        if not prompt:
            return None
        if event == 'PostToolUse':
            return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': prompt}}
        if not body['stop_hook_active']:
            return {'decision': 'block', 'reason': prompt}
        return None
    if product == 'codex' and event == 'PreToolUse':
        tool = {'mcp__team_hub__team_send': 'send',
                'mcp__team_hub__team_members': 'roster',
                'mcp__team_hub__team_whoami': 'whoami'}.get(payload.get('tool_name'))
        if not tool:
            return None
        tool_input = payload.get('tool_input') or {}
        if tool_input.get('team_hub_ticket'):
            return None
        if is_global:
            body = {'scope': 'global', 'session_id': payload.get('session_id'), 'tool': tool}
        else:
            body = {'team_id': int(team_scope), 'paths': [payload.get('cwd')],
                    'session_id': payload.get('session_id'), 'tool': tool}
        result = request(port, 'ticket', body)
        if 'ticket' not in result:
            return None
        return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'allow',
                'updatedInput': tool_input | {'team_hub_ticket': result['ticket']}}}
    if is_global:
        return None
    if product == 'antigravity':
        raw_paths = list(payload.get('workspacePaths') or [])
        if payload.get('cwd') and payload['cwd'] not in raw_paths:
            raw_paths.append(payload['cwd'])
        paths = raw_paths
    else:
        paths = [payload.get('cwd')]
    scope = {'team_id': int(team_scope), 'paths': paths}
    session = (payload.get('conversationId', payload.get('session_id')) if product == 'antigravity'
               else payload.get('session_id', payload.get('conversationId')))
    result = request(port, 'context', scope | {'product': product,
                                      'session_id': session,
                                      'event': payload.get('source', event)})
    chunks = []
    if 'role' in result:
        chunks.append(format_context(result))
    if result.get('cli_delivery'):
        chunks.append(result['cli_delivery'])
    if not chunks:
        return {} if product == 'antigravity' else None
    context = '\n\n'.join(chunks)
    if product == 'antigravity':
        return {'injectSteps': [{'systemMessage': {'systemMessage': context}}]}
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': context}}


TOOLS = [
    {'name': 'team_send', 'description': '向当前 Team 的成员发信。用成员名指定收件人。',
     'inputSchema': {'type': 'object', 'required': ['to', 'text'], 'properties': {
         'to': {'type': 'string'}, 'text': {'type': 'string', 'maxLength': 12000},
         'mode': {'type': 'string', 'enum': ['auto', 'queue', 'steer'],
                  'description': '投递模式：auto（默认，执行中优先同轮送达、空闲时排队唤醒）、queue（仅排队）、steer（优先同轮引导）。'}}}},
    {'name': 'team_members', 'description': '查看当前 Team 的最新成员名、分工、负责工作树、职责与连接状态。',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'team_whoami', 'description': '查看当前原会话绑定的实时成员名、分工、版本与负责工作树。',
     'inputSchema': {'type': 'object', 'properties': {}}},
]


def serve(port, frame):
    method = frame.get('method')
    params = frame.get('params', {})
    if method == 'initialize':
        return {'protocolVersion': params['protocolVersion'], 'serverInfo': {'name': 'team-hub', 'version': '1.1'},
                'capabilities': {'tools': {}}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method != 'tools/call' or params.get('name') not in ('team_send', 'team_members', 'team_whoami'):
        raise KeyError(method)
    args = params.get('arguments', {})
    ticket = args.get('team_hub_ticket')
    if not ticket:
        result = {'error': '请先绑定 Codex 原聊天，并在客户端确认 Team Hub 项目 Hook。'}
    else:
        result = request(port, 'tool', {'ticket': ticket, 'to': args.get('to'), 'text': args.get('text'),
                                        'mode': args.get('mode')})
        result.pop('task_id', None)
    return {'isError': 'error' in result, 'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}


def mcp(port):
    for line in sys.stdin:
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if 'id' not in frame:
            continue
        reply = {'jsonrpc': '2.0', 'id': frame['id']}
        try:
            reply['result'] = serve(port, frame)
        except KeyError:
            reply['error'] = {'code': -32601, 'message': 'Unknown method or tool'}
        print(json.dumps(reply, ensure_ascii=False), flush=True)


def serve_route(port, route, frame):
    method = frame.get('method')
    params = frame.get('params', {})
    if method == 'initialize':
        return {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                'serverInfo': {'name': 'team-hub', 'version': '1.2'},
                'capabilities': {'tools': {}}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    name = params.get('name')
    if method != 'tools/call' or name not in ('team_send', 'team_members', 'team_whoami'):
        raise KeyError(method)
    if not route:
        result = {'error': '当前会话尚未绑定 Team Hub（缺少 TEAM_HUB_ROUTE）。'}
    else:
        args = params.get('arguments') or {}
        if name == 'team_send':
            result = request_path(port, '/acp/send', {
                'route': route, 'to': args.get('to'), 'text': args.get('text'),
                'mode': args.get('mode'),
                'request_id': str(uuid.uuid4()),
            })
            result.pop('task_id', None)
        elif name == 'team_members':
            result = request_path(port, '/acp/roster', {'route': route})
        else:
            result = request_path(port, '/acp/whoami', {'route': route})
    return {'isError': 'error' in result, 'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}


def route_mcp(port, route=None):
    effective_route = (route or os.environ.get('TEAM_HUB_ROUTE') or '').strip()
    for line in sys.stdin:
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if 'id' not in frame:
            continue
        reply = {'jsonrpc': '2.0', 'id': frame['id']}
        try:
            reply['result'] = serve_route(port, effective_route, frame)
        except KeyError:
            reply['error'] = {'code': -32601, 'message': 'Unknown method or tool'}
        print(json.dumps(reply, ensure_ascii=False), flush=True)


TRANSIENT_HUB_ERROR = '暂时连不上本机 Team Hub。'


def wait_inbox(port, route, timeout=0, disconnect_grace=60.0, poll_interval=1.5):
    """Block silently until a teammate message arrives, binding ends, or Hub stays unreachable past grace."""
    timeout_val = int(timeout)
    deadline = (time.monotonic() + max(1, timeout_val)) if timeout_val > 0 else None
    disconnect_since = None
    backoff = max(0.05, float(poll_interval))
    try:
        while True:
            res = request_path(port, '/agy-cli/poll', {'route': route})
            err = res.get('error')
            if err:
                if err != TRANSIENT_HUB_ERROR:
                    print(f'[Team Hub 监听中断] {err}', flush=True)
                    return 1
                now = time.monotonic()
                if disconnect_since is None:
                    disconnect_since = now
                if now - disconnect_since >= max(0.0, float(disconnect_grace)):
                    print(f'[Team Hub 监听中断] {err}', flush=True)
                    return 1
                if deadline is not None and now >= deadline:
                    print(f'[Team Hub 监听中断] {err}', flush=True)
                    return 1
                sleep_sec = backoff
                if deadline is not None:
                    sleep_sec = min(sleep_sec, max(0.05, deadline - now))
                time.sleep(sleep_sec)
                backoff = min(8.0, backoff * 2.0)
                continue
            disconnect_since = None
            backoff = max(0.05, float(poll_interval))
            if not res.get('bound'):
                print('[Team Hub 监听结束] 当前 AGY CLI 会话绑定已失效，请重新绑定。', flush=True)
                return 1
            delivery = res.get('delivery')
            if delivery and delivery.get('prompt'):
                request_path(port, '/agy-cli/received', {
                    'route': route,
                    'id': delivery['id'],
                    'revision': delivery['revision'],
                })
                print(delivery['prompt'], flush=True)
                print('\n[Team Hub] 收到队友消息；处理并回复后，请重新在后台运行同一条 wait-inbox 命令以继续静默收件（不要设置周期提醒超时）。', flush=True)
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                print('[Team Hub] 后台监听已达到指定等待时长（当前无新消息）。', flush=True)
                return 0
            time.sleep(max(0.05, float(poll_interval)))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1] == 'hook':
        scope_arg = sys.argv[3] if sys.argv[3] == 'global' else int(sys.argv[3])
        response = run_hook(sys.argv[2], scope_arg, int(sys.argv[4]))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False))
    elif sys.argv[1] == 'route-mcp':
        route_arg = sys.argv[3] if len(sys.argv) > 3 else None
        route_mcp(int(sys.argv[2]), route_arg)
    elif sys.argv[1] == 'wait-inbox':
        timeout_sec = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        sys.exit(wait_inbox(int(sys.argv[2]), sys.argv[3], timeout=timeout_sec))
    else:
        mcp(int(sys.argv[2]))
