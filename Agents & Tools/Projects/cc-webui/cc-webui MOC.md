---
aliases:
  - cc-webui
  - Web Code
tags:
  - MOC
  - cc-webui
  - projects
created: "2026-04-23"
---

# cc-webui

Self-hosted Claude Code web UI。基于 `@anthropic-ai/claude-agent-sdk`，把 Claude Code CLI 的能力迁到浏览器：多会话 / 项目切换 / 自定义 Bash MCP / 后台任务 / 实时 SSE / 断线续流 / 运行时干预。

仓库：`github.com/KyrieXu11/cc-webui`

## 运行时架构

- [[Claude Agent SDK 自定义 Bash MCP]] — 自定义 Bash MCP server 替换内置 Bash / BashOutput / KillBash；后台任务注册表；rolling 输出；session 隔离；进程生命周期清理；两条 SSE 流（list + per-task output）；ZodError 陷阱
- [[Claude Agent SDK 流式对话断线续流]] — `InFlightChat` + fanout + attach 端点；`clientTurnId` + localStorage 暂存 pending prompt；`applySDKMessage` 幂等（正确的 id 源是 `stream_message_id`，不是 `msg.uuid`）；409 `session_busy` / `turn_busy` 互斥
- [[Claude Agent SDK 运行时干预：取消与前后台互转]] — Stop 按钮（iterator.return 出 for-await）+ Ctrl+B detach 前台 bash 到后台（proc 移交 + Promise 手动 resolve）+ step 视觉区分"等待审批" vs "执行中"

## 相关

- [[Claude Code]]
- [[MCP (Model Context Protocol)]]
- [[Tool Use 工具调用]]
