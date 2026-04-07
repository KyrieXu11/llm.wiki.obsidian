---
tags:
  - deployment
  - security
  - sandbox
  - linux
created: "2026-04-07"
aliases:
  - Landlock
  - landlock
url: "https://landlock.io/"
---

# Landlock LSM

## 简介

Landlock 是 Linux 内核自 5.13（2021 年 6 月）起内置的安全模块（LSM），允许**非特权进程**主动限制自身的文件系统和网络访问权限。与 SELinux/AppArmor 由管理员配置不同，Landlock 的核心设计理念是：**应用程序自己定义自己的沙箱边界**。

关键特性：
- **非特权（Unprivileged）**：不需要 root 或任何 capability
- **不可逆（Irreversible）**：一旦施加，当前进程及所有子进程永远受限
- **可叠加（Stackable）**：多次调用取交集，只会越来越严格
- **零依赖**：纯内核功能，不需要用户态工具

## 解决的问题

| 传统方案 | 痛点 |
|---------|------|
| SELinux / AppArmor | 需要 root 配置策略，应用无法自我限制 |
| seccomp-BPF | 只能过滤 syscall 号，无法区分文件路径 |
| bubblewrap (bwrap) | 依赖 user namespaces，非特权容器中不可用 |
| chroot | 需要 root |
| Docker/容器 | 粒度太粗，容器内多 session 无法互隔离 |

Landlock 填补了 "**应用层自主、路径级粒度、非特权可用**" 的空白。

## 工作原理

### 三个系统调用

```
landlock_create_ruleset()  →  landlock_add_rule()  →  landlock_restrict_self()
       创建规则集                添加允许规则              施加到当前进程
```

#### Step 1: `landlock_create_ruleset()`

创建空的规则集，声明要管控哪些类型的访问，返回 file descriptor。

```c
struct landlock_ruleset_attr attr = {
    .handled_access_fs = LANDLOCK_ACCESS_FS_READ_FILE |
                         LANDLOCK_ACCESS_FS_WRITE_FILE |
                         LANDLOCK_ACCESS_FS_READ_DIR,
};
int ruleset_fd = landlock_create_ruleset(&attr, sizeof(attr), 0);
```

**重要**：`handled_access_fs` 中声明的访问类型，若没有对应 `add_rule`，则该类型访问被**完全禁止**。未声明的类型不受管控。

#### Step 2: `landlock_add_rule()`

为特定路径添加允许规则。`PATH_BENEATH` 表示规则对指定路径**及其所有子路径**递归生效。

```c
struct landlock_path_beneath_attr path_rule = {
    .allowed_access = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE,
    .parent_fd = open("/tmp/workspace", O_PATH),
};
landlock_add_rule(ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, &path_rule, 0);
```

#### Step 3: `landlock_restrict_self()`

施加到当前进程。调用前**必须**先设置 `PR_SET_NO_NEW_PRIVS`。

```c
prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
landlock_restrict_self(ruleset_fd, 0);
close(ruleset_fd);
// 此后当前进程及所有子进程永远受限
```

### 内核检查流程

```
用户态进程 → open("/tmp/other/secret.txt")
    ↓
VFS 层 → LSM hook: security_file_open()
    ↓
Landlock 检查:
  1. 当前进程是否有 ruleset？
  2. 是否声明管控 READ_FILE？
  3. 目标路径是否匹配 allow 规则？
  4. 不匹配 → 返回 -EACCES (Permission denied)
```

### 规则继承与叠加

```
进程 A: restrict_self(ruleset_1)    # 允许 /tmp/workspace 读写
  ├─ fork() → 子进程 B
  │   自动继承 ruleset_1
  │   ├─ restrict_self(ruleset_2)   # 额外限制：只允许 /tmp/workspace/subdir
  │   │   最终权限 = ruleset_1 ∩ ruleset_2
  │   └─ exec("bash") → 子进程 C
  │       继承 ruleset_1 ∩ ruleset_2，无法解除
  └─ 进程 A 本身只受 ruleset_1 限制
```

## ABI 版本演进

| ABI 版本 | 内核版本 | 新增能力 |
|----------|---------|---------|
| v1 | 5.13 | 基础文件系统访问控制（13 种 access right） |
| v2 | 5.19 | `REFER`（跨目录 rename/link） |
| v3 | 6.2 | `TRUNCATE`（truncate 文件） |
| v4 | 6.7 | **网络控制**：`NET_BIND_TCP`、`NET_CONNECT_TCP` |
| v5 | 6.10 | `IOCTL_DEV`（设备 ioctl） |

版本兼容最佳实践：尝试最高版本 ruleset，失败则降级：

```c
int fd = landlock_create_ruleset(&v4_attr, sizeof(v4_attr), 0);
if (fd < 0 && errno == EINVAL) {
    fd = landlock_create_ruleset(&v1_attr, sizeof(v1_attr), 0);  // 降级
}
```

## 文件系统访问权限（v1-v5）

