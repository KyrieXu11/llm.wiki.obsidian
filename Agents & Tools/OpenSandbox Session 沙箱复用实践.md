---
tags:
  - agents
  - sandbox
  - practice
  - architecture
created: "2026-04-08"
aliases:
  - Sandbox Session 复用
  - 沙箱会话复用
---

# OpenSandbox Session 沙箱复用实践

## 结论

**可以支持，但要分两层理解：**

1. **OpenSandbox 原生支持在创建 sandbox 时动态传入 `volumes`**，所以你完全可以在业务层根据 `session_id` 生成不同的 `host.path`、`pvc.claimName` 或 `subPath`。
2. **OpenSandbox 不内建 `session_id` 语义**，不会自动帮你维护“同一个 session 应该挂哪个目录/PVC、复用哪个 sandbox”。这部分需要业务层自己维护 `session_id -> sandbox_id / storage` 映射。

一句话说：**OpenSandbox 支持“按 session_id 动态挂载”，但这是 request-time 的动态参数能力，不是平台内建的 Session Mount 功能。**

## 问题背景

在多轮对话的 Agent 场景中（如合同审查、代码分析），一个 chat session 往往跨越多次请求：

1. 用户："帮我审查这个合同"
2. Assistant 反问："请问你是甲方还是乙方立场？"
3. 用户："甲方"
4. Assistant 继续审查

每次请求都创建新 sandbox 意味着每次都要重新上传文件，带来不必要的性能损耗。需要一种机制让同一 session 的多次请求共享文件和执行环境。

## 方案：Volume 挂载 + Sandbox 复用

核心思路：**文件持久化靠 volume，执行环境复用靠 sandbox 存活检测 + 按需重建**。

### 架构

```
                  session_id → sandbox_id 映射表
                              │
请求1 ──→ 创建 sandbox ───→ 挂载 /data/sessions/{session_id}/ ──→ 执行
                              │
请求2 ──→ sandbox 还活着？ ──┤
           │                  │
           ├─ Yes → connect + 续期 ──→ 执行
           │
           └─ No → 创建新 sandbox ──→ 挂载同一 volume ──→ 执行
                   （文件还在，无需重传）
```

### 两层保障

| 层次      | 机制                    | 作用              | 丢失影响                       |
| ------- | --------------------- | --------------- | -------------------------- |
| **文件层** | Volume 挂载（host / PVC） | 文件跨 sandbox 持久化 | 无——sandbox 死了文件还在          |
| **环境层** | Sandbox 复用 + TTL 续期   | 避免重复创建容器的开销     | 仅丢失内存状态（Python 变量等），文件不受影响 |

### 实现

#### 1. 文件准备

用户上传文件时，按 session_id 存到宿主机或 PVC：

```python
import shutil
from pathlib import Path

def prepare_session_files(session_id: str, uploaded_files: list) -> str:
    session_dir = Path(f"/data/sessions/{session_id}")
    session_dir.mkdir(parents=True, exist_ok=True)
    for f in uploaded_files:
        shutil.copy(f, session_dir / Path(f).name)
    return str(session_dir)
```

#### 2. 获取或创建 Sandbox

```python
import httpx
from datetime import datetime, timedelta, timezone

SESSION_SANDBOX_MAP: dict[str, str] = {}  # 生产环境用 Redis
SANDBOX_TTL = 600  # 10 分钟
SERVER_URL = "http://localhost:8080"

async def get_or_create_sandbox(session_id: str) -> str:
    """返回可用的 sandbox_id，优先复用现有 sandbox。"""
    sandbox_id = SESSION_SANDBOX_MAP.get(session_id)

    if sandbox_id:
        # 检查是否还活着
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{SERVER_URL}/sandboxes/{sandbox_id}")
            if resp.status_code == 200 and resp.json()["status"]["state"] == "Running":
                # 续期
                new_expires = (datetime.now(timezone.utc) + timedelta(seconds=SANDBOX_TTL)).isoformat()
                await client.post(
                    f"{SERVER_URL}/sandboxes/{sandbox_id}/renew-expiration",
                    json={"expiresAt": new_expires},
                )
                return sandbox_id

    # 不存在或已死，创建新 sandbox
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{SERVER_URL}/sandboxes", json={
            "image": {"uri": "python:3.11"},
            "timeout": SANDBOX_TTL,
            "resourceLimits": {"cpu": "0.5", "memory": "512Mi"},
            "entrypoint": ["tail", "-f", "/dev/null"],
            "volumes": [{
                "name": "session-data",
                "host": {"path": f"/data/sessions/{session_id}"},
                "mountPath": "/mnt/work",
            }],
        })
        new_id = resp.json()["id"]
        SESSION_SANDBOX_MAP[session_id] = new_id
        return new_id
```

#### 3. Session 清理

```python
async def cleanup_session(session_id: str):
    """Session 结束时主动清理 sandbox 和文件。"""
    sandbox_id = SESSION_SANDBOX_MAP.pop(session_id, None)
    if sandbox_id:
        async with httpx.AsyncClient() as client:
            await client.delete(f"{SERVER_URL}/sandboxes/{sandbox_id}")

    # 按需清理文件（或保留供审计）
    session_dir = Path(f"/data/sessions/{session_id}")
    if session_dir.exists():
        shutil.rmtree(session_dir)
```

### TTL 策略选择

