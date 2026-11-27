"""One-shot local inbox probe, scoped to the Desktop session that armed it."""

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
if __package__:
    from .desktop_member import DesktopMember, write_inbox
else:
    from desktop_member import DesktopMember, write_inbox


RUNS = Path(__file__).resolve().parents[1] / '.team-hub' / 'desktop-probes'
active = None
member = DesktopMember(int(sys.argv[1]) if __name__ == '__main__' and len(sys.argv) > 1 else 58767)


def wait_for_trigger(run, address, token):
    for _ in range(600):
        if (run / 'trigger').is_file():
            break
        time.sleep(1)
    else:
        (run / 'expired').touch()
        return
    prompt = (f'[Team Hub local wake test {run.name}] Reply in this chat with the '
              'test word previously supplied by the operator. Call record_desktop_reply '
              'with that test word and this request ID. If unknown, use UNKNOWN.')
    try:
        write_inbox(address, token, run.name, prompt)
        (run / 'written').touch()
    except OSError as error:
        (run / 'error').write_text(f'{type(error).__name__}: {error.errno}', encoding='ascii')


def arm():
    global active
    address = os.environ.get('CLAUDE_CODE_MESSAGING_SOCKET', '')
    token = os.environ.get('CLAUDE_CODE_MESSAGING_TOKEN', '')
    if not token or probe()['address_format'] != 'windows_named_pipe':
        return {'armed': False, 'token_present': bool(token), **probe()}
    if active is None or any((active / name).exists() for name in ('reply.json', 'error', 'expired')):
        active = RUNS / str(uuid.uuid4())
        active.mkdir(parents=True)
        threading.Thread(target=wait_for_trigger, args=(active, address, token), daemon=True).start()
    return {'armed': True, 'request_id': active.name, 'expires_in_minutes_from_arming': 10}


def probe():
    address = os.environ.get('CLAUDE_CODE_MESSAGING_SOCKET', '')
    session_id = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    host_session_id = os.environ.get('CLAUDE_CODE_HOST_SESSION_ID', '')
    return {
        'socket_exposed_to_mcp': bool(address),
        'address_format': ('windows_named_pipe' if address.lower().startswith('\\\\.\\pipe\\')
                           else 'other' if address else 'absent'),
        'session_id_present': bool(session_id),
        'session_id_prefix': session_id[:8] if session_id else None,
        'host_session_id_present': bool(host_session_id),
        'hub_token_present': bool(member.hub_token),
        'bound_member': member.name,
        'claude_env_keys': sorted(key for key in os.environ if key.upper().startswith('CLAUDE')),
        'pid': os.getpid(),
        'ppid': os.getppid(),
    }


def serve(request):
    method = request.get('method')
    if method == 'initialize':
        return {'protocolVersion': request['params']['protocolVersion'],
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'team-hub-claude', 'version': '5'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [{'name': 'inspect_desktop_inbox',
                           'description': 'Report whether this MCP process received the Claude messaging endpoint.',
                           'inputSchema': {'type': 'object', 'properties': {}}},
                          {'name': 'arm_desktop_wake', 'description': 'Arm this session for one local operator wake test.',
                           'inputSchema': {'type': 'object', 'properties': {}}},
                          {'name': 'record_desktop_reply', 'description': 'Record the test word after a wake message.',
                           'inputSchema': {'type': 'object', 'properties': {
                               'request_id': {'type': 'string'}, 'word': {'type': 'string', 'maxLength': 80}},
                               'required': ['request_id', 'word']}},
                          {'name': 'bind_team_member', 'description': 'Bind this original Desktop conversation to its Team Hub member name.',
                           'inputSchema': {'type': 'object', 'properties': {
                               'name': {'type': 'string'}, 'code': {'type': 'string'}},
                                           'required': ['name']}},
                          {'name': 'report_team_result', 'description': 'Return the findings of the assigned Hub task to Captain.',
                           'inputSchema': {'type': 'object', 'properties': {
                               'ref': {'type': 'string'}, 'result': {'type': 'string', 'maxLength': 12000}},
                               'required': ['ref', 'result']}},
                          {'name': 'team_dispatch', 'description': 'Send an instruction to any member in this Team.',
                           'inputSchema': {'type': 'object', 'properties': {
                               'to': {'type': 'string'}, 'instruction': {'type': 'string'}},
                               'required': ['to', 'instruction']}},
                          {'name': 'team_send', 'description': '向同 Team 的成员按成员名发信。',
                           'inputSchema': {'type': 'object', 'properties': {
                               'to': {'type': 'string'}, 'text': {'type': 'string'}},
                               'required': ['to', 'text']}},
                          {'name': 'team_members', 'description': '查看当前 Team 最新的成员名、职位、实际模型、负责工作树、职责和连接状态。',
                           'inputSchema': {'type': 'object', 'properties': {}}},
                          {'name': 'team_whoami', 'description': '查看当前会话绑定的实时成员名、职位、角色版本、实际模型与负责工作树。',
                           'inputSchema': {'type': 'object', 'properties': {}}},
                          {'name': 'acknowledge_team_result', 'description': 'Confirm the current Captain result.',
                           'inputSchema': {'type': 'object', 'properties': {'ref': {'type': 'string'}}, 'required': ['ref']}}]}
    if method == 'tools/call' and request['params']['name'] in (
            'bind_team_member', 'report_team_result', 'team_dispatch', 'team_send', 'team_members',
            'team_whoami', 'acknowledge_team_result'):
        args = request['params'].get('arguments', {})
        name = request['params']['name']
        if name == 'bind_team_member':
            result = member.bind(args.get('name'), args.get('code'))
        elif name == 'report_team_result':
            result = member.report(args.get('ref'), args.get('result'))
        elif name == 'team_dispatch':
            result = member.dispatch(args.get('to'), args.get('instruction'))
        elif name == 'team_send':
            result = member.dispatch(args.get('to'), args.get('text'))
        elif name == 'team_members':
            result = member.captain_request('roster')
        elif name == 'team_whoami':
            result = member.captain_request('whoami')
        else:
            result = member.acknowledge(args.get('ref'))
        return {'isError': 'error' in result, 'content': [{'type': 'text', 'text': json.dumps(result)}]}
    if method == 'tools/call' and request['params']['name'] == 'arm_desktop_wake':
        return {'content': [{'type': 'text', 'text': json.dumps(arm())}]}
    if method == 'tools/call' and request['params']['name'] == 'record_desktop_reply':
        args = request['params'].get('arguments', {})
        word = args.get('word')
        ok = (active is not None and active.name == args.get('request_id')
              and (active / 'trigger').exists() and isinstance(word, str) and 0 < len(word) <= 80)
        if ok:
            (active / 'reply.json').write_text(json.dumps({'request_id': active.name, 'word': word}), encoding='utf-8')
        return {'isError': not ok, 'content': [{'type': 'text', 'text': json.dumps({'recorded': ok})}]}
    if method == 'tools/call' and request['params']['name'] == 'inspect_desktop_inbox':
        return {'content': [{'type': 'text', 'text': json.dumps(probe())}]}
    raise KeyError(method)


if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('method') == 'notifications/initialized':
            member.start_auto_recover()
            continue
        if 'id' not in request:
            continue
        response = {'jsonrpc': '2.0', 'id': request['id']}
        try:
            response['result'] = serve(request)
        except KeyError:
            response['error'] = {'code': -32601, 'message': 'Unknown method or tool'}
        print(json.dumps(response), flush=True)
        if request.get('method') == 'tools/list':
            member.start_auto_recover()
