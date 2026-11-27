# Security & Privacy Policy

English | 中文见下方

## Trust Boundary & Network Model

- **Loopback-only Hub routing**: Agent Team Hub's own HTTP server binds exclusively to `127.0.0.1` (default port `58767`) and makes zero outbound cloud requests for Hub message routing or console serving. Note that once a message is injected into a native client (Antigravity, Claude Code, Codex, etc.), that host processes it under its own model/network policy; additionally, if legacy queued rows exist in the historical `tasks` table, `TaskRunner` (`hub.py` / `claude_worker.py`) may invoke the local `claude` CLI.
- **Single-tenant local bearer token (`TEAM_HUB_TOKEN`)**: Every API endpoint except `GET /health` and the explicit static console whitelist (`CONSOLE_FILES`) requires `Authorization: Bearer <TEAM_HUB_TOKEN>` (minimum 32 printable ASCII characters). All local clients and plugins holding `TEAM_HUB_TOKEN` operate inside the same local full-trust boundary; per-member route tokens and Codex tool tickets identify sessions and prevent spoofing between sessions, but do not isolate administrative privileges across untrusted local OS users.
- **Static console hardening**: The web console is served only from an explicit file whitelist (`CONSOLE_FILES` in `hub.py`) with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and `Content-Security-Policy: default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`. Non-whitelisted files (including `console/package.json`) are never served.
- **Antigravity plugin legacy HTTP port (`58766`)**: Disabled by default. Only enabled if `RELAY_ENABLE_LEGACY_HTTP=1` is explicitly set, in which case it binds to `127.0.0.1` with mandatory `Bearer TEAM_HUB_TOKEN` authentication and a 64 KiB body cap.

## Local Data Storage

- All teams, member bindings, message queues, and managed document revisions are stored **in plaintext** inside local SQLite (`.team-hub/team.db`).
- Do **not** commit `.team-hub/` or `.env` files into any Git repository.

## Files Agent Team Hub May Create, Modify, or Restore

Agent Team Hub writes, updates, or restores local files during explicit management actions (**Deploy/Sync/Remove Hooks**, **Configure/Remove Global Access**, **Archive Team**, **Docs Studio save/sync/rollback/reset**, and **plugin installation/upgrade**):

| Scope | Path | Triggering Operations & Behavior |
| :--- | :--- | :--- |
| Hub runtime | `<repo>/.team-hub/` | Created by launch scripts / `hub.py`: SQLite DB, per-port PID/log files, and optional `.env` (Unix `chmod 600`) |
| Structured Team workspace (per worktree) | `.team-hub/project.json`, `.claude/settings.local.json`, `.mcp.json`, `.codex/hooks.json`, `.codex/config.toml`, `.agents/hooks.json`, `.git/info/exclude` | Written/updated on **Deploy / Sync Hooks**; restored/cleaned on **Remove Hooks** or **Archive Team** |
| Structured Team role overrides | `<workspace>/.team-hub/roles/*.md` | Created/updated on Docs Studio **Save / Rollback** for role rules; deleted on **Reset to Default** |
| Project `AGENTS.md` (opt-in only) | `<worktree>/AGENTS.md` | Modified only when you explicitly click **Save**, **Sync**, **Batch Append/Replace/Remove**, or **Rollback** in Docs Studio (never deleted by Remove Hooks or Archive Team) |
| Global cross-project access (opt-in) | `~/.claude.json`, `~/.codex/config.toml`, `~/.codex/hooks.json` | Merged on **Configure Global Access** (backed up once to `*.before-team-hub.bak`; never injects `SessionStart` or `UserPromptSubmit`); managed blocks removed on **Remove Global Access** |
| Antigravity / AGY CLI plugin (opt-in) | `~/.gemini/config/plugins/mcp-aux-relay-plugin/` | Installed/upgraded via `stage.py` / `install.py` (directory name retained for historical compatibility) |

## Uninstalling & Cleanup

1. Stop the Hub (`Stop-Hub.ps1` / `Stop-Hub.cmd` / `scripts/stop-hub.sh`).
2. For each structured team, click **Remove Hooks** in the Workspace tab before archiving (restores backed-up managed files and cleans `.git/info/exclude`).
3. If you enabled global cross-project access, click **Remove Global Access** in a Peer team (or restore the `*.before-team-hub.bak` files).
4. Optionally remove `~/.gemini/config/plugins/mcp-aux-relay-plugin/`, `<repo>/.team-hub/`, and the `TEAM_HUB_TOKEN` environment variable.

## Reporting a Vulnerability

Do **not** post unpatched vulnerabilities or secret tokens in public issues. If **GitHub Private Vulnerability Reporting** is enabled on this repository's Security tab, please submit a private report there; otherwise contact the repository maintainer privately through GitHub profile channels before public disclosure. Include reproduction steps, affected OS/client versions, and impact.

---

## 安全与隐私说明（中文）

- **纯本机回环路由**：Hub 自身 HTTP 服务仅绑定 `127.0.0.1`（默认端口 `58767`），路由转发与管理台服务本身不向云端上传数据。消息注入各原生客户端后遵循对应客户端自身的网络与模型策略；若历史 `tasks` 表中存在旧版派单任务，`TaskRunner`（`hub.py` / `claude_worker.py`）仍可能调用本机 `claude` CLI。
- **本机全权信任域**：除 `/health` 与管理台显式白名单静态资源外，所有接口均要求 `Bearer TEAM_HUB_TOKEN`；持有该令牌的本机插件与脚本同处一个本机信任边界，不提供多用户租户隔离。Antigravity 插件的旧版 `58766` 辅助 HTTP 端口默认已关闭。
- **本地明文存储**：团队配置、消息队列与受管文档历史明文存放在 `.team-hub/team.db`，切勿将 `.team-hub/` 提交到版本库。
- **受管文件与完整卸载**：上述表格列出了部署/同步/撤销 Hook、配置/移除全局接入、归档 Team、Docs Studio 保存/回滚/恢复默认及插件安装升级涉及的全部文件；撤销 Hook、移除全局接入并删除 `.team-hub/` 与插件目录即可干净卸载，撤销 Hook 或归档 Team 绝不会删除你的 `AGENTS.md`。
- **漏洞反馈**：切勿在公开 Issue 中披露未修复的漏洞或泄露令牌。若仓库 Security 页已启用 **GitHub Private Vulnerability Reporting**，请优先通过该私有渠道提交；否则请先私下联系仓库维护者协调修复后再公开。
