---
tags:
  - deployment
  - sandbox
  - kubernetes
  - docker
created: "2026-04-07"
aliases:
  - OpenSandbox 部署指南
---

# OpenSandbox 部署与使用指南

## 1. 前置条件

**通用要求：**

- Python 3.10+
- `uv` 包管理器（`pip install uv` 或 `brew install uv`）
- macOS / Linux / Windows WSL2

**Docker 部署额外要求：**

- Docker Engine 20.10+（守护进程运行中）

**Kubernetes 部署额外要求：**

- minikube 已安装并可用（或其他 K8s 集群）
- Helm 3.0+
- Kubernetes 1.21.1+

---

## 2. Docker 部署

Docker 模式下，Server 通过 Docker socket 直接管理沙箱容器。以下提供三种部署方式，按复杂度递增排列。

### 2.1 pip 安装（推荐，最简单）

```bash
# 1. 安装 server
uv pip install opensandbox-server

# 2. 生成 Docker 模式的配置文件
opensandbox-server init-config ~/.sandbox.toml --example docker
# 中文注释版本用 --example docker-zh

# 3. 启动 server
opensandbox-server

# 4. 验证
curl http://127.0.0.1:8080/health
# 返回 {"status": "healthy"}
```

Server 启动后：
- Swagger 文档：`http://localhost:8080/docs`
- ReDoc 文档：`http://localhost:8080/redoc`

### 2.2 从源码运行

```bash
# 1. 克隆仓库
git clone https://github.com/alibaba/OpenSandbox.git
cd OpenSandbox/server

# 2. 复制示例配置
cp opensandbox_server/examples/example.config.toml ~/.sandbox.toml

# 3. 安装依赖并启动
uv sync
uv run python -m opensandbox_server.main
```

### 2.3 Docker Compose 全容器化部署

仓库提供了 `server/docker-compose.example.yaml`，包含两个服务：

```bash
cd OpenSandbox/server

# 复制并按需修改
cp docker-compose.example.yaml docker-compose.yaml

# 启动
docker compose up -d
```

该 compose 文件包含：

| 服务 | 说明 |
|---|---|
| `opensandbox-server` | Server 本体，暴露 8090 端口，挂载 Docker socket |
| `sdk-client` | Python 3.11 SDK 客户端容器 |

关键点：Server 容器需要挂载 `/var/run/docker.sock` 以便管理沙箱容器。

---

## 3. Kubernetes (Minikube) 部署

OpenSandbox 原生支持 Kubernetes，提供完整的 Helm charts 和 CRD operator。K8s 模式下**不需要挂载 Docker socket**，沙箱直接以 Kubernetes Pod 形式运行。

### 3.1 启动 minikube

```bash
minikube start --cpus=4 --memory=8192
```

### 3.2 克隆仓库

```bash
git clone https://github.com/alibaba/OpenSandbox.git
cd OpenSandbox
```

### 3.3 修复 Helm 模板并安装

> 当前 v0.1.0 版本的 Helm chart 存在已知问题（详见 3.8），以下命令已包含所有修复步骤。

```bash
# 1. 修复 controller 模板中的类型比较 bug
cd kubernetes/charts/opensandbox-controller/templates
sed -i '' \
  's/gt .Values.controller.kubeClient.qps 0/gt (.Values.controller.kubeClient.qps | float64) 0.0/g; s/gt .Values.controller.kubeClient.burst 0/gt (.Values.controller.kubeClient.burst | float64) 0.0/g' \
  deployment.yaml

# 2. 重新构建 umbrella chart 依赖
cd ../../opensandbox
helm dependency build

# 3. 创建沙箱 Pod 所需的 namespace
kubectl create namespace opensandbox

# 4. 安装（含 kubeClient 参数跳过修复）
helm install opensandbox ./kubernetes/charts/opensandbox \
  --namespace opensandbox-system \
  --create-namespace \
  --set opensandbox-server.server.replicaCount=1 \
  --set opensandbox-server.server.resources.requests.cpu=500m \
  --set opensandbox-server.server.resources.requests.memory=1Gi \
  --set opensandbox-controller.controller.replicaCount=1 \
  --set-json 'opensandbox-controller.controller.kubeClient={"qps":0.0,"burst":0.0}'
```

