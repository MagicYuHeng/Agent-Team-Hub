<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Agent Team Hub — 本机多产品 AI 编程原会话协作路由与管理台">
</p>

**Agent Team Hub** 是一个运行在本机（默认 `127.0.0.1:58767`）的零云端中转多 Agent 协作中心。它通过本机 Hub 转发机制，连接 **Antigravity App**、**AGY CLI**、**AGY ACP（自定义本地宿主）**、**Claude Code Desktop** 与 **Codex Desktop** 的原生会话，让不同产品的模型在各自原生窗口中互发消息、离线排队收件，并按需提供动态角色分工与多工作树 `AGENTS.md` 规范治理。

<p align="center">
  <img src="assets/readme/architecture.svg" width="100%" alt="分工 Team 与无分工 Team 两种协作模式对比">
</p>

---

## 核心特性

- **原生会话接入与持久收件队列**：每个成员绑定一条原生会话标识，消息经本机 Hub 转发并持久化在本地 SQLite 队列，离线消息安全暂存、重连后按序送达并记录回执。
- **两类团队模式**：
  - **分工 Team (`structured`)**：绑定单一项目工作区，支持成员独立职位（`captain` / `worker` / `reviewer` / `helper` / `reviewer+helper`）、负责工作树、按版本增量 Hook 注入（版本未变时静默跳过以保护前缀缓存），以及三栏式 `AGENTS.md` 与角色规范工作台（漂移对比、跨树查类似替换/移除、批量添加、按树单文件安全提交）。
  - **无分工 Team (`peer`)**：不绑定工作区、零团队身份注入，所有成员平级沟通，仅保留可选短备注与成员变动轻提醒，适合多个独立项目联合开发。
- **零外部后端依赖**：服务端完全基于 Python 3.11+ 标准库与本机 SQLite，管理台为原生静态网页（内置浅色暖纸 / 深色石墨一键切换）。

---

## 快速部署

### 1. 环境要求与已验证平台

- **Python 3.11+**（需已加入系统 `PATH`，或使用自定义解释器启动）
- **Git**（仅「分工 Team」多工作树发现与单文件提交需要）
- **Node.js 20+**（仅在从源码构建 Antigravity 插件 `dist/app.html` 时需要，由锁定 MCP 依赖包声明）
- **已验证平台**：当前完整实机端到端验证均在 **Windows 11 + PowerShell 7** 下完成（Linux / macOS 可运行核心 Python 服务端与通用 HTTP/MCP 通道，但 Claude Desktop 命名管道推送目前仅面向 Windows 桌面端）。

### 2. 启动本机 Team Hub

克隆仓库后，在项目根目录执行：

**Windows (PowerShell 7 推荐)**：

```powershell
# 一键后台启动（首次运行会自动生成并写入用户级 TEAM_HUB_TOKEN，默认端口 58767）
.\Start-Hub.ps1

# 查看运行状态 / 停止服务（支持自定义 -Port）
.\Status-Hub.ps1
.\Stop-Hub.ps1

# 自动携带本机令牌打开管理台（支持自定义 -Port）
.\Open-Console.ps1
```

**跨平台 / 手动启动**：

```bash
# 1. 设置长度 >= 32 位的本机访问令牌（建议写入用户环境变量，设置后重启各客户端使其继承）
export TEAM_HUB_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# 2. 启动 Hub（默认监听 127.0.0.1:58767，数据保存在 .team-hub/team.db）
python hub.py --workspace ./fixtures
```

> 如使用自定义端口（例如 `--port 58800`），需确保各客户端 MCP / Hook 配置与 `Open-Console.ps1 -Port 58800` 使用同一端口。

启动后访问 `http://127.0.0.1:58767/manage`，粘贴 `TEAM_HUB_TOKEN` 解锁即可按首启引导创建第一个 Team。

---

## 简短使用方式

1. **创建 Team**：首次解锁后选择「创建分工 Team」（指定项目总工作区路径）或「创建无分工 Team」（无需路径，跨项目平级联调）。
2. **接入客户端**：
   - **分工 Team**：进入「工作区」页点击 **部署 Hook**，Hub 会自动向总工作区及全部 Git worktree 合并写入客户端配置，并通过 `.git/info/exclude` 忽略未跟踪托管文件（非 Git 目录、已被 Git 追踪的文件或只读目录需手动处理）。
   - **无分工 Team**：在「成员」页底部点击 **配置全局接入**，一键写入用户级 Claude / Codex MCP 与 Codex 零注入验票钩子（不改动任何独立项目目录）。
3. **新增成员并绑定原会话**：点击 **新增成员**，选择对应的客户端产品，保存后点 **绑定** 获取配对码或绑定指引，在目标原聊天完成一次绑定即可开始用 `team_send`、`team_members`、`team_whoami` 互发消息。

