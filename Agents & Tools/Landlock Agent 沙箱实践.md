---
tags:
  - agents
  - sandbox
  - security
  - python
created: "2026-04-07"
aliases:
  - Landlock Agent Sandbox
  - Agent 沙箱隔离实践
---

# Landlock Agent 沙箱实践

## 概述

本文记录在 Claude Agent SDK 的非特权 Pod 环境中，使用 [[Landlock LSM]] 替代不可用的 bubblewrap，实现 session 级文件系统隔离的完整方案。

核心目标：每个 agent session 只能访问自己的 workspace，不同 session 之间在内核层面互相隔离。

### 最终隔离效果

```
每个 agent session 独立的 Landlock 规则：
├── /tmp/chat_review/{session_id}/  → 读写（当前 session 的 workspace）
├── /tmp/claude-sandbox-{random}/   → 读写（claude CLI 专属临时目录）
├── /app/                           → 读写（home 目录，claude CLI 需要）
├── /usr, /lib, /bin, /opt          → 只读+可执行（系统库）
├── /etc, /proc, /dev               → 只读
└── 其他所有路径                      → 不可访问（内核强制）
```

## 架构设计

方案由三个文件组成，各司其职：

```
┌─ 项目配置层 ─────────────────────────────────────────────┐
│  .claude/sandbox_config.json    静态规则（系统路径、deny）  │
│  .claude/settings.json          Claude Code 配置           │
└──────────────────────────────────────────────────────────┘
                    │ symlink 到 session workspace
                    ▼
┌─ 应用层 (agent_sdk_runner.py) ───────────────────────────┐
│  读取静态配置 + 动态注入 session workspace                  │
│  → 序列化为 SANDBOX_RULES env var                         │
│  → cli_path = sandbox_cli_wrapper.py                      │
└──────────────────────────────────────────────────────────┘
                    │ SDK spawn
                    ▼
┌─ 包装层 (sandbox_cli_wrapper.py) ────────────────────────┐
│  解析 SANDBOX_RULES → 构建 Landlock 规则                   │
│  → landlock.apply(rules)  不可逆                          │
│  → os.execv(claude)  替换为 claude 进程                    │
└──────────────────────────────────────────────────────────┘
                    │ 内核级限制已生效
                    ▼
┌─ claude CLI 进程 ────────────────────────────────────────┐
│  所有文件操作经过 VFS → Landlock LSM hook                  │
│  ├─ workspace 内操作     → 允许                           │
│  ├─ 其他 session 目录    → EACCES                         │
│  ├─ /etc/shadow 等       → EACCES                         │
│  └─ python -c 子进程越权 → EACCES（子进程继承限制）         │
└──────────────────────────────────────────────────────────┘
```

## 第一层：`landlock.py` — 内核接口封装

**设计目标**：零依赖、最小化、直接调用内核 syscall。

### 核心技术：ctypes

ctypes 是 Python **标准库**模块，可以调用 C 动态链接库函数。Landlock 的 3 个 syscall 没有现成 Python 封装，但通过 ctypes 加载 libc 的通用 `syscall()` 函数即可直接调用：

```python
import ctypes, ctypes.util

libc = ctypes.CDLL(ctypes.util.find_library("c"))

ruleset_fd = libc.syscall(444, ...)   # 444 = landlock_create_ruleset
libc.syscall(445, ...)                # 445 = landlock_add_rule
libc.syscall(446, ...)                # 446 = landlock_restrict_self
libc.prctl(38, 1, 0, 0, 0)           # 38 = PR_SET_NO_NEW_PRIVS
```

调用链路：**Python → ctypes → libc.so → 内核 syscall**

用 ctypes 定义 C 结构体，内存布局与内核定义完全一致：

```python
class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]

class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]
```

**为什么用 ctypes 而不用第三方库**：
- 容器镜像不想多装 pip 包，ctypes 是标准库自带
- Landlock API 极简（3 个 syscall + 2 个结构体），封装代码量小
- 直接控制底层，出问题容易定位

### 关键设计决策

**1. 全量声明管控类型（默认拒绝一切）**

```python
_ALL_HANDLED_ACCESS = (
    FS_ALL
    | LANDLOCK_ACCESS_FS_MAKE_CHAR
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_BLOCK
)
```

`create_ruleset` 时声明管控所有 13 种操作。"声明了但没 add_rule = 完全禁止"，即**白名单模型**。

**2. `O_PATH | O_CLOEXEC` 打开目录**

```python
fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
```

- `O_PATH`：只获取路径 fd，不实际打开文件，不需要读写权限
- `O_CLOEXEC`：exec 后自动关闭 fd，防止泄漏到子进程

**3. 不存在的路径自动跳过**