| 场景 | 推荐 TTL | 理由 |
|------|---------|------|
| 多轮对话（合同审查、代码分析） | 5~10 分钟 | 用户思考+输入时间通常在几分钟内 |
| 交互式开发（Jupyter 风格） | 30~60 分钟 | 长时间交互，频繁续期 |
| 批处理任务 | 与任务时长匹配 | 无需复用 |

配合续期机制，每次请求到达时调用 `renew-expiration` 续期，TTL 只需覆盖"两次请求之间的最大间隔"即可。

### K8s 生产环境适配

开发环境用 `host` 后端即可，K8s 生产环境改用 `pvc`：

```python
# 预创建 PVC（通过 K8s API 或 Helm）
# kubectl apply -f - <<EOF
# apiVersion: v1
# kind: PersistentVolumeClaim
# metadata:
#   name: session-{session_id}
#   namespace: opensandbox
# spec:
#   accessModes: [ReadWriteOnce]
#   resources:
#     requests:
#       storage: 1Gi
# EOF

"volumes": [{
    "name": "session-data",
    "pvc": {"claimName": f"session-{session_id}"},
    "mountPath": "/mnt/work",
}]
```

多节点场景下需使用 `ReadWriteMany` 的 StorageClass（如 NFS、CephFS），或确保同一 session 的 sandbox 调度到同一节点。

### 原方案的优化

原方案是：**每个 `session_id` 创建一个 PVC，再挂到 sandbox**。

更推荐的优化是：**共享 PVC + `subPath=session_id` + sandbox 复用**。

```python
"volumes": [{
    "name": "session-data",
    "pvc": {"claimName": "agent-session-data"},
    "mountPath": "/mnt/work",
    "subPath": f"sessions/{session_id}",
}]
```

核心变化：

- PVC 不再按 session 创建，而是长期复用一个或少量共享 PVC
- session 隔离从“PVC 级别”下沉到“目录级别”
- sandbox 挂掉后，只要重新挂回同一 `subPath`，文件仍可复用

### 原方案的局限

`session_id -> PVC` 的方案主要问题是：

1. PVC/PV 数量会随 session 线性增长，给 K8s 控制面和存储系统增加压力
2. session 结束后的 PVC 回收、残留治理比较麻烦
3. 对 chat 这类短周期请求来说，存储资源粒度过细，成本高于收益

因此，更适合把 PVC 当作**共享存储池**，而不是 **session 级资源**。

### `subPath` 特性介绍

`subPath` 是 Kubernetes `volumeMounts` 的原生能力，用来把一个 volume 中的**某个子目录**挂载到容器内。

例如：

- 共享 PVC：`agent-session-data`
- session A 挂载 `sessions/a`
- session B 挂载 `sessions/b`

这样多个 session 可以复用同一个 PVC，但在各自独立目录下工作。

`subPath` 适合这个场景的原因：

1. 避免为每个 session 单独创建 PVC
2. 仍然能做到 session 级目录隔离
3. sandbox 重建后可以重新挂回同一目录

需要注意：

- `subPath` 解决的是**目录隔离**，不是**并发控制**
- 它不会自动提供 session 语义，`session_id -> sandbox_id` 仍需业务层维护
- 共享 PVC 的清理粒度通常是删除目录，不是删除 PVC

### `subPath` 使用方式

K8s 原生写法示意：

```yaml
volumes:
  - name: session-data
    persistentVolumeClaim:
      claimName: agent-session-data

containers:
  - name: sandbox
    volumeMounts:
      - name: session-data
        mountPath: /mnt/work
        subPath: sessions/session-123
```

OpenSandbox 对应的请求参数可写成：

```json
{
  "volumes": [
    {
      "name": "session-data",
      "pvc": { "claimName": "agent-session-data" },
      "mountPath": "/mnt/work",
      "subPath": "sessions/session-123"
    }
  ]
}
```

推荐目录约定：

```text
sessions/<session_id>/
  input/
  workspace/
  output/
```

在业务层的典型用法是：

1. 根据 `session_id` 计算 `subPath`
2. 查询是否已有可复用的 sandbox
3. 没有则创建新 sandbox，并挂载共享 PVC + 对应 `subPath`
4. 有则直接复用并续期
5. session 结束后删除 `sessions/<session_id>/` 目录

### 设计边界与注意事项

- `host` / `hostPath` 更适合单机或开发环境；K8s 多节点生产优先用 `pvc`
- 如果用 `hostPath`，OpenSandbox 服务端要允许对应的宿主机路径前缀
- 如果用 `pvc`，PVC 必须预先存在，而且要和 sandbox 在同一 namespace
- `subPath` 解决的是路径隔离，不解决并发写冲突；同一目录被多个 sandbox 同时读写时，锁和一致性要业务层自己处理
- 如果你只复用文件，不复用进程内状态，那么 sandbox 挂掉也没关系；新 sandbox 挂回同一 volume 即可继续
- 如果你还想复用 Python 变量、缓存、已启动进程等内存态，就还需要维护 `session_id -> sandbox_id` 并配合 TTL/续期

## 相关页面

- [[opensandbox-deploy-guide|OpenSandbox 部署指南]] — 部署、Volume 挂载 API、生命周期管理详解
- [[OpenSandbox 沙箱后端集成]] — Deep Agents 后端集成
- [[Deep Agents]] — LangChain Agent 框架
