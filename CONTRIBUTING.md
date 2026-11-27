# Contributing to Agent Team Hub

Thank you for your interest in improving **Agent Team Hub**! This project aims to stay lightweight, local-first, and easy to inspect without heavy runtime dependencies.

## Architecture Overview

- `hub.py`: CLI entrypoint, `HubServer`, `HubHandler` (auth, static whitelist, thin delegation), and `TaskRunner`.
- `server/`: Internal backend modules (`config_utils.py`, `global_access.py`, `http_utils.py`, `store.py`, `routes_control.py`, `routes_clients.py`). Keep `server/__init__.py` empty and avoid reverse-importing `hub.py`.
- `doc_engine/`: Stateless text/diff/clause-similarity helpers (`text_ops.py`) and Git single-file commit / `.ai` inspection (`git_ops.py`).
- `docs_store.py`, `teams.py`, `messages.py`, `workspace.py`, `codex_native.py`, `claude_worker.py`, `project_agent.py`: Core domain modules and client Hook/MCP adapters.
- `console/`: Zero-build native browser ES Modules (`console.js` + `modules/{core,members,messages,workspace,docs_diff,docs_studio}.js`). Every served file must be explicitly registered in `CONSOLE_FILES` inside `hub.py` (never expose `console/package.json`).
- `integrations/antigravity/` & `probes/`: Antigravity / AGY CLI plugin source and Claude Desktop inbox bridge.
- `tests/`: Domain-split isolated unit test suites (`test_teams_and_messages.py`, `test_clients_and_hooks.py`, `test_docs_and_worktrees.py`).

## Design Principles

1. **Zero external Python dependencies**: Stick to Python 3.11+ standard library and SQLite for the Hub backend.
2. **Zero frontend build chain**: Keep the console runnable directly via native browser ES Modules.
3. **Preserve external entrypoints**: Do not rename or break `hub.py`, `project_agent.py`, `probes/desktop_inbox.py`, or existing public exports used by deployed client configs.
4. **Never inject real model names into agents**: Model notes are human-facing Console labels only; avoid injecting or exposing them via Hooks or agent tools.
5. **Isolate tests from production data**: Tests must use temporary directories and ephemeral loopback ports (`127.0.0.1:0`).

## Local Verification Before Opening a PR

```bash
# 1. Run the full isolated unit test suite
python -m unittest discover -s tests -v

# 2. Check every console ES module individually (Node 20+ ignores extra args in a single call)
python -c "import pathlib, subprocess, sys
for f in [pathlib.Path('console/console.js')] + sorted(pathlib.Path('console/modules').glob('*.js')):
    rc = subprocess.run(['node', '--check', str(f)]).returncode
    if rc != 0:
        sys.exit(rc)"

# 3. Verify external CWD module imports & root anchors
python tests/check_imports.py
```

On Linux/macOS, also validate shell scripts if modified:

```bash
for f in scripts/*.sh; do sh -n "$f"; done
```

## Licensing

By contributing to Agent Team Hub, you agree that your contributions will be licensed under the **Apache License 2.0** (see [LICENSE](LICENSE) and [NOTICE](NOTICE)).
