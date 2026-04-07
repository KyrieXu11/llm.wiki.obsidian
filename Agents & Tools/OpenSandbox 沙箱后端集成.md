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
│   ├── __init__.py         # 导出 OpenSandboxBackend
│   ├── sandbox.py          # BaseSandbox 实现（核心）
│   └── provider.py         # CLI SandboxProvider 实现
├── tests/
│   └── test_sandbox.py     # 17 个单元测试（全部通过）
├── pyproject.toml
└── README.md
```

## 核心实现代码

### `sandbox.py` — BaseSandbox 实现

完整的 `OpenSandboxBackend` 类，继承 `BaseSandbox`，通过 OpenSandbox 同步 SDK 代理所有操作：

```python
from deepagents.backends.protocol import (
    ExecuteResponse, FileDownloadResponse, FileOperationError, FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.exceptions import SandboxApiException, SandboxException
from opensandbox.models.execd import RunCommandOpts
from opensandbox.sync.sandbox import SandboxSync

_DEFAULT_TIMEOUT = 30 * 60  # 30 分钟，与其他后端一致


class OpenSandboxBackend(BaseSandbox):

    def __init__(self, sandbox: SandboxSync, *, default_timeout=_DEFAULT_TIMEOUT,
                 working_directory: str | None = None):
        self._sandbox = sandbox
        self._default_timeout = default_timeout
        self._working_directory = working_directory

    # -- 工厂方法 --

    @classmethod
    def create(cls, image="opensandbox/code-interpreter:v1.0.2", *,
               timeout=timedelta(minutes=30), api_key=None, domain=None,
               protocol=None, entrypoint=None, resource=None, env=None,
               default_timeout=_DEFAULT_TIMEOUT, working_directory=None,
               **sandbox_kwargs):
        config_kwargs = {}
        if api_key:   config_kwargs["api_key"] = api_key
        if domain:    config_kwargs["domain"] = domain
        if protocol:  config_kwargs["protocol"] = protocol
        config = ConnectionConfigSync(**config_kwargs)

        create_kwargs = {"timeout": timeout, "connection_config": config}
        if entrypoint: create_kwargs["entrypoint"] = entrypoint
        if resource:   create_kwargs["resource"] = resource
        if env:        create_kwargs["env"] = env
        create_kwargs.update(sandbox_kwargs)

        sandbox = SandboxSync.create(image, **create_kwargs)
        return cls(sandbox, default_timeout=default_timeout,
                   working_directory=working_directory)

    @classmethod
    def connect(cls, sandbox_id, *, api_key=None, domain=None, protocol=None,
                default_timeout=_DEFAULT_TIMEOUT, working_directory=None):
        config_kwargs = {}
        if api_key:   config_kwargs["api_key"] = api_key
        if domain:    config_kwargs["domain"] = domain
        if protocol:  config_kwargs["protocol"] = protocol
        config = ConnectionConfigSync(**config_kwargs)
        sandbox = SandboxSync.connect(sandbox_id, connection_config=config)
        return cls(sandbox, default_timeout=default_timeout,
                   working_directory=working_directory)

    # -- 4 个必须实现的抽象方法 --

    @property
    def id(self) -> str:
        return self._sandbox.id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        effective_timeout = timeout if timeout is not None else self._default_timeout
        opts = RunCommandOpts(timeout=timedelta(seconds=effective_timeout))
        if self._working_directory:
            opts.working_directory = self._working_directory

        try:
            result = self._sandbox.commands.run(command, opts=opts)
        except SandboxException as exc:
            return ExecuteResponse(output=str(exc), exit_code=1, truncated=False)

        # 合并 stdout + stderr（遵循 Runloop/Modal 等现有后端惯例）
        stdout_text = "\n".join(m.text.rstrip("\n") for m in result.logs.stdout)
        stderr_text = "\n".join(m.text.rstrip("\n") for m in result.logs.stderr)
        parts = [p for p in (stdout_text, stderr_text) if p]
        return ExecuteResponse(
            output="\n".join(parts), exit_code=result.exit_code, truncated=False,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses = []
        for path, content in files:
            try:
                self._sandbox.files.write_file(path, content)
                responses.append(FileUploadResponse(path=path, error=None))
            except SandboxApiException as exc:
                responses.append(FileUploadResponse(path=path, error=_map_api_error(exc)))
            except SandboxException:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = []
        for path in paths:
            try:
                content = self._sandbox.files.read_bytes(path)
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            except SandboxApiException as exc:
                responses.append(FileDownloadResponse(
                    path=path, content=None, error=_map_api_error(exc)))
            except SandboxException:
                responses.append(FileDownloadResponse(
                    path=path, content=None, error="file_not_found"))
        return responses

    # -- 生命周期 --

    def kill(self):
        self._sandbox.kill()

    def close(self):
        self._sandbox.close()

    def __enter__(self):  return self
    def __exit__(self, *_): self.close()


def _map_api_error(exc: SandboxApiException) -> FileOperationError:
    """将 OpenSandbox HTTP 错误映射到 Deep Agents 错误码"""
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if status == 404 or "not found" in msg:       return "file_not_found"
    if status == 403 or "permission" in msg:       return "permission_denied"
    if "is a directory" in msg:                    return "is_directory"
    if "invalid" in msg:                           return "invalid_path"
    return "file_not_found"
```

### `provider.py` — CLI 生命周期管理

Deep Agents CLI 通过 `SandboxProvider` 管理沙箱的创建和销毁：

```python
import os
from datetime import timedelta
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.sync.sandbox import SandboxSync

_DEFAULT_IMAGE = "opensandbox/code-interpreter:v1.0.2"
_DEFAULT_ENTRYPOINT = ["/opt/opensandbox/code-interpreter.sh"]


class OpenSandboxProvider:
    """通过环境变量配置，管理 OpenSandbox 实例生命周期"""

    def __init__(self):
        kwargs = {}
        if key := os.environ.get("OPEN_SANDBOX_API_KEY"):  kwargs["api_key"] = key
        if dom := os.environ.get("OPEN_SANDBOX_DOMAIN"):    kwargs["domain"] = dom
        if proto := os.environ.get("OPEN_SANDBOX_PROTOCOL"):kwargs["protocol"] = proto
        self._config = ConnectionConfigSync(**kwargs)

    def get_or_create(self, sandbox_id: str | None = None) -> SandboxSync:
        if sandbox_id:
            return SandboxSync.connect(sandbox_id, connection_config=self._config)

        image = os.environ.get("OPEN_SANDBOX_IMAGE", _DEFAULT_IMAGE)
        timeout_min = int(os.environ.get("OPEN_SANDBOX_TIMEOUT", "30"))
        entrypoint_env = os.environ.get("OPEN_SANDBOX_ENTRYPOINT")
        entrypoint = entrypoint_env.split(",") if entrypoint_env else _DEFAULT_ENTRYPOINT

        return SandboxSync.create(
            image, entrypoint=entrypoint,
            timeout=timedelta(minutes=timeout_min),
            connection_config=self._config,
        )

    def delete(self, sandbox_id: str):
        sandbox = SandboxSync.connect(sandbox_id, connection_config=self._config)
        sandbox.kill()
        sandbox.close()
```

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
