---
tags:
  - agents
  - sandbox
  - integration
  - deepagents
created: "2026-04-07"
aliases:
  - langchain-opensandbox
  - OpenSandbox Deep Agents Backend
url: "https://github.com/alibaba/OpenSandbox"
---

# OpenSandbox 沙箱后端集成

## 概述

本文记录为 [[Deep Agents]]（LangChain Agent 框架）实现 [[opensandbox-deploy-guide|OpenSandbox]] 沙箱后端的技术方案。OpenSandbox 是阿里巴巴开源的 AI 沙箱平台，Deep Agents 是 LangChain 的 coding agent 框架。二者之前没有官方集成，本后端 `langchain-opensandbox` 填补了这一空白。

## 架构设计

### 接口映射

Deep Agents 的 `BaseSandbox` 要求实现 4 个抽象方法，与 OpenSandbox SDK 的映射关系：

| Deep Agents 接口                                           | OpenSandbox SDK 调用                        |
| -------------------------------------------------------- | ----------------------------------------- |
| `id` → `str`                                             | `SandboxSync.id`                          |
| `execute(cmd)` → `ExecuteResponse`                       | `sandbox.commands.run(cmd, opts=...)`     |
| `upload_files([(path, bytes)])` → `[FileUploadResponse]` | `sandbox.files.write_file(path, content)` |
| `download_files([path])` → `[FileDownloadResponse]`      | `sandbox.files.read_bytes(path)`          |

### 关键设计决策

1. **使用同步 SDK（`SandboxSync`）**：Deep Agents 的 `BaseSandbox.execute()` 是同步方法，因此使用 OpenSandbox 的同步客户端而非 async 版本。

2. **stdout + stderr 合并输出**：遵循 Runloop/Modal 等现有后端的惯例，将 `Execution.logs.stdout` 和 `Execution.logs.stderr` 合并为单一 `output` 字符串。

3. **错误映射**：OpenSandbox 的 `SandboxApiException` 通过 HTTP status code 和错误消息映射到 Deep Agents 的 `FileOperationError` 枚举（`file_not_found`, `permission_denied`, `is_directory`, `invalid_path`）。

4. **生命周期由调用方管理**：`OpenSandboxBackend` 不在析构时自动 kill 沙箱，遵循 OpenSandbox SDK 的设计——`close()` 仅释放本地 HTTP 资源，`kill()` 才终止远程沙箱。

## 使用方式

### 编程接口

```python
from langchain_opensandbox import OpenSandboxBackend

# 创建沙箱
backend = OpenSandboxBackend.create(
    image="opensandbox/code-interpreter:v1.0.2",
    domain="localhost:8080",
    working_directory="/workspace",
)

# 执行命令
result = backend.execute("echo hello")
# result.output == "hello", result.exit_code == 0

# 文件操作
backend.upload_files([("/workspace/app.py", b"print('hi')")])
result = backend.execute("python /workspace/app.py")

# 清理
backend.kill()
backend.close()
```

### 连接已有沙箱

```python
backend = OpenSandboxBackend.connect("sandbox-id", domain="localhost:8080")
```

### CLI 集成

注册到 Deep Agents CLI 需修改 `sandbox_factory.py` 中的三处：

1. `_PROVIDER_TO_WORKING_DIR` 添加 `"opensandbox": "/home/user"`
2. `_get_provider()` 添加 `OpenSandboxProvider` 分支
3. `verify_sandbox_deps()` 添加 `"opensandbox": ("langchain_opensandbox", "opensandbox")`

### 环境变量

| 变量                      | 说明               | 默认值                                   |
| ----------------------- | ---------------- | ------------------------------------- |
| `OPEN_SANDBOX_API_KEY`  | API 认证密钥         | —                                     |
| `OPEN_SANDBOX_DOMAIN`   | 服务器地址            | `localhost:8080`                      |
| `OPEN_SANDBOX_PROTOCOL` | `http` / `https` | `http`                                |
| `OPEN_SANDBOX_IMAGE`    | 沙箱 Docker 镜像     | `opensandbox/code-interpreter:v1.0.2` |
| `OPEN_SANDBOX_TIMEOUT`  | 沙箱 TTL（分钟）       | `30`                                  |

## 项目结构

```
langchain-opensandbox/
├── langchain_opensandbox/
│   ├── __init__.py              # 导出 OpenSandboxBackend
│   ├── sandbox.py               # BaseSandbox 实现（核心）
│   └── provider.py              # CLI SandboxProvider 实现
├── tests/
│   ├── test_sandbox.py          # 17 个单元测试（mock）
│   └── test_integration.py      # 21 个集成测试（真实 OpenSandbox）
├── pyproject.toml
└── README.md
```