### 3.4 等待 Pod 就绪

```bash
kubectl get pods -n opensandbox-system -w
# 等待 opensandbox-controller-manager 和 opensandbox-server 都变为 1/1 Running
```

### 3.5 预加载沙箱镜像

Minikube 节点可能无法直接拉取 Docker Hub 镜像（见 3.8 问题四），建议提前从本机导入：

```bash
# 将本机已有的镜像导入 minikube（以实际可用的镜像为准）
minikube image load ezone.kingsoft.com/ksyun/ai-app-docker/release/python:3.12-bookworm-slim-uv0.8-patched
```

### 3.6 端口转发访问 Server

```bash
kubectl port-forward svc/opensandbox-server 8080:80 -n opensandbox-system
```

验证：

```bash
curl -s http://127.0.0.1:8080/health
# 返回 {"status": "healthy"}
```

API 文档同样可通过 `http://localhost:8080/docs` 访问。

### 3.7 Minikube 注意事项

| 项目 | 说明 |
|---|---|
| 资源分配 | 建议 4 CPU / 8G 内存，本地开发可降低 replica 和 request |
| 沙箱运行方式 | K8s 模式下沙箱是原生 Pod，不是 Docker 容器 |
| Pool 预热 | 支持通过 Pool CRD 预创建沙箱 Pod，本地可把 `bufferMin`/`bufferMax` 设小 |
| 暂停/恢复 | K8s 运行时**不支持** pause/resume API |
| gVisor 隔离 | 可选，项目提供了 RuntimeClass 配置示例（见 `kubernetes/test/e2e_runtime/gvisor/`） |

详细 Helm 部署文档见仓库 `kubernetes/docs/HELM-DEPLOYMENT.md`。

### 3.8 本地 Minikube 部署已知问题

以下问题在本地 Minikube 环境实际部署验证中发现（OpenSandbox Helm chart v0.1.0）：

**问题一：Helm 模板类型比较错误**

使用 umbrella chart 安装时，controller 的 `deployment.yaml` 模板会报错：

```
error calling gt: incompatible types for comparison: float64 and int
```

原因：模板中 `gt .Values.controller.kubeClient.qps 0` 将 float64 类型的 values 与 int 类型的 `0` 比较，Helm 不允许跨类型比较。

修复方法：编辑 `kubernetes/charts/opensandbox-controller/templates/deployment.yaml`，将比较改为统一的 float 类型：

```yaml
# 修改前
{{- if and .Values.controller.kubeClient (gt .Values.controller.kubeClient.qps 0) }}
{{- if and .Values.controller.kubeClient (gt .Values.controller.kubeClient.burst 0) }}

# 修改后
{{- if and .Values.controller.kubeClient (gt (.Values.controller.kubeClient.qps | float64) 0.0) }}
{{- if and .Values.controller.kubeClient (gt (.Values.controller.kubeClient.burst | float64) 0.0) }}
```

修改后需重新构建依赖：

```bash
cd kubernetes/charts/opensandbox && helm dependency build
```

**问题二：Controller 不识别 `--kube-client-qps/burst` 启动参数**

修复问题一后 controller Pod 仍会 CrashLoopBackOff，日志显示：

```
flag provided but not defined: -kube-client-qps
```

原因：当前版本的 controller 二进制（v0.1.0）尚未实现 `--kube-client-qps` 和 `--kube-client-burst` 参数，但 Helm chart values 中 `kubeClient.qps` 默认值为 100（大于 0），导致模板渲染出了不被支持的启动参数。

解决方法：安装/升级时将这两个值设为 0 以跳过参数生成：

