---
aliases:
  - 基础设施
tags:
  - MOC
  - infra
---

# 基础设施

## 部署推理
- [[Deployment MOC|部署推理总览]]
- [[Flux 部署指南 (Forge)|FLUX.1 部署指南 (Forge)]]
- [[FLUX.2 部署指南 (ComfyUI)|FLUX.2 部署指南 (ComfyUI)]] — H20 全量 bf16，推荐方案
- [[OpenSandbox 架构]] — Server / execd / Ingress / Egress 四层架构详解
- [[opensandbox-deploy-guide|OpenSandbox 部署指南]]
- [[Kubernetes SubPath 与 PVC 创建]]
- [[Landlock LSM]]

## 网络
- [[Docker 网络模式与旁路由]]

## 内网穿透 / 隧道
- [[FRP 隐蔽部署实战]] — wss + Nginx 反代 + 证书 + 伪装，抗国内 VPS 主动检测的 FRP 部署
- [[FRP 协议层特征]] — FRP 默认流量在线上的可识别点（0x17 首字节、`/~!frp` 路径等）

## 实时通信 / NAT 穿透
- [[WebRTC 技术栈调研]] — 协议栈、SFU 对比、WHIP/WHEP、TURN 账单、2026 演进
- [[STUN 实现原理]] — 地址反射机制、XOR-MAPPED-ADDRESS、打洞、最小实现

## 相关页面
- [[Agents MOC|Agent 与工具]]
- [[Frameworks MOC|工具框架]]
