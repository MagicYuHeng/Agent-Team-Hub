# Changelog

All notable changes to **Agent Team Hub** are documented in this file.

## [Unreleased]

### Added
- **Codex in-turn Hook soft-steering (`PostToolUse` + `Stop`)**: While a bound Codex session is actively running and its Hooks are observed, incoming team messages are handed off into the active turn via `PostToolUse` (`0.45s` internal single-HTTP timeout, normal `【来自 <member>】` wording) with `Stop` hook fallback for tool-free turns, falling back to `codex queue` when the thread is idle. Both project-level (`HOOK_SCHEMA_VERSION = 5`) and user-level global Codex hooks are supported.
- **Delivery status semantics & cancel safety**: Kept Codex Hook and `codex queue` handoffs truthfully as `accepted` (reserving `delivered` for channels with explicit client acknowledgement, without heuristic rollout text-matching). Only advances `notice_revision` / `roster.seen_revision` after Hook HTTP transmission succeeds (HTTP write errors settle as `unconfirmed` without consuming org-change reminders). Introduced `cancel_requested` so cancel requests during an in-flight `codex queue` call settle truthfully as `accepted` or `canceled`, and switched `CodexRunner` to a per-member 4-worker concurrency pool (additional slow deliveries queue when all 4 workers are busy).
- **Cross-platform launch scripts**: Added Windows `.cmd` wrappers (`Start-Hub.cmd`, `Stop-Hub.cmd`, `Status-Hub.cmd`, `Open-Console.cmd`) and POSIX Shell scripts (`scripts/start-hub.sh`, `scripts/stop-hub.sh`, `scripts/status-hub.sh`, `scripts/open-console.sh`) with per-port PID files and checkout-entry verification.
- **Open-source CI & governance**: Added GitHub Actions CI workflow (`.github/workflows/ci.yml` for `ubuntu-latest` + `windows-latest`), bilingual documentation (`README.md` + `README.en.md`), `SECURITY.md`, `CONTRIBUTING.md`, client compatibility matrix, Issue templates, `.gitattributes`, and sanitized Web Console screenshots.

### Changed
- **Modularized core codebase**: Split `docs_store.py` into `doc_engine/{text_ops,git_ops}.py`, split `hub.py` and `workspace.py` into `server/{config_utils,global_access,http_utils,store,routes_control,routes_clients}.py`, split `console/console.js` into native ES Modules under `console/modules/`, and split the unit test suite by domain under `tests/`.
- **Relaxed default collaboration tone & no model-name injection**: Removed real model-name injection from Hooks, `team_members`, and `team_whoami` (keeping model notes Console-only); updated default role templates in `roles/*.md` to a natural peer collaboration style.

## [0.1.0] - 2026-11-27

### Added
- Initial Apache-2.0 open-source release of **Agent Team Hub**:
  - Local-first loopback router (`127.0.0.1:58767`) using Python 3.11+ standard library and SQLite.
  - Native session integrations for **Antigravity App**, **AGY CLI**, **AGY ACP**, **Claude Code Desktop**, and **Codex Desktop**.
  - **Structured Team** (`structured`) and **Peer Team** (`peer`) modes, plus user-level global cross-project access.
  - Three-column Docs Studio for `AGENTS.md` multi-worktree drift sync, clause similarity search/replace/remove, batch append, revision rollback, and single-file safe Git commits.
  - Security hardening: legacy plugin HTTP port `58766` disabled by default (opt-in loopback + Bearer auth), PID verification in `Stop-Hub.ps1`, strict static whitelist & CSP.
