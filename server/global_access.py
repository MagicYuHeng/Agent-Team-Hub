"""User-level cross-project client access (~/.claude.json, ~/.codex/*) and dynamic client launch descriptors."""

import sys
import tomllib
from pathlib import Path

from server.config_utils import (
    AGENT,
    SOURCE,
    command,
    encode_json,
    mcp_config,
    read_json,
    register_hooks,
    write_file,
)
from teams import Conflict


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
    codex_steer_hook = False
    if codex_hooks_path.is_file():
        try:
            hdata = read_json(codex_hooks_path.read_bytes())
            hooks_map = hdata.get('hooks') or {}
            def _has_cmd(event_name):
                groups = hooks_map.get(event_name) or []
                return any(
                    isinstance(g, dict)
                    and len(g.get('hooks', [])) == 1
                    and g['hooks'][0].get('command') == codex_hook_cmd
                    for g in groups
                )
            codex_ticket_hook = _has_cmd('PreToolUse')
            codex_steer_hook = _has_cmd('PostToolUse') and _has_cmd('Stop')
        except (OSError, ValueError):
            codex_ticket_hook = False
            codex_steer_hook = False

    ag_plugin = home / '.gemini' / 'config' / 'plugins' / 'mcp-aux-relay-plugin' / 'plugin.json'
    return {
        'claude_mcp': claude_mcp,
        'codex_mcp': codex_mcp,
        'codex_ticket_hook': codex_ticket_hook,
        'codex_steer_hook': codex_steer_hook,
        'antigravity_plugin': ag_plugin.is_file(),
        'ready': bool(claude_mcp and codex_mcp and codex_ticket_hook and codex_steer_hook),
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

    # 3. Codex user-level ~/.codex/hooks.json (PreToolUse ticket + PostToolUse/Stop inbox steer; zero SessionStart/UserPromptSubmit)
    codex_hooks_path = home / '.codex' / 'hooks.json'
    codex_hooks_raw = codex_hooks_path.read_bytes() if codex_hooks_path.is_file() else None
    codex_hooks_data = read_json(codex_hooks_raw)
    register_hooks(
        codex_hooks_data,
        {
            'PreToolUse': '^mcp__team_hub__team_(send|members|whoami)$',
            'PostToolUse': '.*',
            'Stop': None,
        },
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