| 权限标志 | 含义 | ABI |
|---------|------|-----|
| `FS_EXECUTE` | 执行文件 | v1 |
| `FS_WRITE_FILE` | 写入文件 | v1 |
| `FS_READ_FILE` | 读取文件 | v1 |
| `FS_READ_DIR` | 列出目录 | v1 |
| `FS_REMOVE_DIR` | 删除目录 | v1 |
| `FS_REMOVE_FILE` | 删除文件 | v1 |
| `FS_MAKE_CHAR` | 创建字符设备 | v1 |
| `FS_MAKE_DIR` | 创建目录 | v1 |
| `FS_MAKE_REG` | 创建普通文件 | v1 |
| `FS_MAKE_SOCK` | 创建 socket 文件 | v1 |
| `FS_MAKE_FIFO` | 创建 FIFO | v1 |
| `FS_MAKE_BLOCK` | 创建块设备 | v1 |
| `FS_MAKE_SYM` | 创建符号链接 | v1 |
| `FS_REFER` | 跨目录 rename/link | v2 |
| `FS_TRUNCATE` | truncate 文件 | v3 |
| `FS_IOCTL_DEV` | 设备 ioctl | v5 |

## 网络访问权限（v4+，内核 6.7+）

| 权限标志 | 含义 |
|---------|------|
| `NET_BIND_TCP` | 绑定 TCP 端口 |
| `NET_CONNECT_TCP` | 连接 TCP 端口 |

```c
struct landlock_net_port_attr net_rule = {
    .allowed_access = LANDLOCK_ACCESS_NET_CONNECT_TCP,
    .port = 443,
};
landlock_add_rule(ruleset_fd, LANDLOCK_RULE_NET_PORT, &net_rule, 0);
```

## 与其他安全机制的对比

| 特性 | Landlock | SELinux | AppArmor | seccomp | bwrap |
|------|---------|---------|----------|---------|-------|
| 需要 root | **否** | 是 | 是 | 否 | 否（需 user ns） |
| 路径级粒度 | 是 | 是 | 是 | 否 | 是 |
| 非特权容器可用 | **是** | 是 | 是 | 是 | **否** |
| 应用自主配置 | **是** | 否 | 否 | 是 | 是 |
| 网络控制 | 6.7+ | 是 | 是 | 是 | 部分 |
| 可叠加 | **是** | 否 | 部分 | 是 | 否 |
| 性能开销 | 极低 | 中 | 低 | 极低 | 低 |

## 已知限制

| 限制 | 说明 |
|------|------|
| 无法排除子目录 | 允许 `/app` 就允许其下所有内容 |
| 仅 TCP 网络控制 | v4 不支持 UDP/ICMP/Unix socket |
| 目录级规则 | 无法精确到单个文件 |
| 已打开的 fd 不受限 | 仅在 open 时检查 |
| procfs/sysfs | 特殊文件系统行为可能与预期不同 |
| 无信号控制 | 无法限制进程间信号传递 |

**v1 的 rename/link 绕过问题（5.13-5.18）**：可通过 `rename()` 将无权限目录中的文件移到有权限目录来"偷取"文件。v2（5.19）引入 `REFER` 修复。建议 5.19 以下内核配合 seccomp 禁用 `rename`/`link`。

## 已采用的知名项目

- **Chromium/Chrome**：沙箱化渲染进程
- **systemd**：`LandlockPaths=` 指令
- **Android (AOSP)**：应用沙箱增强
- **Tor Browser**：限制浏览器文件访问
- **pipewire**：限制音频服务文件访问

## 各语言使用方式

### Python（ctypes 直接调用）

```python
import ctypes, ctypes.util, os

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
ruleset_fd = libc.syscall(444, ...)   # landlock_create_ruleset
libc.syscall(445, ...)                # landlock_add_rule
libc.prctl(38, 1, 0, 0, 0)           # PR_SET_NO_NEW_PRIVS
libc.syscall(446, ...)                # landlock_restrict_self
```

第三方库：`pip install landlock`

### Rust

```rust
use landlock::{Access, AccessFs, PathBeneath, PathFd, Ruleset, ABI};

Ruleset::default()
    .handle_access(AccessFs::from_all(ABI::V3))?
    .create()?
    .add_rule(PathBeneath::new(PathFd::new("/tmp/workspace")?,
        AccessFs::from_read(ABI::V3) | AccessFs::WriteFile))?
    .restrict_self()?;
```

crate: [`landlock`](https://crates.io/crates/landlock)（Landlock 作者维护）

### Go

```go
import "github.com/landlock-lsm/go-landlock/landlock"

landlock.V3.BestEffort().RestrictPaths(
    landlock.RWDirs("/tmp/workspace"),
    landlock.RODirs("/usr", "/etc"),
)
```

库: [`go-landlock`](https://github.com/landlock-lsm/go-landlock)（官方维护）

## 最佳实践

### 优雅降级

```python
if landlock.is_supported():
    landlock.apply(rules)
else:
    logger.warning("Landlock not supported, falling back to app-level checks")
```

### 最小权限原则

```python
# 好：精确到 session 目录
rules = {f"/tmp/chat_review/{session_id}": FS_READ_WRITE}

# 差：放开整个 /tmp
rules = {"/tmp": FS_READ_WRITE}  # 所有 session 互相可见！
```

### 多层防御

```
层级 1: Landlock           → 内核级文件系统隔离（不可绕过）
层级 2: can_use_tool 回调   → 应用层工具白名单
层级 3: permissions 配置    → Claude Code 自带权限规则
层级 4: K8s NetworkPolicy   → 网络层隔离
```

## 参考资料

- Linux 内核文档: https://docs.kernel.org/userspace-api/landlock.html
- Landlock 官网: https://landlock.io/
- 内核源码: `security/landlock/` 目录
- [[opensandbox-deploy-guide|OpenSandbox 部署指南]] — 容器级沙箱方案
- [[OpenSandbox 沙箱后端集成]] — Agent 框架沙箱集成
