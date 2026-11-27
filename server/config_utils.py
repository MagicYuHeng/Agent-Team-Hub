"""Shared leaf utilities for repository root anchoring, atomic file writes, and Hook/MCP config encoding."""

import json
import os
import shlex
import subprocess
import tomllib
import uuid
from pathlib import Path

from teams import Conflict


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT
AGENT = REPO_ROOT / 'project_agent.py'
CODEX_BLOCK = ('# BEGIN Team Hub project MCP', '# END Team Hub project MCP')


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


def command(args):
    if os.name == 'nt':
        # Use forward slashes so Windows Git Bash (used by Claude hooks), cmd, and PowerShell all run cleanly.
        normalized = [arg.replace('\\', '/') if isinstance(arg, str) else arg for arg in args]
        return subprocess.list2cmdline(normalized)
    return shlex.join(args)


def is_managed_hook(cmd, product, team_id):
    if not isinstance(cmd, str):
        return False
    marker = 'project_agent.py'
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
            timeout = 2 if event in ('PostToolUse', 'Stop') else 4
            handler = {'type': 'command', 'command': runner, 'timeout': timeout}
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
