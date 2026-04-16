---
tags:
  - agents
  - sandbox
  - architecture
  - opensandbox
created: "2026-04-09"
aliases:
  - OpenSandbox Architecture
  - OpenSandbox 内部架构
url: "https://github.com/alibaba/OpenSandbox"
---

# OpenSandbox 架构

## 概述

OpenSandbox 是阿里巴巴开源的 AI 沙箱平台（Apache 2.0，已进入 CNCF Landscape），为 LLM Agent 提供安全的代码执行环境。本文深入分析其内部架构，重点解读各组件的职责和协作方式。

## 四层架构

```
┌─────────────────────────────────────────────────────┐
│  SDK Layer       Python / TypeScript / Java / C#    │
│                  开发者面向的客户端库                    │
└────────────────────────┬────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────┐
│  Specs Layer     sandbox-lifecycle.yml (生命周期 API) │
│                  execd-api.yaml (执行 API)           │
│                  两套 OpenAPI 规范定义所有契约            │
└────────────────────────┬────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────┐
│  Runtime Layer   Server (Python/FastAPI, :8080)      │
│                  Ingress / Egress (Go)               │
│                  K8s CRD Controller                   │
│                  沙箱编排、网络路由、生命周期管理           │
└────────────────────────┬────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────┐
│  Sandbox Layer   用户镜像 + 注入的 execd 守护进程        │
│                  每个沙箱是一个独立容器实例               │
└─────────────────────────────────────────────────────┘
```

## 核心组件

| 组件                 | 位置                    | 语言               | 职责                                               |
| ------------------ | --------------------- | ---------------- | ------------------------------------------------ |
| **Server**         | `server/`             | Python (FastAPI) | 沙箱生命周期管理（创建、暂停、恢复、销毁、过期）                         |
| **execd**          | `components/execd/`   | Go (Gin)         | 沙箱内执行守护进程——命令、代码、文件、指标                           |
| **Ingress**        | `components/ingress/` | Go               | K8s 模式下的 HTTP/WebSocket 反向代理，路由请求到沙箱实例           |
| **Egress**         | `components/egress/`  | Go               | 基于 FQDN 的出站网络控制（DNS 代理 + nftables）               |
| **SDKs**           | `sdks/`               | 多语言              | 客户端库：Sandbox、Filesystem、Commands、CodeInterpreter |
| **K8s Controller** | `kubernetes/`         | Go               | BatchSandbox CRD 控制器，支持池化和批量创建                   |

## execd —— 沙箱内的执行守护进程

### 是什么

execd 是一个 **Go HTTP 服务**（基于 Gin 框架），被注入到每个沙箱容器中，监听 **端口 44772**。它是外部世界与沙箱内部交互的 **唯一入口**——所有命令执行、代码运行、文件操作、终端交互都通过 execd 完成。

### 注入过程

