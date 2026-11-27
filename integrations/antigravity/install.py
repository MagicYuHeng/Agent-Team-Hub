"""Back up and install a staged upgrade into the existing Aux Relay plugin."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from shutil import copy2
import uuid


FILES = (Path('team_relay.py'), Path('mcp_server.py'), Path('dist/app.html'))


def replace_bytes(target, content):
    staged = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        staged.write_bytes(content)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('installed', type=Path)
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    if args.installed.name != 'mcp-aux-relay-plugin' or not (args.installed / 'plugin.json').is_file():
        parser.error('Expected the previously installed Aux Relay plugin.')
    payloads = {file: (args.candidate / file).read_bytes() for file in FILES}
    base = json.loads((args.candidate / 'base.json').read_text(encoding='utf-8'))
    for file in FILES:
        target = args.installed / file
        digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        if base[file.as_posix()] != digest:
            parser.error(f'The installed file changed during validation: {file}')
    compile(payloads[Path('mcp_server.py')], 'mcp_server.py', 'exec')
    if b'bind_team_window' not in payloads[Path('dist/app.html')]:
        parser.error('The staged app does not contain the scoped binding tool.')
    args.backup.mkdir(parents=True)
    for file in FILES:
        source = args.installed / file
        if source.exists():
            saved = args.backup / file
            saved.parent.mkdir(parents=True, exist_ok=True)
            copy2(source, saved)
    changed = []
    try:
        for file in FILES:
            replace_bytes(args.installed / file, payloads[file])
            changed.append(file)
        for file in FILES:
            assert hashlib.sha256((args.installed / file).read_bytes()).digest() == hashlib.sha256(payloads[file]).digest()
    except (OSError, AssertionError):
        for file in reversed(changed):
            saved = args.backup / file
            if saved.is_file():
                replace_bytes(args.installed / file, saved.read_bytes())
            else:
                (args.installed / file).unlink(missing_ok=True)
        raise
    print(f'Installed {len(FILES)} files. Backup: {args.backup.resolve()}')