```bash
helm install opensandbox ./kubernetes/charts/opensandbox \
  --namespace opensandbox-system \
  --create-namespace \
  --set opensandbox-server.server.replicaCount=1 \
  --set opensandbox-server.server.resources.requests.cpu=500m \
  --set opensandbox-server.server.resources.requests.memory=1Gi \
  --set opensandbox-controller.controller.replicaCount=1 \
  --set-json 'opensandbox-controller.controller.kubeClient={"qps":0.0,"burst":0.0}'
```

**问题三：沙箱 namespace 不存在**

创建沙箱时报错：

```
namespaces "opensandbox" not found
```

原因：Server 的配置 `[kubernetes] namespace = "opensandbox"` 默认将沙箱 Pod 创建在 `opensandbox` namespace 中，但 Helm 安装只创建了 `opensandbox-system` namespace。

解决方法（二选一）：

- **方式 A：** 创建独立 namespace：`kubectl create namespace opensandbox`
- **方式 B：** 修改 ConfigMap 让沙箱复用 `opensandbox-system` namespace：

```bash
# 导出、修改、应用 ConfigMap
kubectl get configmap opensandbox-server-config -n opensandbox-system \
  -o jsonpath='{.data.config\.toml}' \
  | sed 's/namespace = "opensandbox"/namespace = "opensandbox-system"/' \
  | kubectl create configmap opensandbox-server-config \
      --from-file=config.toml=/dev/stdin \
      --dry-run=client -o yaml \
  | kubectl apply -n opensandbox-system -f -

# 重启 server 使配置生效
kubectl rollout restart deployment/opensandbox-server -n opensandbox-system
```

> 注意：该配置项嵌在 TOML 字符串中，无法通过 Helm `--set` 修改，需要直接操作 ConfigMap。

**问题四：Minikube 无法拉取 Docker Hub 镜像**

创建沙箱后 Pod 卡在 `ErrImagePull`：

```
Failed to pull image "python:3.11": net/http: request canceled while waiting for connection
```

原因：Minikube 节点无法访问 `registry-1.docker.io`（国内网络环境常见问题）。

解决方法：从本机 Docker 导入镜像到 Minikube：

```bash
# 先确认本机有目标镜像（或从可用的镜像源拉取）
docker pull <your-python-image>

# 导入到 minikube
minikube image load <your-python-image>
```

创建沙箱时使用已导入的镜像即可。

**问题五：SDK `metadata: null` 解析崩溃**

使用 Python SDK 的 `Sandbox.create()` 创建沙箱时，Server 返回 `"metadata": null`，但 SDK 尝试将其作为 dict 解析导致报错：

```
TypeError: 'NoneType' object is not iterable
```

这是 SDK（v0.1.6）与 Server（v0.1.8）的兼容性 bug。解决方法：使用 REST API 直接调用（见下方端到端验证章节）。

---

## 4. 配置说明

### 4.1 Docker 模式配置（~/.sandbox.toml）

```toml
[server]
host = "127.0.0.1"    # 监听地址
port = 8080            # 监听端口
log_level = "INFO"
api_key = ""           # 生产环境建议设置

[runtime]
type = "docker"
execd_image = "opensandbox/execd:v1.0.9"  # 执行守护进程镜像

[docker.security]
drop_capabilities = ["NET_ADMIN", "SYS_ADMIN", "SYS_PTRACE"]
no_new_privileges = true
pids_limit = 4096

[egress]
# DNS 级别的出站流量控制

[ingress]
# 默认 direct 直连模式
```

可通过环境变量覆盖配置文件路径：

```bash
SANDBOX_CONFIG_PATH=/path/to/config.toml opensandbox-server
```

### 4.2 Kubernetes 模式配置

K8s 模式下配置通过 Helm values 注入，核心区别是 `runtime.type = "kubernetes"`。Server 通过 ServiceAccount 获取集群权限，无需 Docker socket。可通过 Helm `--set` 或自定义 `values.yaml` 调整参数。

---

## 5. SDK 安装与使用

```bash
# 安装 Python SDK（含 code interpreter）
uv pip install opensandbox-code-interpreter
```

