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

## 相关页面

- [[opensandbox-deploy-guide|OpenSandbox 部署指南]] — 部署、Volume 挂载 API、生命周期管理详解
- [[OpenSandbox 沙箱后端集成]] — Deep Agents 后端集成
- [[Deep Agents]] — LangChain Agent 框架