> [!IMPORTANT]
> **Codex Hook 手动审批提醒**：无论是分工 Team 的项目级 `.codex/hooks.json`，还是无分工 Team 的用户级 `~/.codex/hooks.json`，在首次部署完成后，**必须在 Codex Desktop 设置（Hooks / 钩子管理）中手动审批（信任）一次对应的 Team Hub 钩子**，否则 Codex 调用 `team_send` / `team_members` 时会提示未获取会话凭据。

---

## 一键安装 Prompt（直接发给对应产品的 Agent）

克隆本仓库后，把下方对应产品的提示词中的 `<仓库绝对路径>` 替换为你的本机路径，直接发送给该产品的 Agent 即可自动完成安装或接入准备。

<details open>
<summary><strong>1. Antigravity App / AGY CLI（共用同一份插件，含首次安装与升级）</strong></summary>

```text
请帮我安装或升级本机 Agent Team Hub 的 Antigravity 插件（Antigravity App 与 AGY CLI 共用同一份插件，无需重复安装）：
1. 确认用户级环境变量 TEAM_HUB_TOKEN 已存在（长度 >= 32 位）；如缺失请提醒我先运行 <仓库绝对路径>/Start-Hub.ps1 自动生成。
2. 确认本机 Node.js 版本 >= 20，进入 <仓库绝对路径>/integrations/antigravity/package/app_build 运行 npm ci。
3. 使用 Python 构建候选目录（输出到 <仓库绝对路径>/.team-hub/ag-candidate）：
   - 如果是首次全新安装（~/.gemini/config/plugins/mcp-aux-relay-plugin 尚不存在）：
     先运行 python "<仓库绝对路径>/integrations/antigravity/stage.py" "<仓库绝对路径>/integrations/antigravity/package" "<仓库绝对路径>/.team-hub/ag-candidate"
     然后将生成的候选目录完整复制到 ~/.gemini/config/plugins/mcp-aux-relay-plugin，并确保其中 mcp_config.json 的 Python 命令在本机可执行。
   - 如果是升级已有安装：
     先运行 python "<仓库绝对路径>/integrations/antigravity/stage.py" "~/.gemini/config/plugins/mcp-aux-relay-plugin" "<仓库绝对路径>/.team-hub/ag-candidate"
     再运行 python "<仓库绝对路径>/integrations/antigravity/install.py" "<仓库绝对路径>/.team-hub/ag-candidate" "~/.gemini/config/plugins/mcp-aux-relay-plugin" "<仓库绝对路径>/.team-hub/ag-backup" 完成备份与覆盖。
4. 完成后提醒我在 Antigravity 的 Customizations → Installed 中启用或重启 relay MCP 服务（AGY CLI 重启会话即可生效）。
```

</details>

<details>
<summary><strong>2. Claude Code Desktop（支持跨项目全局 MCP 接入）</strong></summary>

```text
请帮我为本机 Claude Code Desktop 配置 Agent Team Hub 跨项目全局 MCP（仅添加 MCP 工具，不注入任何全局 Hook）：
1. 确认用户级环境变量 TEAM_HUB_TOKEN 已存在（与 Team Hub 使用的令牌一致）；如缺失请提醒我先运行 <仓库绝对路径>/Start-Hub.ps1 自动生成。
2. 优先方式：直接在 Team Hub 管理台的无分工 Team 页面点击「配置全局接入」。
   如需手动配置，请先安全备份 ~/.claude.json，再在顶层 mcpServers 中合并添加名为 "team-hub" 的 stdio MCP 配置（保留已有其他服务）：
   - type: "stdio"
   - command: 本机 Python 3.11+ 可执行文件绝对路径
   - args: ["<仓库绝对路径>/probes/desktop_inbox.py", "58767"]
3. 完成后提醒我重启 Claude Code Desktop，并在目标原会话中执行管理台生成的 bind_team_member(...) 绑定指令。
```

</details>

<details>
<summary><strong>3. Codex Desktop（支持跨项目全局 MCP + 零注入验票钩子）</strong></summary>

```text
请帮我为本机 Codex Desktop 配置 Agent Team Hub 跨项目全局接入（仅提供通信 MCP 与 PreToolUse 验票，不注入团队身份）：
1. 确认用户级环境变量 TEAM_HUB_TOKEN 已存在，且本机已安装支持 `codex queue` 命令的 Codex CLI。
2. 优先方式：直接在 Team Hub 管理台的无分工 Team 页面点击「配置全局接入」。
   如需手动配置，请先安全备份 ~/.codex/config.toml 与 ~/.codex/hooks.json：
   - 在 ~/.codex/config.toml 末尾合并受管段：
     # BEGIN Team Hub project MCP
     [mcp_servers.team_hub]
     command = "<本机 Python 3.11+ 绝对路径>"
     args = ["<仓库绝对路径>/project_agent.py", "mcp", "58767"]
     # END Team Hub project MCP
   - 在 ~/.codex/hooks.json 的 hooks.PreToolUse 中合并一条匹配 "^mcp__team_hub__team_(send|members|whoami)$"、timeout 为 4 的命令钩子（路径含空格时须加双引号）：
     "\"<本机 Python 3.11+ 绝对路径>\" \"<仓库绝对路径>/project_agent.py\" hook codex global 58767"
     不要添加 SessionStart 或 UserPromptSubmit 钩子。
3. 完成后务必提醒我：重启 Codex Desktop 后，必须在 Codex 设置的 Hooks 列表里手动审批/信任一次该 Team Hub 钩子，才能正常调用 team_send 与 team_members。
```

