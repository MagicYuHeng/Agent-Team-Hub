"""Verify external-CWD module imports and REPO_ROOT path anchors across import orders."""

import pathlib
import subprocess
import sys


def main():
    repo = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
    orders = [
        'hub',
        'workspace, docs_store, hub',
        'doc_engine.text_ops, doc_engine.git_ops, server.config_utils, server.global_access, '
        'server.http_utils, server.store, server.routes_control, server.routes_clients, hub',
    ]
    for seq in orders:
        code = (
            f'import sys, pathlib; sys.path.insert(0, {str(repo)!r}); '
            f'import {seq}; import hub, workspace; '
            f'assert workspace.SOURCE == pathlib.Path({str(repo)!r}); '
            f"assert workspace.AGENT == pathlib.Path({str(repo)!r}) / 'project_agent.py'; "
            f'assert hub.TaskStore is not None and callable(hub.required_text)'
        )
        res = subprocess.run(
            [sys.executable, '-c', code],
            cwd=str(repo.parent),
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            print('FAILED:', seq, res.stderr, file=sys.stderr)
            sys.exit(res.returncode)
        print('OK:', seq)


if __name__ == '__main__':
    main()
