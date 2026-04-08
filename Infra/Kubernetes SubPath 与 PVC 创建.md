---
tags:
  - infra
  - kubernetes
  - storage
created: "2026-04-08"
aliases:
  - K8s SubPath 与 PVC
  - Kubernetes 存储挂载基础
---

# Kubernetes SubPath 与 PVC 创建

## 结论卡片

- `subPath` 是 Kubernetes `volumeMounts` 的原生能力，用来挂载 volume 的某个子目录
- `PVC` 是 Pod 申请持久化存储的标准方式
- 对多轮 chat / sandbox 场景，优先推荐：**共享 PVC + `subPath=session 目录`**
- 不推荐：**每个 session 一个 PVC**

## `subPath`

### 是什么

- 一个 volume 的子目录挂载能力
- 适合“共享大盘，按目录隔离”的场景

### 解决什么

- 目录隔离
- 文件复用
- sandbox 重建后重新挂回原目录

### 不解决什么

- 并发写冲突
- session 生命周期管理
- 强隔离租户边界

### 最小示例

```yaml
volumes:
  - name: session-data
    persistentVolumeClaim:
      claimName: agent-session-data

containers:
  - name: app
    volumeMounts:
      - name: session-data
        mountPath: /mnt/work
        subPath: sessions/session-123
```

语义：容器里的 `/mnt/work` 对应 PVC `agent-session-data` 下的 `sessions/session-123`。

## PVC

### 相关对象

- `PV`：底层存储资源
- `PVC`：应用申请存储
- `StorageClass`：动态供给存储的规则

### 创建方式

#### 1. 动态创建 PVC

最常见，通常最推荐。

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: agent-session-data
  namespace: opensandbox
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 20Gi
  storageClassName: nfs-client
```

适合：

- 集群已有可用 `StorageClass`
- 共享 PVC 作为平台基础设施

#### 2. 静态创建 PV + PVC

先建 PV，再建 PVC。

适合：

- 已知底层存储位置
- 需要自己控制后端存储
- 自建 NFS / CephFS / local PV

#### 3. 通过 Helm 创建 PVC

适合：

- 把共享 PVC 当部署基础设施统一管理
- 环境初始化时一次性创建

#### 4. 业务代码动态创建 PVC

能做，但通常不建议细化到每个 session。

更适合：

- tenant 级 PVC
- 业务池级 PVC

## 选择建议

### 推荐

- 一个或少量共享 PVC
- `subPath = sessions/<session_id>`
- session 结束后删目录，不删 PVC

### 不推荐

- 每个 `session_id` 一个 PVC

原因：

- PVC/PV 数量线性增长
- 控制面和存储系统压力更大
- 清理和回收复杂

## 创建 PVC 时重点看什么

### AccessModes

- `RWO`：通常单节点读写
- `RWX`：多节点读写
- `ROX`：多节点只读

如果 Pod 可能跨节点调度，优先确认后端是否支持 `RWX`。

### StorageClass

- 云盘类很多只支持 `RWO`
- NFS、CephFS 更常见 `RWX`

### Namespace

- PVC 和 Pod 必须在同一个 namespace

## 对 chat / sandbox 的建议

推荐结构：

```text
共享 PVC: agent-session-data
  sessions/<session_id>/
    input/
    workspace/
    output/
```

推荐流程：

1. 预创建共享 PVC
2. 按 `session_id` 计算 `subPath`
3. 创建 Pod / sandbox 时挂载同一个 PVC
4. session 结束后清理 `sessions/<session_id>/`

## 何时需要更强隔离

下面场景可考虑比 `subPath` 更强的存储隔离：

- 强多租户隔离
- 单 session 数据量很大
- 需要独立配额、审计、加密边界

即便如此，通常也优先考虑：**tenant 级 PVC + session 级 `subPath`**。