沙箱容器启动前，Server 通过 Docker API 注入 execd。核心代码位于 [`server/opensandbox_server/services/docker.py`](https://github.com/alibaba/OpenSandbox/blob/main/server/opensandbox_server/services/docker.py)，涉及以下方法：

| 方法 | 职责 |
|------|------|
| `create_sandbox()` → `_provision_sandbox()` | 创建容器，调用注入流程，覆盖 entrypoint |
| `_prepare_sandbox_runtime()` | 组合调用下面两个方法 |
| `_fetch_execd_archive()` | 创建临时容器从 execd 镜像中 `get_archive("/execd")` 提取二进制 tar 包，按平台缓存 |
| `_copy_execd_to_container()` | 通过 `container.put_archive()` 将 tar 包注入到 `/opt/opensandbox/` |
| `_install_bootstrap_script()` | 生成 bootstrap.sh，通过 `put_archive()` 注入，权限 0o755 |

完整流程：

```
create_sandbox()
  → _provision_sandbox()
      1. docker.create_container(image, entrypoint="/opt/opensandbox/bootstrap.sh")
         （容器处于 stopped 状态）
      2. _prepare_sandbox_runtime(container):
         a. _fetch_execd_archive():
            创建临时容器 → container.get_archive("/execd") → 缓存 tar 包
         b. _copy_execd_to_container():
            container.put_archive("/opt/opensandbox", tar_data)
         c. _install_bootstrap_script():
            生成脚本 → container.put_archive("/opt/opensandbox", script_tar)
      3. container.start()
      4. 轮询 execd /ping 直到健康
```

生成的 bootstrap.sh 内容：

```bash
#!/bin/sh
set -e
/opt/opensandbox/execd >/tmp/execd.log 2>&1 &   # 后台启动 execd
exec "$@"                                         # 运行用户 entrypoint
```

execd 守护进程二进制本身位于 [`components/execd/`](https://github.com/alibaba/OpenSandbox/tree/main/components/execd)（Go 源码），入口点为 [`main.go`](https://github.com/alibaba/OpenSandbox/blob/main/components/execd/main.go)。

### API 全景

| 分组 | 端点 | 功能 |
|------|------|------|
| **Health** | `GET /ping` | 存活检查 |
| **Command** | `POST /command`, `DELETE /command`, `GET /command/status/:id`, `GET /command/:id/logs` | Shell 命令执行（前台/后台） |
| **Code** | `POST /code`, `DELETE /code`, `POST /code/context`, `GET /code/contexts` 等 | 多语言代码执行（通过 Jupyter） |
| **Files** | `GET /files/info`, `POST /files/upload`, `GET /files/download`, `DELETE /files`, `POST /files/mv`, `POST /files/replace`, `GET /files/search` | 文件系统 CRUD |
| **Directories** | `POST /directories`, `DELETE /directories` | 目录管理 |
| **Session** | `POST /session`, `POST /session/:id/run`, `DELETE /session/:id` | 基于 pipe 的 bash 会话 |
| **PTY** | `POST /pty`, `GET /pty/:id/ws`, `DELETE /pty/:id` | WebSocket 交互终端（支持回放） |
| **Metrics** | `GET /metrics`, `GET /metrics/watch` | CPU/内存/运行时间（watch 使用 SSE） |

### 两种执行后端

execd 内部的 `runtime.Controller` 根据语言类型路由到不同后端：

```go
switch request.Language {
case Command:           → runCommand()          // OS exec 后端
case BackgroundCommand: → runBackgroundCommand() // 后台模式
case Bash, Python, Java, JavaScript, TypeScript, Go:
                        → runJupyter()           // Jupyter 后端
case SQL:               → runSQL()
}
```

**1. OS exec 后端**（Shell 命令）

```
execd → os/exec → bash -c "<command>"
  → 设置进程组 (Setpgid: true) 以便信号转发
  → tail stdout/stderr 文件
  → 通过 SSE hooks 流式返回输出
```

**2. Jupyter 后端**（代码执行）

```
execd → WebSocket → Jupyter Server (127.0.0.1:54321, 容器内部)
  → 创建 kernel session
  → 发送 execute_request (Jupyter wire protocol)
  → 流式读取 result/stream/error 消息
  → 通过 SSE hooks 返回给客户端
```

### execd 内部包结构

| 包 | 职责 |
|------|------|
| `pkg/flag/` | CLI 参数：`--jupyter-host`, `--jupyter-token`, `--port`, `--access-token` |
| `pkg/web/router.go` | Gin 路由注册、认证中间件、日志、代理中间件 |
| `pkg/web/controller/` | HTTP 处理器：Filesystem、CodeInterpreting、Metric、PTY |
| `pkg/runtime/` | 执行调度器：管理 `jupyterClientMap`、`commandClientMap`、`bashSessionClientMap`、`ptySessionMap` |
| `pkg/jupyter/` | Jupyter 客户端：kernel 管理、session CRUD、WebSocket 传输 |
| `pkg/clone3compat/` | 可选 seccomp 过滤器，兼容 glibc >= 2.34 的老容器运行时 |

### 资源占用

- 空闲：~50MB RAM，<1% CPU，~15 goroutines
- 可处理 100+ 并发 SSE 连接
- 延迟：`/ping` <1ms，代码执行 50-200ms，文件上传(1MB) 10-50ms

## 命令执行完整链路

```
Client/SDK
  │ POST /sandboxes { image, timeout, resources }
  ▼
Server (FastAPI :8080)  ── 生命周期 API
  │ 1. 拉取镜像
  │ 2. 创建容器（stopped）
  │ 3. 注入 execd 二进制 + bootstrap.sh (Docker put_archive)
  │ 4. 启动容器 → execd 在后台监听 :44772
  │ 5. 轮询 /ping 直到健康
  │ 6. 返回 sandbox ID + endpoints
  ▼
Client/SDK
  │ POST /command { code: "echo hello", language: "command" }
  │ （直连 execd 或通过 Server proxy / K8s Ingress）
  ▼
execd (:44772)
  │ CodeInterpretingController.RunCommand()
  │ → runtime.Controller.Execute()
  │ → runCommand(): bash -c "echo hello"
  │ → tail stdout/stderr, 通过 SSE hooks 流式返回
  ▼
SSE 响应流:
  event: init     → { session_id }
  event: stdout   → "hello\n"
  event: complete → { execution_time_ms }
```

## 沙箱容器内部结构

每个沙箱容器包含：

```
容器
├── 用户基础镜像内容（如 ubuntu:22.04, python:3.11）
├── /opt/opensandbox/
│   ├── execd              ← 注入的 Go 二进制
│   └── bootstrap.sh       ← 注入的启动脚本
├── Jupyter Server (:54321) ← 沙箱镜像 entrypoint 启动（代码执行用）
└── 用户 entrypoint 进程
```

## 网络架构

### Docker 模式

| 子模式 | 原理 | 特点 |
|--------|------|------|
| **Host** | 容器共享宿主机网络，execd 在 `localhost:44772` | 简单，但同时只能跑一个沙箱 |
| **Bridge** | 隔离网络，Server 分配随机宿主机端口(40000-60000)映射到容器 44772 | 支持多沙箱并行 |

Bridge 模式下 SDK 无法直连 execd，通过 Server 内置的 HTTP/WebSocket 代理路由：
`/sandboxes/{id}/proxy/{port}/{path}` → 转发到正确容器。

### Kubernetes 模式

```
Client/SDK ──[HTTP]──→ Ingress (Go 反向代理)
                           │ 路由方式二选一：
                           ├─ Header: OpenSandbox-Ingress-To: <sandbox-id>-<port>
                           └─ URI: /<sandbox-id>/<port>/<path>
                           │
                           ▼
                      Sandbox Pod (:44772 execd)

Sandbox Pod ──[出站]──→ Egress sidecar (共享网络命名空间)
                           ├─ Layer 1: DNS 代理 (127.0.0.1:15353 + iptables 重定向)
                           │           FQDN 白名单/黑名单过滤
                           └─ Layer 2 (可选 dns+nft 模式): nftables 规则
                                       根据 DNS 解析的 IP + TTL 动态控制
                           运行时策略 API: :18080 (POST/PATCH/GET /policy)
```

### 通信总结

```
Client/SDK ──[HTTP]──────→ Server (:8080)         生命周期 API
Client/SDK ──[HTTP/SSE/WS]→ execd (:44772)        执行 API（直连 / proxy / ingress）
execd      ──[WebSocket]──→ Jupyter (:54321)       容器内部代码执行
Egress     ──[iptables/nft]→ 控制沙箱出站流量
```

## 沙箱生命周期

| 阶段     | API                               | 操作                                               |
| ------ | --------------------------------- | ------------------------------------------------ |
| **创建** | `POST /sandboxes`                 | 拉镜像 → 创建容器 → 注入 execd → 启动 → 健康检查 → 返回 ID        |
| **运行** | execd API                         | SDK 与 execd 交互；TTL 倒计时；可通过 `renew-expiration` 续期 |
| **暂停** | `POST /sandboxes/{id}/pause`      | `container.pause()` 冻结所有进程，状态保留在内存               |
| **恢复** | `POST /sandboxes/{id}/resume`     | 解冻暂停的沙箱                                          |
| **销毁** | `DELETE /sandboxes/{id}` 或 TTL 过期 | 停止并删除容器；Bridge 模式同时清理 Egress sidecar；释放端口        |

## 与 Deep Agents 的集成

参见 [[OpenSandbox 沙箱后端集成]]，`langchain-opensandbox` 后端将 Deep Agents 的 `BaseSandbox` 接口映射到 OpenSandbox SDK：

- `execute()` → `sandbox.commands.run()` → execd `/command` 端点
- `upload_files()` → `sandbox.files.write_file()` → execd `/files/upload` 端点
- `download_files()` → `sandbox.files.read_bytes()` → execd `/files/download` 端点

SDK 的 `use_server_proxy=True` 配置在 K8s 环境下通过 Server 代理路由 execd 调用，解决 SDK 无法直连沙箱 Pod 的问题。

## 参考资料

- [OpenSandbox GitHub](https://github.com/alibaba/OpenSandbox)
- [[OpenSandbox 沙箱后端集成]] — Deep Agents 集成实现
- [[opensandbox-deploy-guide|OpenSandbox 部署指南]] — 本地部署与配置
- [[OpenSandbox Session 沙箱复用实践]] — Session 复用优化
- [[Deep Agents]] — Agent 框架与沙箱协议
- [[Landlock Deep Agents 后端集成]] — 本地轻量沙箱方案（互补）
