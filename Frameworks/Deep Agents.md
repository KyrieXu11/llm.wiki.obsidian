---
tags:
  - frameworks
  - agents
  - langchain
created: "2026-04-07"
aliases:
  - DeepAgents
  - deepagents
url: "https://github.com/langchain-ai/deepagents"
---

# Deep Agents

## 简介

Deep Agents 是 LangChain 推出的开源 Agent 框架，基于 [[LangGraph]] 构建，灵感来自 Claude Code。它是一个通用的 coding agent harness，支持任意具备 tool calling 能力的 LLM，内置规划工具、文件系统后端和子 Agent 派生能力。

**核心设计理念**：信任 LLM 的判断力，通过工具层和沙箱层实施安全边界，而非依赖模型自我约束。

- GitHub: [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)（Python）
- GitHub: [langchain-ai/deepagentsjs](https://github.com/langchain-ai/deepagentsjs)（TypeScript）
- License: MIT

## 安装

```bash
pip install deepagents
```

## 核心概念

### 沙箱后端协议（Sandbox Backend Protocol）

Deep Agents 的沙箱系统采用可插拔架构，类层次为：

```
BackendProtocol (ABC)           ← 文件操作接口
  └── SandboxBackendProtocol    ← 添加 execute() + id
        └── BaseSandbox (ABC)   ← 默认实现（ls/read/write/edit/grep/glob）
              ├── ModalSandbox
              ├── DaytonaSandbox
              ├── RunloopSandbox
              └── OpenSandboxBackend（自定义）
```

实现一个新的沙箱后端只需继承 `BaseSandbox` 并实现 **4 个抽象成员**：

| 成员                 | 签名                                                            | 说明          |
| ------------------ | ------------------------------------------------------------- | ----------- |
| `id`               | `@property → str`                                             | 沙箱唯一标识      |
| `execute()`        | `(command, *, timeout) → ExecuteResponse`                     | 执行 shell 命令 |
| `upload_files()`   | `(files: list[tuple[str, bytes]]) → list[FileUploadResponse]` | 上传文件        |
| `download_files()` | `(paths: list[str]) → list[FileDownloadResponse]`             | 下载文件        |

其余高层操作（`ls`, `read`, `write`, `edit`, `grep`, `glob`）由 `BaseSandbox` 通过组合 `execute()` 和 `upload_files()` 自动提供。

### 关键数据类型

定义于 `deepagents.backends.protocol`：

- **`ExecuteResponse`**: `output: str`, `exit_code: int | None`, `truncated: bool`
- **`FileUploadResponse`**: `path: str`, `error: FileOperationError | None`
- **`FileDownloadResponse`**: `path: str`, `content: bytes | None`, `error: FileOperationError | None`
- **`FileOperationError`**: `Literal["file_not_found" | "permission_denied" | "is_directory" | "invalid_path"]`

### CLI 沙箱工厂

Deep Agents CLI 通过 `sandbox_factory.py` 管理沙箱生命周期：

- `SandboxProvider`（ABC）：实现 `get_or_create()` 和 `delete()` 方法
- `_PROVIDER_TO_WORKING_DIR`：注册沙箱默认工作目录
- `verify_sandbox_deps()`：验证后端依赖是否可用

### Agent 与沙箱的两种连接模式

1. **Agent 在沙箱内运行**：镜像本地开发环境，Agent 直接访问文件系统
2. **Agent 在沙箱外运行**：沙箱作为工具调用，API key 留在安全侧

### 官方支持的沙箱提供商

| 提供商 | 特点 |
|--------|------|
| AgentCore (AWS) | MicroVM 隔离，Code Interpreter |
| Modal | ML/AI 工作负载，GPU 访问 |
| Daytona | 快速冷启动，TS/Python 开发 |
| Runloop | 一次性 devbox，隔离代码执行 |
| [[OpenSandbox 沙箱后端集成\|OpenSandbox]]（社区） | Docker/K8s 运行时，多语言支持 |

## 项目结构

```
libs/
  deepagents/          ← 核心库（协议、BaseSandbox）
  partners/
    modal/             ← Modal 后端
    daytona/           ← Daytona 后端
    runloop/           ← Runloop 后端
  cli/                 ← CLI 工具（sandbox_factory、provider）
```

## 优缺点

| 优点 | 缺点 |
|------|------|
| LLM 无关，支持任意模型 | 依赖 LangChain/LangGraph 生态 |
| 沙箱协议简洁，4 方法即可接入 | 文档尚不完善（截至 2026-04） |
| 内置规划、子 Agent、上下文管理 | 同步阻塞 API，无原生 async 支持 |
| 生产就绪：流式、持久化、检查点 | 社区后端需手动注册到 CLI |

## 参考资料

- 官方文档: [docs.langchain.com/deepagents](https://docs.langchain.com/oss/python/deepagents/)
- 沙箱集成: [Execute Code with Sandboxes for Deep Agents](https://blog.langchain.com/execute-code-with-sandboxes-for-deepagents/)
- 两种沙箱模式: [The two patterns by which agents connect sandboxes](https://blog.langchain.com/the-two-patterns-by-which-agents-connect-sandboxes/)
- [[Agents MOC|Agent 与工具]]
- [[OpenSandbox 沙箱后端集成]]