不同环境（容器 vs 物理机）目录结构不同，如 `/lib64` 在某些系统不存在。跳过而不报错，保证同一份配置跨环境可用。

**4. `is_supported()` 探测方式**

不检查内核版本字符串（不可靠，有 backport），而是直接尝试 `create_ruleset` syscall。不支持返回 `ENOSYS`，支持则返回有效 fd。

**5. `_check()` 错误处理**

```python
def _check(ret: int, name: str):
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"{name}: {os.strerror(errno)}")
```

ctypes 调用 syscall 失败返回 -1 而非抛异常，必须手动检查 `ctypes.get_errno()`，包装成 `OSError` 供上层优雅降级。

## 第二层：`sandbox_cli_wrapper.py` — CLI 包装器

**核心思路**：利用 SDK 的 `cli_path` 参数，在 claude CLI 启动**之前**注入 Landlock 限制。

```
正常流程:  SDK → spawn claude CLI → 运行
沙箱流程:  SDK → spawn wrapper.py → apply Landlock → os.execv(claude) → 运行（受限）
```

### 为什么用 `os.execv` 而不是 `subprocess`

`os.execv` 直接**替换**当前进程为 claude，不创建子进程：
- Landlock 限制自动继承（同一进程 exec 后的新程序）
- 没有多余 Python 进程占资源
- SDK 管理的进程 PID 不变，不影响进程管理

### 配置传递链路

```
sandbox_config.json (静态基础规则)
        │
        ▼
agent_sdk_runner.py (读取 + 动态注入 session workspace)
        │
        ├─ env["SANDBOX_RULES"] = json.dumps(完整规则)
        ├─ env["SANDBOX_CLAUDE_BIN"] = 真实 claude 路径
        ├─ env["SANDBOX_WORKSPACE"] = /tmp/chat_review/{session_id}
        └─ env["SANDBOX_CLAUDE_TMPDIR"] = /tmp/claude-sandbox-{random}
        │
        ▼
sandbox_cli_wrapper.py (解析 → 构建规则 → apply → exec)
```

用环境变量传递而非命令行参数，因为 SDK 控制了 claude CLI 的完整命令行参数，wrapper 只能通过 `sys.argv[1:]` 透传。

### 路径解析

```python
def _resolve_path(path: str, workspace: str) -> str:
    if path == "./" or path == ".":
        return workspace
    if path.startswith("./"):
        return os.path.join(workspace, path[2:])
    if path.startswith("~/"):
        return os.path.expanduser(path)
    return path
```

配置文件支持三种路径：`./`（session workspace）、`~/`（home 目录）、绝对路径。

### TMPDIR 重定向

```python
if claude_tmpdir:
    os.environ["TMPDIR"] = claude_tmpdir
```

claude CLI（Node.js）默认用 `/tmp`，但 Landlock 没给 `/tmp` 整体权限。设置 `TMPDIR` 让 claude 用专属临时目录，不放开整个 `/tmp`。

## 第三层：`agent_sdk_runner.py` — 集成入口

### 动态规则生成

```python
# 1) 当前 session 的 workspace
_rw_paths.append(_session_workspace)  # /tmp/chat_review/{session_id}/

# 2) 专属临时目录
_claude_tmp = tempfile.mkdtemp(prefix="claude-sandbox-")
_rw_paths.append(_claude_tmp)

# 3) deny 其他 session
_deny_paths.append(_chat_review_root)  # /tmp/chat_review/
```

实现 session 间隔离的核心：
- 允许：`/tmp/chat_review/session-A/`
- deny：`/tmp/chat_review/`（整个目录）

由于 Landlock 是白名单模型，deny 路径不加入规则即可。`session-B` 无 allow 规则，内核直接返回 `EACCES`。

### macOS 兼容

```python
_use_landlock = sys.platform == "linux" and ...
```

开发环境（macOS）自动跳过 Landlock，`sandbox_cli_path` 为 `None`，SDK 直接调用 claude CLI。本地开发不受影响，只有线上 Linux Pod 启用沙箱。

## 实践笔记

- ctypes 调用 syscall 需设置 `use_errno=True`，否则 `get_errno()` 拿不到正确值
- Landlock 只在 `open` 时检查，已打开的 fd 不受限，wrapper 必须在 exec claude 之前 close 所有不必要的 fd
- `PR_SET_NO_NEW_PRIVS` 是 Landlock 的前置要求，也能防止子进程通过 setuid 提权
- 测试时可通过 `strace -e landlock_create_ruleset,landlock_add_rule,landlock_restrict_self` 追踪 syscall 调用

## 参考资料

- [[Landlock LSM]] — Landlock 技术原理与 API 详解
- [[OpenSandbox 沙箱后端集成]] — 容器级沙箱方案（互补）
- [[Deep Agents]] — Agent 框架的沙箱协议
