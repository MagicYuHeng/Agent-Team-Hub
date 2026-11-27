"""Scoped tools added to the installed Aux Relay MCP server."""

import json
import uuid


def tool(name, description, props, required, app_only=False):
    spec = {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': {key: {'type': 'string'} for key in props},
        'required': required, 'additionalProperties': False}}
    if app_only:
        spec['_meta'] = {'ui': {'visibility': ['app']}}
    return spec


TOOLS = [
    tool('bind_team_window', 'Redeem a management console code for this sidebar.',
         ['instance', 'code', 'proof'], ['instance', 'code'], app_only=True),
    tool('restore_team_window', 'Restore this sidebar using proof issued by the native chat opener.',
         ['instance', 'proof'], ['instance', 'proof'], app_only=True),
    tool('poll_team_window', 'Fetch only the inbox belonging to this sidebar.',
         ['instance'], ['instance'], app_only=True),
    tool('team_send', '向同 Team 成员发信，route 使用当前侧栏绑定时提供的参数。',
         ['route', 'to', 'text'], ['route', 'to', 'text']),
    tool('team_members', '查看当前 Team 成员名、分工、负责工作树、职责与连接状态。',
         ['route'], ['route']),
    tool('team_whoami', '查看当前原会话绑定的实时成员名、分工、版本与负责工作树。',
         ['route'], ['route']),
    tool('team_cli_bind', '在 AGY CLI 原会话中用管理台生成的 10 位配对码绑定 Team Hub。调用成功后会返回 route 与 wait_command，请立即在后台执行 wait_command 启动自动收件监听，后续每次被队友消息唤醒并回复后重新挂起该监听。',
         ['code', 'label'], ['code']),
    tool('team_cli_restore', '当用户说「恢复收件」「继续收件」「恢复 Team」「重连」或在 AGY CLI 重启/--resume 后调用：自动按当前会话 ID 恢复 Team Hub 绑定，拿到返回的 wait_command 后请立即在后台运行它以恢复自动收件唤醒（无需再向用户要配对码）。',
         [], []),
    tool('team_cli_poll', '在 AGY CLI 原会话中手动拉取并确认一条待送达的队友消息（方案 C 兜底）。',
         ['route'], ['route']),
    tool('acknowledge_team_result', 'Confirm delivery of a previous Hub task result.',
         ['route', 'ref'], ['route', 'ref'], app_only=True),
    {'name': 'receive_team_message', 'description': 'Record that the sidebar accepted a message.',
     'inputSchema': {'type': 'object', 'required': ['route', 'id', 'revision'], 'properties': {
         'route': {'type': 'string'}, 'id': {'type': 'string'}, 'revision': {'type': 'integer'}}},
     '_meta': {'ui': {'visibility': ['app']}}},
]

PATHS = {'bind_team_window': 'bind', 'poll_team_window': 'poll', 'acknowledge_team_result': 'ack',
         'team_send': 'send', 'team_members': 'roster', 'team_whoami': 'whoami',
         'receive_team_message': 'received', 'restore_team_window': 'restore'}


def open_result(meta, hub_http_json):
    result = {
        'content': [{'type': 'text', 'text': 'Aux Relay panel opened.'}],
        'structuredContent': {'status': 'ready'},
    }
    conversation_id = meta.get('antigravity.google/conversation_id') if isinstance(meta, dict) else None
    if isinstance(conversation_id, str):
        try:
            status, answer = hub_http_json('POST', '/antigravity/open', {'conversation_id': conversation_id}, timeout=4)
        except (OSError, ValueError):
            return result
        if status == 200 and answer.get('proof'):
            result['_meta'] = {'team_hub_receive_proof': answer['proof']}
    return result


def handle(name, args, request_id, hub_http_json, respond, meta=None):
    if name in ('team_cli_bind', 'team_cli_restore', 'team_cli_poll'):
        conv_id = meta.get('antigravity.google/conversation_id') if isinstance(meta, dict) else None
        if name == 'team_cli_bind':
            payload = {'code': args.get('code', '')}
            if args.get('label'):
                payload['label'] = args['label']
            if isinstance(conv_id, str) and conv_id.strip():
                payload['session_id'] = conv_id.strip()
            ep = '/agy-cli/bind'
        elif name == 'team_cli_restore':
            payload = {'session_id': conv_id.strip() if isinstance(conv_id, str) else ''}
            ep = '/agy-cli/restore'
        else:
            payload = {'route': args.get('route', ''), 'accept': True}
            ep = '/agy-cli/poll'
        try:
            status, response = hub_http_json('POST', ep, payload, timeout=4)
        except (OSError, ValueError):
            status, response = 503, {'error': '暂时连不上 Team Hub。'}
        if not 200 <= status < 300:
            respond(request_id, response.get('error', 'Team Hub 请求失败。'), response, is_error=True)
            return True
        respond(request_id, json.dumps(response, ensure_ascii=False), response)
        return True

    path = PATHS.get(name)
    if not path:
        return False
    if path == 'send':
        args = args | {'request_id': str(uuid.uuid4())}
    try:
        status, response = hub_http_json('POST', '/antigravity/' + path, args, timeout=4)
    except (OSError, ValueError):
        status, response = 503, {'error': '暂时连不上 Team Hub。'}
    if not 200 <= status < 300:
        respond(request_id, response.get('error', 'Team Hub 请求失败。'), response, is_error=True)
        return True
    if path == 'send':
        response.pop('task_id', None)
    if path == 'poll' and response.get('message'):
        message = response.pop('message')
        ref = message['task_id'].replace('-', '')[:12]
        response['legacy_delivery'] = {
            'id': message['message_id'],
            'ref': ref,
            'prompt': f"【来自 {message['from']}】\n{message['result']}",
        }
    text = 'OK'
    if path in ('roster', 'whoami'):
        text = json.dumps(response, ensure_ascii=False)
    elif path == 'ack':
        text = '结果已确认。'
    respond(request_id, text, response)
    return True
