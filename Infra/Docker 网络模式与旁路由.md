---
tags:
  - infra
  - docker
  - network
created: "2026-04-07"
aliases:
  - Docker Networking
  - macvlan
  - 旁路由
---

# Docker 网络模式与旁路由

## 概述

Docker 容器默认使用 bridge 网络，容器 IP 在虚拟网段内（如 `172.17.0.x`），外部设备无法直接访问。如果需要容器在局域网中拥有独立 IP（如旁路由场景），需要使用 macvlan 网络。

## Bridge vs Macvlan

### Bridge 模式（默认）

```
你家局域网 192.168.1.0/24
  └── 宿主机 192.168.1.100
         └── Docker bridge 172.17.0.0/16（独立虚拟网段）
               └── 容器 172.17.0.2
                     ↑ 局域网其他设备访问不到这个地址
```

- 容器在 Docker 内部虚拟网段，通过 NAT 访问外网
- 外部访问容器需要 `-p` 端口映射
- 容器没有局域网真实 IP

### Macvlan 模式

```
你家局域网 192.168.1.0/24
  ├── 宿主机  192.168.1.100 (MAC: aa:bb:cc:dd:ee:01)
  ├── 容器    192.168.1.2   (MAC: aa:bb:cc:dd:ee:02) ← 独立设备
  └── 投影仪  192.168.1.50  → 网关指向 192.168.1.2 ✓
```

- 物理网卡虚拟出多个独立 MAC 地址的子网卡
- 每个容器拿到局域网真实 IP，路由器看到的是独立设备
- 无需端口映射，局域网内直接可达

### 对比

| 特性 | Bridge | Macvlan |
|------|--------|---------|
| 容器 IP 网段 | Docker 内部（172.17.x.x） | 与宿主机同网段（192.168.1.x） |
| 局域网可见性 | 不可见，需端口映射 | 独立设备，直接可达 |
| MAC 地址 | 共享宿主机 | 独立 MAC |
| 适用场景 | 普通 Web 服务 | 旁路由、需要独立 IP 的服务 |
| 配置复杂度 | 低（默认） | 中（需手动创建网络） |

## Docker 旁路由实践

### 场景

用 Docker 跑 OpenWrt/iStoreOS 做旁路由，给投影仪等设备提供透明代理。

### 操作步骤

```bash
# 1. 开启网卡混杂模式
sudo ip link set eth0 promisc on

# 2. 创建 macvlan 网络（按实际网段修改）
docker network create -d macvlan \
  --subnet=192.168.1.0/24 \
  --gateway=192.168.1.1 \
  -o parent=eth0 macnet

# 3. 启动 OpenWrt 容器
docker run -d --restart always \
  --name openwrt \
  --network macnet \
  --ip 192.168.1.2 \
  --privileged \
  sulinggg/openwrt:x86_64
```

### 投影仪侧配置

手动设置网络：
- **IP**：`192.168.1.50`（或 DHCP）
- **网关**：`192.168.1.2`（指向旁路由）
- **DNS**：`192.168.1.2`

其他设备保持原样，不受影响。

### 注意事项

- 必须用 `--privileged`，容器需要操作网络栈
- macvlan 容器与宿主机之间**默认不能互通**，需要额外创建 macvlan bridge 接口解决
- 旁路由 IP 不要和 DHCP 地址池冲突
- N100 小主机跑旁路由 CPU 占用几乎为零

### macvlan 宿主机互通（可选）

```bash
# 宿主机创建 macvlan 子接口
sudo ip link add macvlan0 link eth0 type macvlan mode bridge
sudo ip addr add 192.168.1.101/32 dev macvlan0
sudo ip link set macvlan0 up
sudo ip route add 192.168.1.2/32 dev macvlan0
```

## 其他 Docker 网络模式

| 模式 | 说明 | 使用场景 |
|------|------|---------|
| `bridge` | 默认，NAT 隔离 | 大部分容器服务 |
| `host` | 共享宿主机网络栈 | 高性能网络、避免 NAT 开销 |
| `macvlan` | 独立 MAC + 局域网 IP | 旁路由、DHCP 服务器 |
| `ipvlan` | 共享 MAC、独立 IP | 类似 macvlan，MAC 地址受限时用 |
| `none` | 无网络 | 纯隔离容器 |
| `overlay` | 跨主机网络 | Docker Swarm / 集群 |

## 参考资料

- Docker 官方文档: [Networking overview](https://docs.docker.com/network/)
- [[Deployment MOC|部署推理]]
- [[Infra MOC|基础设施]]