示例代码：

```python
import asyncio
from datetime import timedelta
from opensandbox import Sandbox

async def main():
    sandbox = await Sandbox.create(
        "opensandbox/code-interpreter:v1.0.2",
        entrypoint=["/opt/opensandbox/code-interpreter.sh"],
        env={"PYTHON_VERSION": "3.11"},
        timeout=timedelta(minutes=10),
    )

    # 执行命令
    result = await sandbox.commands.run("echo hello")
    print(result.stdout)

    # 用完销毁
    await sandbox.kill()

asyncio.run(main())
```

如果 Server 不在默认地址，通过环境变量指定：

```bash
export OPENSANDBOX_SERVER_URL=http://localhost:8090
```

---

## 6. 快速验证清单

| 步骤 | Docker 部署 | Kubernetes 部署 |
|---|---|---|
| 确认运行环境 | `docker ps` | `kubectl get pods -n opensandbox-system` |
| Server 健康检查 | `curl http://127.0.0.1:8080/health` | 端口转发后同左 |
| 查看 API 文档 | 访问 `http://localhost:8080/docs` | 端口转发后同左 |
| 端到端验证 | 用 SDK 创建沙箱并执行命令 | 见下方 REST API 验证 |

### 6.1 Kubernetes 端到端验证（REST API）

由于 SDK 存在兼容性问题（见 3.8 问题五），K8s 部署建议使用 REST API 进行端到端验证。以下命令均已在本地 Minikube 环境实际验证通过。

**第一步：创建沙箱**

```bash
curl -s -X POST http://127.0.0.1:8080/sandboxes \
  -H "Content-Type: application/json" \
  -d '{
    "image": {"uri": "ezone.kingsoft.com/ksyun/ai-app-docker/release/python:3.12-bookworm-slim-uv0.8-patched"},
    "timeout": 300,
    "resourceLimits": {"cpu": "0.5", "memory": "512Mi"},
    "entrypoint": ["tail", "-f", "/dev/null"]
  }'
# 返回示例：{"id": "99bff462-ff90-4033-aa5e-3b36969b878c", "status": {"state": "Allocated"}, ...}
```

> `image.uri` 需替换为 Minikube 中已存在的镜像（见 3.5 预加载步骤）。

**第二步：等待沙箱就绪**

```bash
SANDBOX_ID="<返回的沙箱 id>"
# 轮询直到 status.state 变为 "Running"
curl -s http://127.0.0.1:8080/sandboxes/$SANDBOX_ID | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['state'])"
```

**第三步：通过 proxy 执行命令**

沙箱内的 execd 守护进程监听 44772 端口，通过 Server 的 proxy API 转发调用：

```bash
# 执行 echo 命令
curl -s -X POST "http://127.0.0.1:8080/sandboxes/$SANDBOX_ID/proxy/44772/command" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo Hello from OpenSandbox on K8s!"}'
# 返回 SSE 格式：
# {"type":"stdout","text":"Hello from OpenSandbox on K8s!","timestamp":1775205348990}

# 执行 Python 计算
curl -s -X POST "http://127.0.0.1:8080/sandboxes/$SANDBOX_ID/proxy/44772/command" \
  -H "Content-Type: application/json" \
  -d '{"command": "python3 -c \"print(sum(range(101)))\"" }'
# {"type":"stdout","text":"5050","timestamp":...}

# 测试文件读写
curl -s -X POST "http://127.0.0.1:8080/sandboxes/$SANDBOX_ID/proxy/44772/command" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo test content > /tmp/test.txt && cat /tmp/test.txt"}'
# {"type":"stdout","text":"test content","timestamp":...}
```

**第四步：销毁沙箱**

```bash
curl -s -X DELETE http://127.0.0.1:8080/sandboxes/$SANDBOX_ID
# 返回 204 No Content
```

### 6.2 自动化验证脚本

将以下脚本保存为 `test_opensandbox.py`，可一键完成健康检查、沙箱创建、命令执行和清理的端到端验证。

