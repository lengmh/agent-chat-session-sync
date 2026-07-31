# agent-chat-session-sync

[![CI](https://github.com/Sanshix/agent-chat-session-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/Sanshix/agent-chat-session-sync/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Sanshix/agent-chat-session-sync?include_prereleases)](https://github.com/Sanshix/agent-chat-session-sync/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)

把本地已经启动的 Codex / Claude Code 会话接入飞书：每个 Agent 会话自动创建一个专属群，并让飞书回复继续运行同一个本地会话。

> **English summary:** A durable, local-first session orchestration layer for
> **Codex Desktop / Codex CLI / Claude Code + Feishu (Lark) + cc-connect**.
> It automatically creates one chat per local Agent session, attaches the chat to
> an existing Codex rollout or Claude session, and resumes that exact session when
> a user replies from Feishu.

**发布状态：v0.5.0-alpha.1 · Experimental Alpha · macOS 自动安装 · Python 3.11+ · cc-connect v1.4.1 pinned patch set**

## 你是否需要它

如果你的工作流是下面这样，这个项目就是为你准备的：

```text
先在 Codex Desktop、Codex CLI 或 Claude Code 本地开始任务
                         ↓
自动创建「用户 + Bot」专属飞书群
                         ↓
本地 prompt / Assistant 最终回复同步到群
                         ↓
从飞书继续同一个 rollout / session，而不是另开会话
```

| 需求 | 本项目 |
|---|---|
| 飞书先发消息，再创建 Agent 会话 | 直接使用 cc-connect 原生能力 |
| 本地会话先启动，再自动创建飞书群 | 支持 |
| 一个本地任务一个独立群 | 支持 |
| 绑定已经存在的 Codex rollout / Claude session | 支持 |
| Codex Desktop 和飞书轮流更新同一会话 | 支持，发送前检测外部 rollout 更新并 resume |
| Hook 失败后不丢消息 | 支持，SQLite inbox/outbox + emergency spool |
| Slack、Telegram 或 Windows 自动安装 | 当前未实现 |

不适合以下场景：只想从飞书发起新会话、希望一个群通过 thread 容纳多个会话，或者不愿维护 patched cc-connect。前两种场景优先直接使用
[`cc-connect`](https://github.com/chenhg5/cc-connect)。

## 让 AI 帮你判断和安装

把仓库 URL 和下面这段提示交给 Codex、Claude Code 或其他代码 Agent。它会先检查环境，不应直接覆盖你正在运行的服务：

```text
请审查 agent-chat-session-sync 仓库，并在这台 macOS 机器上安装它。
目标：把本地 Codex 和 Claude Code 已有会话同步到专属飞书群，飞书回复必须恢复同一个原生 session。

执行前请先：
1. 检查 Python、Go、Codex、Claude Code、cc-connect、飞书权限和当前后台进程；
2. 确认 cc-connect 与仓库锁定的 revision/patch 兼容；
3. 备份现有 cc-connect 二进制与 Hook 配置；
4. 不输出或提交 app_secret、token、open_id、chat_id、rollout 内容；
5. 构建 patched cc-connect，运行 Python/Go 测试，再安装 Hook 和 LaunchAgent；
6. 用 doctor 证明 Socket 权限和运行版本；
7. 分别执行 Codex、Claude Code 的 acceptance-live，不能用 --skip-reply 代替双向验收。

遇到缺权限、版本不匹配或现有服务来源不明时停止并向我说明，不要猜测部署成功。
```

AI 搜索或提问时可使用这些自然语言关键词：

- “Codex Desktop session sync to Feishu / Lark”
- “attach an existing Codex rollout to cc-connect”
- “one Feishu group per local Codex task”
- “resume Claude Code session from Feishu”
- “Codex App Server Feishu bridge”
- “local-first AI agent chat session orchestration”

## 与 cc-connect 的边界

本项目不是新的消息桥接器。飞书长连接、消息协议、Agent 子进程和会话恢复仍由
[`cc-connect`](https://github.com/chenhg5/cc-connect) 负责；本项目只提供它缺少的本地会话生命周期编排层。

| cc-connect 负责 | 本项目负责 |
|---|---|
| 飞书 WebSocket、Bot 收发、卡片和附件 | 监听本地 Agent Hook 并可靠入队 |
| 启动/恢复 Codex 与 Claude Code | 解析既有 rollout/session 的稳定身份 |
| 平台 session 和 Agent session 持久化 | 每个本地会话自动建群、绑定、改名和自愈 |
| 飞书入站消息路由 | 外部已有 session attach、binding replay、幂等 outbox |

项目不会复制飞书凭据；运行时只读取本机已有的 cc-connect 配置。

## 工作原理

```text
本地 Codex / Claude Code Hook
      │  只做原始事件持久化（SQLite 失败时 fsync emergency spool）
      ▼
SQLite inbox → agent-chat-session-sync worker
      ├── 可等待的 rollout 身份解析状态机
      ├── 创建或恢复专属飞书群
      ├── 绑定既有 rollout 到 cc-connect session
      └── SQLite outbox + 平台幂等键
      ▼
cc-connect ↔ 飞书 → delivered + platform message ID
```

## 快速开始（macOS）

这不是单独执行一次 `pip install` 就能工作的纯 Python 工具。完整安装包含 Python daemon 和 pinned cc-connect patch 两部分。

```bash
# 1. 克隆本仓库并进入目录

# 2. 构建经过锁定和测试的 cc-connect
./scripts/build-cc-connect.sh

# 3. 按下文配置 ~/.cc-connect/config.toml 和 ~/.codex/config.toml
#    然后显式备份、停止旧 cc-connect，再部署 dist/cc-connect

# 4. 安装 Python worker、Codex/Claude Hooks 和 macOS LaunchAgent
./scripts/install.sh

# 5. 检查配置、Socket、权限和运行版本
~/.local/share/agent-chat-session-sync/venv/bin/agent-chat-session-sync doctor

# 6. 做真实双向验收；按提示在两个测试群分别回复 token
~/.local/share/agent-chat-session-sync/venv/bin/agent-chat-session-sync acceptance-live --agent codex --timeout 900
~/.local/share/agent-chat-session-sync/venv/bin/agent-chat-session-sync acceptance-live --agent claudecode --timeout 900
```

`build-cc-connect.sh` 只产生二进制，不会替你覆盖或重启现有服务。这个限制是刻意的：部署路径和服务管理方式必须由本机管理员明确确认。

### 最小飞书权限

飞书应用至少需要满足 cc-connect 原生收发消息的权限，以及本项目的建群、读群能力。完整 acceptance 清理还需要：

- `im:chat:create`
- `im:chat:read`
- `im:chat:delete` 或 `im:chat`
- cc-connect 收发消息所需的消息权限

若不授予删除权限，双向消息仍可工作，但验收程序无法自动解散测试群。

## 当前能力

- 一个本地 Codex 任务对应一个独立飞书群。
- 群名固定带 Agent 标识：`Codex · 任务名` 或 `Claude · 任务名`。
- Hook 只负责可靠收件；解析、建群和发送由常驻 worker 异步执行。
- SQLite inbox/outbox 支持失败重试、重启续传、历史补发和幂等去重。
- rollout 延迟创建或 transcript 暂时缺失时保留事件；多候选进入 `waiting_confirmation`，不猜测绑定。
- 群名读取 Codex `session_index.jsonl` 中的真实任务标题；若标题晚于建群生成，会在后续 Hook 自动改名。
- 将新群绑定到已经存在的 Codex rollout，而非新建 Agent 会话。
- 同步本地用户消息和最终回复。
- 使用 `CC_SESSION_KEY` 避免飞书发起的 Codex 再次触发镜像循环。
- 从 `transcript_path` 解析稳定 rollout ID，兼容 Codex Desktop 上下文压缩后的临时 session ID。
- 群被解散或 Bot 被移除后，在下一次 Hook 事件中自动重建并重发。
- 原子状态写入、进程级文件锁和幂等 Hook 安装。
- 不把飞书凭据复制到自己的配置；直接读取现有 `cc-connect` 项目配置。
- Codex 原生 permission profile：按 cwd 发现并验证 profile，禁止和 legacy sandbox 同时发送；不可用时失败关闭。
- Codex App Server 生命周期：飞书入站通过 `thread/resume`、`turn/start` 和实时 `thread/turn/item` 事件工作，不直接追加 rollout JSONL。
- 飞书每次复用 Codex 进程前检查 rollout offset 与最新 turn ID；发现 Desktop 等其他客户端追加新 turn 时，先关闭旧进程并 resume 同一 rollout，再处理消息。
- 支持 `stdio` 独立生命周期，以及通过 `codex app-server proxy` 连接持久 daemon 的共享生命周期。
- `doctor` 校验服务 UID、Unix Socket 类型/owner/mode/group/父目录和 App Server 配置一致性。

`0.5.0-alpha.1` 实现 `Codex + 飞书` 与 `Claude Code + 飞书`，并依赖 cc-connect 的 Unix Socket API。当前自动安装和常驻
worker 服务只支持 macOS LaunchAgent；Linux 上的核心代码可以手工运行，但在 systemd 安装器完成前不属于正式支持范围。
同一个飞书 Bot 同时服务 Codex 与 Claude Code 时，两个项目必须启用 `binding_routing = true`；worker 会在 cc-connect
启动或 Socket 重建后，从 SQLite 重放 binding，使每个动态创建的群只进入所属 Agent engine。

## 架构

```text
src/agent_chat_session_sync/
├── runtime.py               # Hook 收件、版本身份日志、emergency spool
├── queue.py                 # SQLite inbox/outbox/bindings 和幂等键
├── resolver.py              # 可等待的 Codex 会话身份状态机
├── worker.py                # 重试、绑定和投递的常驻进程
├── agents/codex.py          # Hook 事件语义、文本和防回环
├── platforms/feishu.py      # 建群、校验和发送
├── bridges/cc_connect.py    # 外部既有 Agent session attach
├── coordinator.py           # 映射生命周期和故障自愈
├── acceptance.py            # 真实 Codex ↔ 飞书验收编排
├── installer.py             # Hook 与 worker 服务安装/卸载
└── provenance.py            # 源码/构建包/Hook import 版本证明
```

各层保持独立：`ClaudeCodeAdapter` 不包含飞书逻辑，平台适配器也不解析 Codex rollout 或 Claude transcript。

## 前置条件

1. Python 3.11 或更高版本。
2. Codex Desktop/CLI 或 Claude Code 已能产生本地会话文件。
3. `cc-connect v1.4.1` 已配置对应的 Codex/Claude Code + 飞书项目。
4. 飞书应用具备建群、读群和发消息权限；正式 release 验收还需要 `im:chat:delete`（或 `im:chat`）用于自动解散测试群。
   `allow_from` 至少包含一个具体 `open_id`，不能只有 `*`。
5. cc-connect 已应用本仓库的 `/sessions/bind-agent` 扩展。

示例 `~/.cc-connect/config.toml`（只展示相关结构）：

```toml
[[projects]]
name = "local-codex"
mode = "multi-workspace"
base_dir = "/"
workspace_init_allow_local_paths = true

[projects.agent]
type = "codex"

[projects.agent.options]
mode = "auto-edit"
backend = "app_server"
app_server_lifecycle = "stdio"
app_server_url = "stdio://"
permission_profile = "cc-connect-workspace"

[[projects.platforms]]
type = "feishu"

[projects.platforms.options]
app_id = "cli_xxx"
app_secret = "从本机安全配置提供，不要提交到仓库"
allow_from = "ou_xxx"
group_reply_all = true
binding_routing = true
```

复用同一个 Bot 的 Claude Code 项目使用相同的飞书凭据，并单独配置：

```toml
[[projects]]
name = "local-claude"
mode = "multi-workspace"
base_dir = "/"
workspace_init_allow_local_paths = true

[projects.agent]
type = "claudecode"

[projects.agent.options]
mode = "auto"

[[projects.platforms]]
type = "feishu"

[projects.platforms.options]
app_id = "cli_xxx"
app_secret = "从本机安全配置提供，不要提交到仓库"
allow_from = "ou_xxx"
group_reply_all = true
binding_routing = true
```

本地优先同步建议使用 cc-connect 的 `multi-workspace` 模式。若需要同步本机任意目录的新会话，
将 `base_dir` 设为 `/`；Hook 会匹配所有绝对 cwd，
attach 时把群持久绑定到实际会话 cwd，从而让同一个飞书 Bot 安全服务多个本地仓库。
单工作区配置仍按 `work_dir` 匹配；多个覆盖目录嵌套时使用最具体的匹配项。

### Codex 权限 profile

推荐使用一个不改变 Desktop 默认权限的专用 profile，并由 cc-connect 在 `thread/start` / `thread/resume`
时显式选择：

```toml
# ~/.codex/config.toml
[permissions.cc-connect-workspace]
description = "Remote turns may edit only the active workspace."
extends = ":workspace"

[permissions.cc-connect-workspace.network]
enabled = false
```

`cc-connect` 会先以当前任务 cwd 调用 `permissionProfile/list`。profile 不存在或被 managed requirements
禁用时，会拒绝启动该远程会话；不会回退到 `danger-full-access`。配置了 `permission_profile` 后，协议层不会再同时发送
legacy `sandbox` / `approvalPolicy` 覆盖值。

Unix Socket 有两层含义：cc-connect 自己的 API Socket 负责 Hook attach；共享 App Server Socket 负责 daemon 生命周期。
这两个 Socket 都必须由服务 UID 拥有，只允许 `0600`，或由明确服务组持有的 `0660`，并且父目录不能 world-writable。
Codex permission profile 内的 `network.unix_sockets` 则只控制 Agent 执行的沙箱命令是否能访问某个 Socket，
不要用 `dangerously_allow_all_unix_sockets = true` 代替精确 allowlist。

### App Server 生命周期模式

```text
stdio（默认可部署）
飞书 → cc-connect → codex app-server --stdio
                    → thread/resume → turn/start → 实时事件

daemon（共享生命周期）
飞书 → cc-connect → codex app-server proxy → App Server control socket
                                          → 已加载 thread + 实时事件
```

启用 daemon：

```toml
[projects.agent.options]
backend = "app_server"
app_server_lifecycle = "daemon"
app_server_socket = "/absolute/path/to/app-server-control.sock" # 可省略，使用 Codex 默认值
permission_profile = "cc-connect-workspace"
```

daemon 模式要求官方 managed standalone Codex 安装和正在运行的 App Server daemon。连接或 profile 验证失败时不会
静默降级到 exec/rollout 路径。当前公开协议不能让第二个客户端接入 Desktop 私有 stdio 子进程；要让 Desktop UI 与飞书
观察完全相同的进程内实时流，Desktop/CLI 也必须连接同一个公开 daemon。项目不依赖或逆向 Desktop 私有 IPC。

## 安装

开发安装：

```bash
python3 -m pip install -e .
agent-chat-session-sync install-hooks
agent-chat-session-sync doctor
```

macOS 普通用户安装可执行：

```bash
./scripts/install.sh
```

安装脚本使用独立 venv（默认 `~/.local/share/agent-chat-session-sync/venv`），不会修改系统 Python。
它会拒绝从 dirty worktree 部署，并强制验证“源码 HEAD = wheel 构建 commit = Hook 实际 import commit”。
这只证明已安装代码的身份；真实双向验收未通过时，不能宣布发布成功。

`install-hooks` 会保留 `~/.codex/hooks.json` 中不属于本项目的 Hook，并替换旧版
`codex_lark_sync.py` 项，避免两个实现重复发送。重复执行是幂等的。

如果路径不是默认值，可使用环境变量：

| 变量 | 默认值 |
|---|---|
| `ACSS_DATA_DIR` | `~/.local/share/agent-chat-session-sync` |
| `CC_CONNECT_CONFIG` | `~/.cc-connect/config.toml` |
| `CC_CONNECT_SOCKET` | `~/.cc-connect/run/api.sock` |
| `CODEX_HOME` | `~/.codex` |
| `CLAUDE_HOME` | `~/.claude` |

本机 data directory 权限会设为 `0700`，SQLite、日志、spool 和 worker lock 会设为 `0600`。
`status` 不显示 chat ID 或用户 open ID：

```bash
agent-chat-session-sync status
agent-chat-session-sync events --limit 50
agent-chat-session-sync resolve EVENT_ID ROLLOUT_ID
agent-chat-session-sync retry EVENT_ID
agent-chat-session-sync uninstall-hooks
```

`events` 会显示持久化状态和歧义候选。人工核对 rollout 后用 `resolve`
确认；修复 Socket/飞书等外部故障后用 `retry` 立即补发。

## 构建带扩展的 cc-connect

仓库内保留了针对上游 revision `5d4c96dd12774574369e75b60084140101c9a59a`
（release/v1.4.1）的可重放补丁：

```bash
./scripts/build-cc-connect.sh
```

脚本会克隆固定 revision、先执行 `git apply --check`、运行 core、Codex、Claude Code 和飞书 Go 测试，然后以
`no_web goolm` tags 构建到 `dist/cc-connect`。它不会自动覆盖正在运行的 cc-connect；请先停止服务，备份原二进制，再显式部署构建产物。

正式 release 必须从 clean worktree 构建并附带校验和：

```bash
python3 -m pip install build
./scripts/build-release.sh
shasum -a 256 -c dist/SHA256SUMS
```

release 包会验证 wheel 不含构建机绝对路径，并验证 sdist 包含 `patches/`、`scripts/` 和 `docs/`。
GitHub Release 还提供经过同一 patch/test gate 构建的 macOS arm64 `cc-connect` 二进制；下载后应先用
`SHA256SUMS` 校验，并显式 `chmod +x`。其他平台请从源码构建，不要使用架构不匹配的二进制。

扩展接口为本机 mode-0600 Unix Socket 上的：

```http
POST /sessions/bind-agent
Content-Type: application/json

{
  "project": "my-project",
  "session_key": "feishu:oc_xxx:ou_xxx",
  "session_id": "existing-codex-rollout-id",
  "session_name": "Codex · project · 12345678",
  "work_dir": "/absolute/path/to/the/local/session/workspace"
}
```

处理器会在 adapter 支持 `SessionIDValidator` 时先验证 session 属于该项目，再调用 cc-connect 已有的
`SwitchToAgentSession`。详细设计见 [docs/CC_CONNECT_EXTENSION.md](docs/CC_CONNECT_EXTENSION.md)。

## 从旧的本机脚本迁移

1. 安装本项目，但先不要删除旧目录。
2. 运行 `agent-chat-session-sync install-hooks`，它会替换旧 Hook 命令。
3. 如需保留既有群映射，将旧 `state.json` 手动复制到新的 `ACSS_DATA_DIR`；必须同时保留本机 rollout 文件。
4. 运行 `agent-chat-session-sync doctor`，再启动一个测试会话验证双向消息。
5. 确认后再移除旧脚本。不要复制 `sync.log`、lock 文件或任何凭据。

默认建议不迁移历史 state，让新设备从新会话开始。仅有 thread ID 而没有对应 rollout 文件无法恢复会话。

若从旧版单体脚本升级到多工作区模式，可安全合并并重新 attach 现有映射：

```bash
agent-chat-session-sync migrate-state \
  --from ~/.local/share/cc-connect-session-sync/state.json
```

## 开发与测试

```bash
python3 -m pip install -e '.[dev]'
python3 -m unittest discover -s tests
```

Hook 遇到错误始终返回成功，避免阻断 Codex 回合；每次收件日志都包含
`service_version`、`git_commit`、`package_path`、`python_path` 和 `config_path`。
正式发布需要依次通过：

```bash
./scripts/install.sh
agent-chat-session-sync acceptance-live --agent codex --timeout 900
agent-chat-session-sync acceptance-live --agent claudecode --timeout 900
```

`acceptance-live` 默认要求在测试群发送指定 reply token，以证明飞书入站恢复了同一 rollout。
`--skip-reply` 只是单向链路诊断，不算完整验收。详见 [docs/RELIABILITY.md](docs/RELIABILITY.md)
和 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)。

## 安全边界

- 不提交 `~/.cc-connect/config.toml`、`state.json`、日志、token 或 `app_secret`。
- attach API 只应暴露在 cc-connect 权限为 `0600` 的本机 Unix Socket 上。
- permission profile 与 Socket ACL 是独立安全边界：前者约束 Agent 命令，后者约束哪个部署身份能连接服务。
- App Server daemon 模式只连接公开的 Codex control socket，不连接 Desktop 私有 IPC。
- 项目匹配和 `SessionIDValidator` 共同防止跨项目恢复错误会话。
- 本项目当前使用 `allow_from` 的第一个具体用户作为群主和成员；多用户选主策略属于后续功能。