</details>

<details>
<summary><strong>4. AGY ACP（自定义本地 ACP 宿主接入）</strong></summary>

```text
请帮我把当前自定义 ACP 本地宿主接入本机 Agent Team Hub（http://127.0.0.1:58767，Bearer TEAM_HUB_TOKEN 鉴权）：
1. 阅读 <仓库绝对路径>/hub.py 中的 acp_request 路由实现，对接以下接口：
   - POST /acp/bind：传入持久 session_id、instance 与管理台生成的 10 位 code（及可选 label），保存返回的 route 与动态 mcp 启动描述
   - POST /acp/restore：启动时传入持久 session_id 与当前 instance，自动恢复绑定并获取最新 mcp 描述
   - POST /acp/poll：支持单条 route 或批量 routes 数组（最多 50 条）轮询待送达消息并续活心跳
   - POST /acp/received：消息注入会话后回执确认
   - POST /acp/unbind：主动解绑
   - POST /acp/context：仅分工 Team（mode === "structured"）时按版本增量拉取角色上下文
2. 在启动或重载 ACP 会话时，将 /acp/bind 或 /acp/restore 返回的 mcp 字段直接挂载到会话的 mcpServers 中（无需硬编码本机路径）。
```

</details>

---

## 客户端接入能力与前置条件

| 客户端产品 | 跨项目支持 | 身份恢复方式 | 收件唤醒机制与前置条件 | 分工 Team 角色注入 |
| :--- | :---: | :--- | :--- | :--- |
| **Antigravity App** | 原生支持 | 原生聊天 ID 自动恢复 | Aux Relay 侧栏推送（需打开侧栏） | `.agents/hooks.json` (`PreInvocation`) |
| **AGY CLI** | 原生支持 | `team_cli_restore()` 自动恢复 | 后台 `wait-inbox` 任务唤醒 + Hook 兜底 | `.agents/hooks.json` (`PreInvocation`) |
| **AGY ACP (自定义宿主)** | 原生支持 | 持久 `session_id` + `instance` 恢复 | 宿主批量 `/acp/poll` 空闲注入 | `/acp/context` 按版本增量注入 |
| **Claude Code Desktop** | 用户级全局 MCP | `CLAUDE_CODE_SESSION_ID` 自动恢复 | Windows 命名管道推送（实验性本地集成） | `.claude/settings.local.json` (`UserPromptSubmit`) |
| **Codex Desktop** | 用户级全局 MCP + 验票 | 原生线程 ID 持久绑定 | 本机 `codex queue` 排队投递（忙时排入下一轮，不中途打断） | `.codex/hooks.json` (`UserPromptSubmit`) |

---

## 安全边界与兼容性说明

- **本机信任边界**：Hub 主服务固定绑定 `127.0.0.1`（默认 `58767`），除 `/health` 与管理台静态资源外均要求 `Authorization: Bearer <TEAM_HUB_TOKEN>`。当前共享的 `TEAM_HUB_TOKEN` 是本机全权信任边界，持有该令牌的本机插件与集成脚本均处在同一信任域内，不提供多租户权限隔离。
- **插件旧版辅助端口**：Antigravity 插件中的旧版辅助 HTTP 端口（`58766`）默认已关闭；仅当显式设置 `RELAY_ENABLE_LEGACY_HTTP=1` 时才在 `127.0.0.1` 启动，并强制校验 `Bearer TEAM_HUB_TOKEN` 与 64 KiB 请求体上限。
- **本地存储**：所有团队配置、成员绑定、受管文档历史与消息队列均明文保存在本机 `.team-hub/team.db`（SQLite）中，请勿将 `.team-hub/` 提交至版本库。
- **实验性客户端集成说明**：Antigravity 插件/ACP、Codex Hook 与 `codex queue` 基于各客户端现有本地能力；Claude Code Desktop 的 Windows 命名管道环境变量、Codex 本地会话文件位置及部分宿主会话元数据属于未承诺长期稳定的实验性本地集成机制，客户端版本升级时可能需要同步适配。

---

## 开发与测试

```bash
# 运行全部自动化单元测试（隔离临时数据库与本机回环端口，不影响生产数据）
python -m unittest discover -s tests -v

# 检查管理台前端语法
node --check console/console.js
```

---

## 许可与声明

本项目以 **[Apache License 2.0](LICENSE)** 开源（版权声明见 [NOTICE](NOTICE)）。本仓库仅分发源码与依赖锁文件，不包含预构建的第三方打包成品。

文中提及的 Antigravity、Claude Code、Codex 等名称及商标归其各自所有者（Google、Anthropic、OpenAI 等）所有；本项目为独立社区工具，与上述厂商无官方关联或背书。