前置依赖：`uv pip install httpx`

```python
"""OpenSandbox Minikube 部署验证测试脚本（基于 REST API）"""

import asyncio
import httpx

SERVER_URL = "http://localhost:8080"
SANDBOX_IMAGE = "ezone.kingsoft.com/ksyun/ai-app-docker/release/python:3.12-bookworm-slim-uv0.8-patched"
EXECD_PORT = 44772


def parse_sse_stdout(text: str) -> str:
    """从 execd SSE 响应中提取 stdout 输出"""
    import json
    lines = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "stdout":
                lines.append(event["text"])
        except json.JSONDecodeError:
            continue
    return "\n".join(lines)


async def test_health_check(client: httpx.AsyncClient):
    """测试 1: Server 健康检查"""
    resp = await client.get(f"{SERVER_URL}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
    print("[PASS] Server 健康检查通过")


async def wait_sandbox_ready(client: httpx.AsyncClient, sandbox_id: str, timeout: int = 180):
    """轮询等待沙箱就绪"""
    import time
    start = time.time()
    state = "Unknown"
    while time.time() - start < timeout:
        resp = await client.get(f"{SERVER_URL}/sandboxes/{sandbox_id}")
        status = resp.json()["status"]
        state = status["state"]
        if state == "Running":
            return True
        if state in ("Failed", "Terminated"):
            raise RuntimeError(f"沙箱进入异常状态: {state} - {status.get('message', '')}")
        await asyncio.sleep(3)
    raise TimeoutError(f"沙箱 {sandbox_id} 在 {timeout}s 内未就绪，最后状态: {state}")


async def exec_command(client: httpx.AsyncClient, sandbox_id: str, command: str) -> str:
    """通过 server proxy 调用 execd 执行命令，返回 stdout"""
    resp = await client.post(
        f"{SERVER_URL}/sandboxes/{sandbox_id}/proxy/{EXECD_PORT}/command",
        json={"command": command},
        timeout=30,
    )
    assert resp.status_code == 200, f"命令执行失败: {resp.text}"
    return parse_sse_stdout(resp.text)


async def test_create_and_run(client: httpx.AsyncClient):
    """测试 2: 创建沙箱并执行命令"""
    resp = await client.post(f"{SERVER_URL}/sandboxes", json={
        "image": {"uri": SANDBOX_IMAGE},
        "timeout": 300,
        "resourceLimits": {"cpu": "0.5", "memory": "512Mi"},
        "entrypoint": ["tail", "-f", "/dev/null"],
    })
    assert resp.status_code == 202, f"创建失败: {resp.text}"
    sandbox_id = resp.json()["id"]
    print(f"[PASS] 沙箱创建成功, id={sandbox_id}")

    try:
        await wait_sandbox_ready(client, sandbox_id)
        print("[PASS] 沙箱已就绪 (Running)")

        stdout = await exec_command(client, sandbox_id, "echo 'Hello from OpenSandbox on K8s!'")
        assert "Hello from OpenSandbox on K8s!" in stdout
        print(f"[PASS] 命令执行成功, stdout={stdout}")

        stdout = await exec_command(client, sandbox_id, "python3 -c \"print(sum(range(101)))\"")
        assert "5050" in stdout
        print(f"[PASS] Python 执行成功, 1+2+...+100={stdout}")

        stdout = await exec_command(client, sandbox_id, "echo 'test content' > /tmp/test.txt && cat /tmp/test.txt")
        assert "test content" in stdout
        print("[PASS] 文件读写成功")

        stdout = await exec_command(client, sandbox_id, "uname -a")
        print(f"[INFO] 沙箱系统信息: {stdout}")

    finally:
        resp = await client.delete(f"{SERVER_URL}/sandboxes/{sandbox_id}")
        print(f"[PASS] 沙箱已销毁 (status={resp.status_code})")


async def test_list_sandboxes(client: httpx.AsyncClient):
    """测试 3: 列出沙箱（确认清理干净）"""
    resp = await client.get(f"{SERVER_URL}/sandboxes")
    assert resp.status_code == 200
    sandboxes = resp.json()
    print(f"[PASS] 沙箱列表查询成功, 当前活跃沙箱数={len(sandboxes)}")


async def main():
    print("=" * 50)
    print("OpenSandbox Minikube 部署验证")
    print("=" * 50)

    async with httpx.AsyncClient(timeout=60) as client:
        tests = [
            ("健康检查", test_health_check),
            ("创建沙箱并执行命令", test_create_and_run),
            ("沙箱列表查询", test_list_sandboxes),
        ]

        passed, failed = 0, 0
        for name, test_fn in tests:
            print(f"\n--- {name} ---")
            try:
                await test_fn(client)
                passed += 1
            except Exception as e:
                print(f"[FAIL] {name}: {e}")
                failed += 1

    print(f"\n{'=' * 50}")
    print(f"结果: {passed} 通过, {failed} 失败")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
```

