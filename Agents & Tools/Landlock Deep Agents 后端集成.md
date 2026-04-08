---
tags:
  - agents
  - sandbox
  - security
  - deepagents
  - landlock
created: "2026-04-07"
aliases:
  - deepagents-landlock
  - Landlock Deep Agents Backend
---

# Landlock Deep Agents 后端集成

## 概述

`deepagents-landlock` 是为 [[Deep Agents]]（LangChain Agent 框架）实现的 [[Landlock LSM]] 本地沙箱后端。与容器级沙箱方案（如 [[OpenSandbox 沙箱后端集成|OpenSandbox]]、Modal）不同，本方案在**同一台机器上**通过 Linux 内核 Landlock 模块实现进程级文件系统隔离，无需远程容器、无需 root 权限。

### 核心优势

| 特性      | Landlock 后端              | 容器级后端 (OpenSandbox/Modal) |
| ------- | ------------------------ | ------------------------- |
| 启动延迟    | **~0ms**（fork + syscall） | 数秒～数十秒（镜像拉取 + 容器启动）       |
| 资源开销    | **极低**（普通子进程）            | 中（容器运行时 + 镜像层）            |
| 需要 root | **否**                    | 否（但需 Docker/K8s）          |
| 隔离级别    | 内核级文件系统                  | 完整容器（网络 + 文件 + 进程）        |
| 网络隔离    | 仅 TCP（6.7+）              | 完整                        |
| 适用场景    | 本地开发、CI、轻量级 Agent        | 生产多租户、不可信代码执行             |

## 架构设计

```
┌─ Deep Agents 框架 ────────────────────────────────┐
│  Agent → 调用 BaseSandbox 接口                      │
│    ├── execute("ls -la")                            │
│    ├── upload_files([("app.py", b"...")])            │
│    └── download_files(["output.txt"])                │
└──────────────────┬────────────────────────────────┘
                   │
                   ▼
┌─ LandlockSandbox ────────────────────────────────┐
│                                                    │
│  execute():                                        │
│    subprocess.run(                                  │
│      [python, -c, <wrapper_script>],               │
│      cwd=workspace                                 │
│    )                                               │
│    wrapper_script:                                  │
│      1. import landlock                             │
│      2. landlock.apply(rules)  ← 不可逆             │
│      3. os.execv("/bin/bash", ["bash", "-c", cmd])  │
│    → 子进程受 Landlock 内核限制                       │
│                                                    │
│  upload_files() / download_files():                 │
│    父进程直接 I/O + 应用层路径校验                     │
│    （不经过 Landlock，由 _validate_path 确保          │
│     路径在 workspace 范围内）                        │
└──────────────────────────────────────────────────┘
```

### 关键设计决策

**1. 父进程不受限，子进程独立受限**

Landlock 的 `restrict_self()` 不可逆。如果限制父进程，整个 Agent 运行时都会被锁定。因此采用 fork 子进程方案：每次 `execute()` 都 fork 一个新进程，在子进程中 apply Landlock 后 exec 命令。

**2. Python wrapper 脚本而非 `preexec_fn`**

使用 `subprocess.run([python, -c, wrapper_script])` 而非 `preexec_fn` 回调。原因：
- `preexec_fn` 在 fork 后的子进程中运行，但 ctypes 和多线程的交互可能不稳定
- wrapper 脚本方式更透明、更容易调试（`strace` 可追踪）
- 与 [[Landlock Agent 沙箱实践]] 中 `sandbox_cli_wrapper.py` 的设计一致

**3. 文件操作由父进程直接完成**

`upload_files()` 和 `download_files()` 不经过子进程/Landlock，由父进程直接读写文件系统。安全边界由应用层 `_validate_path()` 保证路径在 workspace 内。这避免了为每次文件操作都 fork 子进程的开销。

**4. macOS/非 Linux 环境自动降级**

