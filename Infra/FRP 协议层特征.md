---
tags:
  - infra
  - network
  - frp
  - fingerprint
  - dpi
created: "2026-04-24"
aliases:
  - FRP 特征
  - FRP 识别
---

# FRP 协议层特征

## 概述

[fatedier/frp](https://github.com/fatedier/frp) 在默认配置下，流量有多个层面可被识别。DPI/协议探测如果想精准阻断 FRP，抓这几个特征就行。这篇笔记列出完整的已知特征点，作为 [[FRP 隐蔽部署实战]] 的理论基础。

## 特征一：TLS 首字节 0x17

FRP 为了在一个端口同时处理「TLS+非 TLS」，设计了自定义 multiplexer：frpc 如果启用了 TLS 但**没关** `disableCustomTLSFirstByte`，会在 TLS ClientHello 前先发一个 `0x17` 字节作为标记。

**源码**：
- `pkg/util/net/tls.go:26` — `var FRPTLSHeadByte = 0x17`
- `pkg/util/net/tls.go:41-55` — 服务端 `CheckAndEnableTLSServerConnWithTimeout` 用 switch 分流 `0x17`（FRP 自定义）vs `0x16`（标准 TLS）
- `pkg/util/net/dial.go:15` — 客户端 hook 写入这个首字节

```
标准 TLS ClientHello：    0x16 0x03 0x01 ...
FRP 自定义 TLS：         0x17 0x16 0x03 0x01 ...   ← 异常的前置字节
```

标准 TLS 记录类型里 `0x17` 是 **Application Data**，但握手还没完成就出现 Application Data 从协议上讲不合法，扫描器看到这个立刻就能识别。

**关闭方法**：客户端 `transport.disableCustomTLSFirstByte = true`。

这个字段对应 `pkg/config/v1/client.go:173` 的 `DisableCustomTLSFirstByte *bool`。

## 特征二：WebSocket 路径 `/~!frp`

frpc 用 wss 协议时，连接的是**硬编码路径** `/~!frp`：

**源码**：`pkg/util/net/websocket.go:15`
```go
const (
    FrpWebsocketPath = "/~!frp"
)
```

这个路径里的 `~!` 明显不是任何正常 Web 应用会用的，上游 IDS 规则可以直接命中。

**缓解**：改源码常量并重编译。没有运行时配置项能改它。

## 特征三：控制信道明文 `privilege_key`

frpc 登录时发 `Login` 消息，带 `privilege_key`（token 的 MD5 哈希结果）。虽然传输层可能有 TLS，但在**未启用 TLS 的直连 tcp 部署**里，这个字段就是明文。

**源码**：
- `pkg/msg/msg.go:76` — `Login` struct 定义，带 `privilege_key,omitempty` JSON tag
- `pkg/util/util/util.go:50` — `GetAuthKey = hex(md5(token || timestamp))`

```go
type Login struct {
    Version      string
    Hostname     string
    Os           string
    Arch         string
    User         string
    PrivilegeKey string `json:"privilege_key,omitempty"`   // ← 明文字段名
    Timestamp    int64
    RunID        string
    ...
}
```

DPI 直接 grep 报文里的 `"privilege_key"` 字符串就能识别。即使改成 tls 协议，也要注意**控制连接建立时的握手**有没有被完整加密。

**缓解**：
- 用 `tls` 或 `wss` protocol，让控制信道走加密通道
- 不用 token 方式鉴权（但 oidc 方式也有自己的特征）
- 最彻底的是改 struct 字段名 + 重编译（侵入性大）

## 特征四：TLS CipherSuites 无自定义

FRP 用 Go 标准库默认的 TLS 配置，`CipherSuites` 没有改过。这导致 JA3 指纹比较固定——使用 Go TLS 栈的服务很多（gRPC、各种 API server），单凭 JA3 不一定能精准识别 FRP，但和上面几条联合起来就是强特征。

**源码**：`pkg/transport/tls.go` 没有 `CipherSuites` 字段。

**缓解**：
- 套一层 nginx 反代，让真正暴露给公网的是 nginx 的 TLS 栈，JA3 变成 OpenSSL 的
- 或用 [utls](https://github.com/refraction-networking/utls) 模仿 Chrome（需要改源码）

## 特征五：默认端口约定

文档和常见教程习惯用：
- `7000` — 控制端口（bindPort）
- `7500` — dashboard
- `6000-7000` — 被代理的远程端口

扫到 7000 + TLS + 0x17 首字节基本就石锤了。

**缓解**：换端口。

## 特征六：bindAddr / proxyBindAddr 默认行为

frps 默认 `bindAddr = 0.0.0.0`，控制端口对外开放。即使端口换了，扫描器也能发现"有个端口的 TLS 行为异常"。

**缓解**：
- `bindAddr = 127.0.0.1`，只让 nginx 访问
- `proxyBindAddr = 0.0.0.0`（如果需要公网 tcp proxy）

## 特征七：Web Dashboard 路径

frps/frpc 的 web dashboard 静态资源和 API 有固定路径（`/api/*`、`/static/*`、特定 JS 文件名）。如果暴露到公网会立刻被指纹识别。

**源码**：`cmd/frps` 和 `cmd/frpc` 会嵌入 `web/frps/dist` 和 `web/frpc/dist`。

**缓解**：
- 编译时 `-tags noweb` 剥离 web 资源（`make frps` 默认会根据是否有 `web/frps/dist` 自动加）
- dashboard 只绑 127.0.0.1，通过 SSH 隧道转发访问

## 特征八：二进制字符串特征

`strings ./frps` 能看到：
- `fatedier/frp`（Go 模块路径）
- `/~!frp`（默认 WebSocket 路径，改过源码后消失）
- `privilege_key`（协议字段名）
- `FrpWebsocketPath` / `FRPTLSHeadByte` 等符号（通过 `-trimpath -ldflags "-s -w"` 可去掉部分）

**缓解**：
- 编译用 `-trimpath -ldflags "-s -w"`
- 换二进制名、路径（见 [[FRP 隐蔽部署实战#4-进程文件伪装]]）
- 用 upx 压缩（但 upx 壳本身是另一个特征，取舍）

## 特征汇总表

| # | 特征 | 缓解难度 |
|---|---|---|
| 1 | TLS 首字节 0x17 | ⭐ 配置项 |
| 2 | WebSocket 路径 `/~!frp` | ⭐⭐ 改源码 |
| 3 | 明文 `privilege_key` | ⭐ 用 tls/wss |
| 4 | Go 默认 JA3 | ⭐⭐ nginx 反代 |
| 5 | 默认端口 7000 | ⭐ 换端口 |
| 6 | bindAddr 对外 | ⭐ 配置项 |
| 7 | dashboard 资源 | ⭐ noweb 构建 |
| 8 | 二进制字符串 | ⭐⭐ 改源码 + 改名 |

## 分级防御模型

- **L0（零伪装）**：裸 tcp，明文 token，默认端口 —— 扫一下就中
- **L1（简单伪装）**：tls protocol + 换端口 + 关 0x17 —— 抗扫描，不抗主动探测
- **L2（反代伪装）**：wss + nginx + 正规证书 + 改 WebSocket 路径 —— 抗常规主动探测
- **L3（彻底伪装）**：L2 + STCP 不暴露代理端口 + utls 改 JA3 + 改 struct 字段 —— 对抗有针对性检测
- **L4（协议替换）**：放弃 FRP，改用 REALITY/tuic/hysteria —— 但国内机房上 REALITY 会被主动封

实践上 L2 已经足够应付公司网络 DPI；**国内 VPS 不要上 L4**（腾讯云/阿里云都会主动封 trojan/vmess/reality）。

## 相关页面

- [[FRP 隐蔽部署实战]] — 具体部署步骤
- [[STUN 实现原理]] — 另一种思路：P2P 穿透不需要中继服务器

## 参考资料

- 源码 tag：`github.com/fatedier/frp@master`
- [博客：frp 流量伪装思路](https://tonwork.fun/blog/)
- RFC 6455 - WebSocket
- RFC 8446 - TLS 1.3