运行方式：

```bash
# 确保端口转发已建立
kubectl port-forward svc/opensandbox-server 8080:80 -n opensandbox-system &

# 执行测试
python test_opensandbox.py
```

预期输出：

```
==================================================
OpenSandbox Minikube 部署验证
==================================================

--- 健康检查 ---
[PASS] Server 健康检查通过

--- 创建沙箱并执行命令 ---
[PASS] 沙箱创建成功, id=99bff462-ff90-4033-aa5e-3b36969b878c
[PASS] 沙箱已就绪 (Running)
[PASS] 命令执行成功, stdout=Hello from OpenSandbox on K8s!
[PASS] Python 执行成功, 1+2+...+100=5050
[PASS] 文件读写成功
[INFO] 沙箱系统信息: Linux 99bff462-...-0 6.12.76-linuxkit ...
[PASS] 沙箱已销毁 (status=204)

--- 沙箱列表查询 ---
[PASS] 沙箱列表查询成功, 当前活跃沙箱数=...

==================================================
结果: 3 通过, 0 失败
==================================================
```

> 使用前需将 `SANDBOX_IMAGE` 替换为 Minikube 中实际可用的镜像。

---

## 7. Volume 挂载

OpenSandbox 原生支持在创建沙箱时挂载外部存储（OSEP-0003），避免每次重新上传文件。

### 7.1 API

`POST /v1/sandboxes` 的请求体中通过 `volumes` 数组指定挂载：

```json
{
  "image": {"uri": "python:3.11"},
  "timeout": 600,
  "volumes": [
    {
      "name": "workdir",
      "host": {"path": "/data/sessions/abc123"},
      "mountPath": "/mnt/work",
      "subPath": "task-001",
      "readOnly": false
    }
  ]
}
```

每个 volume 项包含：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | DNS-label 标识符，sandbox 内唯一 |
| `mountPath` | 是 | 容器内挂载路径（绝对路径） |
| `readOnly` | 否 | 默认 `false` |
| `subPath` | 否 | 后端路径下的相对子目录 |
| 后端（三选一） | 是 | `host` / `pvc` / `ossfs`，互斥 |

### 7.2 三种后端

| 后端 | 字段 | Docker | K8s | 适用场景 |
|------|------|--------|-----|---------|
| `host` | `host.path` | bind mount | hostPath | 单节点 / 开发环境 |
| `pvc` | `pvc.claimName` | Docker named volume | PersistentVolumeClaim | 多节点 K8s 生产环境 |
| `ossfs` | `ossfs.bucket`, `ossfs.endpoint`, `ossfs.accessKeyId`, `ossfs.accessKeySecret` | Linux FUSE | 计划 CSI | 阿里云 OSS 对象存储 |

### 7.3 安全控制

- **Host path 白名单**：服务端配置 `storage.allowed_host_paths` 限制可挂载的宿主机路径前缀
- **Path traversal 防护**：`subPath` 拒绝 `..` 和绝对路径
- **PVC 需预创建**：不会自动创建 Docker volume 或 K8s PVC
- **OSSFS 注入防护**：bucket 名称和 endpoint 会校验 shell 元字符