`enable_landlock=None`（默认）时自动检测 `landlock.is_supported()`。macOS 开发环境下 Landlock 不可用，自动跳过限制直接执行命令，本地开发体验不受影响。

## 使用方式

### 编程接口

```python
from deepagents_landlock import LandlockSandbox

# 创建沙箱（自动检测 Landlock 支持）
sandbox = LandlockSandbox(
    workspace="/tmp/agent-workspace",
    extra_ro_paths=["/data/models"],    # 额外只读路径
    extra_rw_paths=["/tmp/shared"],     # 额外读写路径
)

# 执行命令（子进程被 Landlock 限制）
result = sandbox.execute("python app.py")
print(result.output, result.exit_code)

# 文件操作（父进程直接 I/O）
sandbox.upload_files([("/tmp/agent-workspace/input.json", b'{"key": "val"}')])
responses = sandbox.download_files(["output.json"])

# 清理
sandbox.cleanup()
```

### 工厂方法

```python
# 自动创建临时 workspace
sandbox = LandlockSandbox.create()

# 指定 workspace
sandbox = LandlockSandbox.create(
    workspace="/tmp/my-sandbox",
    enable_landlock=True,    # 强制启用
)
```

### CLI 集成

注册到 Deep Agents CLI 需修改 `sandbox_factory.py` 中的三处：

1. `_PROVIDER_TO_WORKING_DIR` 添加 `"landlock": "/workspace"`
2. `_get_provider()` 添加 `LandlockProvider` 分支
3. `verify_sandbox_deps()` 添加 `"landlock": ("deepagents_landlock",)`

### 环境变量

| 变量                        | 说明                    | 默认值                        |
| ------------------------- | --------------------- | -------------------------- |
| `LANDLOCK_WORKSPACE_ROOT` | sandbox workspace 父目录 | `/tmp/deepagents-landlock` |
| `LANDLOCK_EXTRA_RO_PATHS` | 逗号分隔的额外只读路径           | —                          |
| `LANDLOCK_EXTRA_RW_PATHS` | 逗号分隔的额外读写路径           | —                          |
| `LANDLOCK_ENABLED`        | `"0"` 强制禁用 Landlock   | 自动检测                       |

## 默认 Landlock 规则

```
workspace/          → 完整读写（FS_READ_WRITE）
workspace/.tmp/     → 子进程临时目录（$TMPDIR 指向此处）
/usr, /lib, /bin... → 只读 + 可执行（FS_READ_EXECUTE）
/etc, /proc, /dev   → 只读（FS_READ）
其他所有路径          → 内核拒绝（EACCES）
```

## 项目结构

```
deepagents-landlock/
├── deepagents_landlock/
│   ├── __init__.py        # 导出 LandlockSandbox
│   ├── landlock.py        # Landlock syscall 封装（零依赖 ctypes）
│   ├── sandbox.py         # BaseSandbox 实现（核心）
│   └── provider.py        # CLI SandboxProvider 实现
├── tests/
│   └── test_sandbox.py    # 28 个单元测试
├── pyproject.toml
└── README.md
```

源码位于 `~/tmp/deepagents-landlock/`。

## 测试

```bash
cd ~/tmp/deepagents-landlock
python -m pytest tests/test_sandbox.py -v    # 28 passed
```

单元测试通过 `enable_landlock=False` 在任何平台运行，验证适配器逻辑：命令执行、stdout/stderr 合并、文件上传下载、路径校验、超时处理、生命周期管理。

Landlock 集成测试需要 Linux 5.13+ 内核环境。

## 参考资料

- [[Landlock LSM]] — Landlock 技术原理与 API 详解
- [[Landlock Agent 沙箱实践]] — Landlock 在 Agent 环境中的实践经验
- [[Deep Agents]] — Deep Agents 框架与沙箱协议
- [[OpenSandbox 沙箱后端集成]] — 容器级沙箱方案（互补参考）
