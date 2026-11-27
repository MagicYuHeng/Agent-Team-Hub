"""Build a candidate upgrade without touching the installed Antigravity plugin."""

import argparse
import hashlib
import json
from pathlib import Path
from shutil import copyfile
import subprocess


SOURCE = Path(__file__).resolve().parent
PACKAGE = SOURCE / 'package'
MARKER = 'from team_relay import TOOLS as SCOPED_TOOLS, handle as handle_scoped_tool'


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'The installed relay layout changed near {old.splitlines()[0]}')
    return text.replace(old, new, 1)


def upgrade(text):
    opener = ('            rpc_tool_result(req_id, "Aux Relay panel opened.", {"status": "ready"})\n')
    probe_opener = ('            from team_relay import open_result\n'
                    '            send_rpc({"jsonrpc": "2.0", "id": req_id, '
                    '"result": open_result(params.get("_meta") or {})})\n')
    replacement = probe_opener.replace('params.get("_meta") or {}', 'params.get("_meta"), hub_http_json')
    if probe_opener in text:
        text = replace_once(text, probe_opener, replacement)
    elif replacement not in text:
        text = replace_once(text, opener, replacement)
    if MARKER in text:
        for old_version in ('0.4.0', '0.5.0', '0.5.1', '0.5.2'):
            text = text.replace(f'"name": "mcp-aux-relay", "version": "{old_version}"',
                                '"name": "mcp-aux-relay", "version": "0.6.0"')
        return text
    text = replace_once(text, 'from urllib.parse import parse_qs, urlparse\n',
                        'from urllib.parse import parse_qs, urlparse\n' + MARKER + '\n')
    text = replace_once(text, '\n]\n\n\n', '\n] + SCOPED_TOOLS\n\n'
        'next(tool for tool in TOOLS if tool["name"] == "team_dispatch")["inputSchema"]["properties"]["route"] = {\n'
        '    "type": "string", "description": "Binding argument issued in this original chat by its Team sidebar"\n'
        '}\n\n')
    text = replace_once(text, '    dispatch_key = hashlib.sha256(f"{to_agent}\\n{instruction}".encode("utf-8")).hexdigest()[:16]',
        '    route = args.get("route")\n'
        '    identity = f"{to_agent}\\n{instruction}" if route is None else json.dumps([route, to_agent, instruction])\n'
        '    dispatch_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]')
    text = replace_once(text, '    last_error = None\n    resp_code = 0',
        '    path = "/tasks"\n'
        '    if route is not None:\n'
        '        path = "/antigravity/dispatch"\n'
        '        payload["route"] = route\n'
        '    last_error = None\n    resp_code = 0')
    text = replace_once(text, 'hub_http_json("POST", "/tasks", payload, timeout=8.0)',
                        'hub_http_json("POST", path, payload, timeout=8.0)')
    text = replace_once(text, '                    "serverInfo": {"name": "mcp-aux-relay", "version": "0.3.0"},',
                        '                    "serverInfo": {"name": "mcp-aux-relay", "version": "0.6.0"},')
    text = replace_once(text, '        args = params.get("arguments") or {}\n',
        '        args = params.get("arguments") or {}\n'
        '        if handle_scoped_tool(name, args, req_id, hub_http_json, rpc_tool_result):\n'
        '            return\n')
    return text


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installed', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--dependencies', type=Path, help='Directory containing the locked app_build dependencies')
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    base = {}
    for relative in ('team_relay.py', 'mcp_server.py', 'dist/app.html'):
        path = args.installed / relative
        base[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    server_bytes = (args.installed / 'mcp_server.py').read_bytes()
    if hashlib.sha256(server_bytes).hexdigest() != base['mcp_server.py']:
        raise RuntimeError('The installed MCP server changed while staging.')
    server = upgrade((PACKAGE / 'mcp_server.py').read_text(encoding='utf-8-sig'))
    compile(server, 'mcp_server.py', 'exec')
    (destination / 'mcp_server.py').write_text(server, encoding='utf-8', newline='\n')
    copyfile(SOURCE / 'team_relay.py', destination / 'team_relay.py')
    copyfile(args.installed / 'relay_config.json', destination / 'relay_config.json')
    for name in ('plugin.json', 'mcp_config.json'):
        copyfile(PACKAGE / name, destination / name)
    build = ['node', str(SOURCE / 'build.mjs'), str(args.installed), str(destination)]
    if args.dependencies:
        build.append(str(args.dependencies))
    subprocess.run(build, check=True)
    (destination / 'base.json').write_text(json.dumps(base), encoding='utf-8')