### 7.4 Python SDK 示例

```python
from opensandbox.api.lifecycle.models.volume import Volume
from opensandbox.api.lifecycle.models.host import Host
from opensandbox.api.lifecycle.models.pvc import PVC

sandbox = await Sandbox.create(
    "python:3.11",
    timeout=timedelta(minutes=10),
    volumes=[
        Volume(name="workdir", host=Host(path="/data/user"), mount_path="/mnt/work"),
        Volume(name="models", pvc=PVC(claim_name="shared-models"),
               mount_path="/mnt/models", read_only=True),
    ],
)
```

> 相关设计文档：`oseps/0003-volume-and-volumebinding-support.md`

---

## 8. 生命周期管理

### 8.1 状态机

```
Pending → Running → Stopping → Terminated
              ↓          ↑
           Pausing    (kill / TTL 到期)
              ↓
           Paused → Running (resume)

任意状态 → Failed（遇到不可恢复错误时）
```

状态通过 `GET /sandboxes/{id}` 返回的 `status` 对象获取：

```json
{
  "state": "Running",
  "reason": "user_delete | ttl_expiry | provision_timeout | runtime_error",
  "message": "...",
  "lastTransitionAt": "2026-04-08T10:00:00Z"
}
```

### 8.2 TTL 与续期

**创建时设置 TTL：**

```json
{"timeout": 600}
```

到期后自动销毁。Docker 模式使用 `threading.Timer` 调度，K8s 模式由 controller 根据 `spec.expireTime` 调谐。

**续期 API：**

```bash
POST /sandboxes/{id}/renew-expiration
Content-Type: application/json

{"expiresAt": "2026-04-08T11:00:00Z"}
```

- 续期次数无限制
- 约束：新过期时间必须在当前之后，且不超过 `max_sandbox_timeout_seconds`（服务端配置）
- Docker 模式下会取消旧 Timer 并创建新的
- K8s 模式下更新 `spec.expireTime`，controller 自动调谐

**自动续期（OSEP-0009 Renew-on-Access）：**

创建沙箱时设置扩展参数 `access.renew.extend.seconds`（范围 300~86400），每次访问沙箱时自动延期指定秒数。通过 ingress 网关 + Redis renew intent 队列实现。

### 8.3 Pause/Resume（K8s，OSEP-0008，implementing）

| 操作                            | 说明                                                                       |
| ----------------------------- | ------------------------------------------------------------------------ |
| `POST /sandboxes/{id}/pause`  | rootfs 快照为 OCI 镜像 → 推送至 registry → 删除原 Pod。状态：Running → Pausing → Paused |
| `POST /sandboxes/{id}/resume` | 从快照镜像创建新 Pod，`sandboxId` 不变。状态：Paused → Resuming → Running               |


适用于请求间隔较长的场景，释放计算资源但保留文件系统状态。Docker 模式下 pause/resume 仅冻结 cgroup（非快照），返回 501。

### 8.4 注意事项

- **无空闲检测**：sandbox 不会因为没有命令执行就自动回收，只有 TTL 到期或主动 DELETE 才会销毁
- **无 webhook 回调**：sandbox 终止时没有主动通知，需轮询 `GET /sandboxes/{id}` 检查状态
- **手动清理模式**：创建时设 `SANDBOX_MANUAL_CLEANUP_LABEL` 可跳过 TTL，必须主动删除
- **失败 sandbox 清理**：Pending 状态失败的 sandbox 默认 1 小时后自动清理（`PENDING_FAILURE_TTL` 环境变量控制）

---

## 9. 参考链接

- 项目地址：https://github.com/alibaba/OpenSandbox
- 配置详解：`server/configuration.md`
- Helm 部署文档：`kubernetes/docs/HELM-DEPLOYMENT.md`
- 常见问题：`server/TROUBLESHOOTING.md`