## 源码

完整实现代码位于 `coding/langchain-opensandbox/` 目录：

| 文件 | 说明 |
|------|------|
| [[coding/langchain-opensandbox/sandbox.py]] | `OpenSandboxBackend` — BaseSandbox 实现（核心），含工厂方法、4 个抽象方法、生命周期管理、错误映射 |
| [[coding/langchain-opensandbox/provider.py]] | `OpenSandboxProvider` — CLI 生命周期管理，通过环境变量配置 |
| [[coding/langchain-opensandbox/test_sandbox.py]] | 17 个单元测试（mock OpenSandbox SDK，无需真实服务） |
| [[coding/langchain-opensandbox/test_integration.py]] | 21 个集成测试（对真实 minikube OpenSandbox 全流程验证） |
| [[coding/langchain-opensandbox/pyproject.toml]] | 包配置，依赖 `deepagents>=0.4.3` + `opensandbox>=0.2.0` |

## 测试

### 单元测试（无需服务）

```bash
cd langchain-opensandbox
python -m pytest tests/test_sandbox.py -v    # 17 passed
```

通过 MagicMock 模拟 OpenSandbox SDK，验证适配器逻辑：执行命令、stdout/stderr 合并、错误映射、文件上传下载、生命周期管理。

### 集成测试（需要 OpenSandbox 服务）

```bash
# 1. 确保 minikube OpenSandbox 已部署且端口转发已建立
kubectl port-forward svc/opensandbox-server 8080:80 -n opensandbox-system &

# 2. 运行集成测试
python -m pytest tests/test_integration.py -v    # 21 passed
```

集成测试对真实 OpenSandbox 验证完整链路：

- **生命周期**：REST API 创建沙箱 → SDK connect → 多连接复用 → 销毁
- **命令执行**：echo、exit code、stderr、多行输出、Python 执行、环境变量、工作目录
- **文件上传**：单文件、多文件、二进制、上传脚本并执行
- **文件下载**：存在/不存在文件、多文件、upload-download 往返一致性
- **端到端工作流**：上传代码 → 执行 → 下载结果 → 验证 JSON

> **注意**：K8s 部署中 SDK 无法直连沙箱 Pod 的 execd 端口，集成测试使用 `use_server_proxy=True` 通过 server 代理路由。沙箱创建/销毁绕过 SDK（存在 metadata=null 解析 bug），直接调用 REST API。

## OpenSandbox SDK 核心 API 备忘

### 命令执行

```python
from opensandbox.models.execd import RunCommandOpts

result = sandbox.commands.run("cmd", opts=RunCommandOpts(
    timeout=timedelta(seconds=60),
    working_directory="/workspace",
    background=True,          # 后台执行
    envs={"KEY": "val"},      # 额外环境变量
))
# result.exit_code, result.logs.stdout, result.logs.stderr, result.text
```

### 文件操作

```python
sandbox.files.write_file("path", "content")        # 写文件
content = sandbox.files.read_file("path")           # 读文件（str）
raw = sandbox.files.read_bytes("path")              # 读文件（bytes）
results = sandbox.files.search(SearchEntry(...))    # 搜索文件
sandbox.files.replace_contents([ContentReplaceEntry(...)])  # 原地替换
```

### 生命周期

```python
sandbox = SandboxSync.create("image", timeout=timedelta(minutes=30), ...)
sandbox = SandboxSync.connect("sandbox-id", connection_config=config)
sandbox.pause()   # 暂停（可恢复）
sandbox.renew(timedelta(minutes=30))  # 续期
sandbox.kill()    # 终止（不可逆）
sandbox.close()   # 释放本地资源
```

## 实践笔记

- OpenSandbox 的 execd 守护进程不需要认证，认证仅在生命周期管理 API 层
- `use_server_proxy=True` 可在 SDK 无法直连沙箱容器时，通过 server 代理路由 execd 调用
- 命令执行通过 SSE（Server-Sent Events）流式返回，同步 SDK 内部会阻塞等待完成
- Deep Agents 的所有现有后端都设置 30 分钟默认超时

## 参考资料

- [[Deep Agents]] — 框架详情与沙箱协议
- [[opensandbox-deploy-guide|OpenSandbox 部署指南]] — 本地部署与配置
- [OpenSandbox GitHub](https://github.com/alibaba/OpenSandbox)
- [Deep Agents GitHub](https://github.com/langchain-ai/deepagents)
- [LangGraph 与 OpenSandbox 集成示例](https://github.com/alibaba/OpenSandbox/tree/main/examples/langgraph)
