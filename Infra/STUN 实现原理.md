---
tags:
  - infra
  - network
  - stun
  - nat
  - rtc
created: "2026-04-20"
aliases:
  - STUN
  - Session Traversal Utilities for NAT
---

# STUN 实现原理

## 概述

STUN (Session Traversal Utilities for NAT, RFC 8489) 的核心就一句话：**让 NAT 后面的机器知道自己在公网上长什么样**。其他所有东西都是围着这个目标长出来的工程细节。

它是 [[WebRTC 技术栈调研|WebRTC]] ICE 框架的基石，也是 TeamViewer、Tailscale、游戏联机等一切 NAT 穿透场景的公共底层。

## 1. 地址反射：最基本的机制

```
  [Client 192.168.1.5:4444]
         │
         ▼
    [NAT: 外网 IP 203.0.113.7]
    NAT 创建映射条目：
      内：192.168.1.5:4444 ↔ 外：203.0.113.7:52678
         │
         ▼ (UDP 包，源地址被 NAT 改写为 203.0.113.7:52678)
  [STUN Server: stun.l.google.com:19302]
```

STUN 服务器做的事**极其简单**：
1. 收到 `Binding Request`
2. 看这个 UDP 包的**源地址 + 源端口**（就是 NAT 转换后的公网地址！）
3. 把这个地址塞进响应发回

客户端收到响应，读出 `XOR-MAPPED-ADDRESS`，就得到了自己的公网映射 (`203.0.113.7:52678`)。

> [!important] 关键设计
> STUN 服务器**无状态、无配置、不认识客户端**；它只是个"公网镜子"。因此 STUN 服务是**纯流量成本、极易部署**（coturn、STUNTMAN、Pion 自带的都行）。

## 2. 报文格式 (RFC 8489)

20 字节固定头 + 任意个 TLV 属性。

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|0 0|     STUN Message Type     |         Message Length        |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                         Magic Cookie  (0x2112A442)            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                                                               |
|                     Transaction ID (96 bits)                  |
|                                                               |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                     Attributes (TLV)   ...                    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### 2.1 Message Type (14 bit) 的奇怪编码

前两 bit 固定 `00`，剩下 14 bit 被编码成 **method (12 bit) + class (2 bit)**，但这 14 bit 并不是简单拼接——class 的两个 bit 分散在 14 bit 的第 4 位和第 8 位：

```
  0                 1
  0 1  2  3  4 5 6 7 8 9 0 1 2 3 4 5
 +--+--+--+--+--+--+--+--+--+--+--+--+--+--+
 |M |M |M |M |M |C |M |M |M |C |M |M |M |M |
 |11|10|9 |8 |7 |1 |6 |5 |4 |0 |3 |2 |1 |0 |
 +--+--+--+--+--+--+--+--+--+--+--+--+--+--+
```

Class 四种：
- `0b00` = Request
- `0b01` = Indication（不需要响应，TURN 的 Send/Data 用）
- `0b10` = Success Response
- `0b11` = Error Response

Method：`0x001` = Binding、`0x003` = Allocate (TURN)、`0x004` = Refresh ……

所以 `Binding Request` 的完整 Message Type = `0x0001`，`Binding Success Response` = `0x0101`。这个分散编码的设计是**为了兼容老 RFC 3489 编码的同时留扩展空间**。

### 2.2 Magic Cookie = `0x2112A442`

固定值，有两个作用：

**a. 多协议解复用**：WebRTC 场景下同一个 UDP 端口要同时收 STUN、DTLS、SRTP、RTCP 包。怎么区分？
- 第一字节：`0x00-0x03` = STUN、`0x14-0x17` = DTLS、`0x80-0xBF` = SRTP/RTCP
- 光看第一字节会冲突，所以 STUN 还要校验 offset 4-7 是不是 `0x2112A442`——双重确认

```python
def classify_packet(pkt):
    b = pkt[0]
    if b < 2 and pkt[4:8] == b'\x21\x12\xA4\x42':
        return 'STUN'
    if 20 <= b <= 63:
        return 'DTLS'
    if 128 <= b <= 191:
        return 'RTP' if is_rtcp_pt(pkt) else 'RTCP'
```

**b. 区分 RFC 3489 vs RFC 5389+**：老 RFC 3489 的 128 bit Transaction ID 里前 32 bit 是随机的；新版把前 32 bit 固化为 Magic Cookie。收到报文就能判断对方是不是老实现。

### 2.3 Transaction ID (96 bit)

客户端随机生成，三个用途：
1. **匹配响应**：多个并发请求用 TxID 区分
2. **反 ID 伪造**：服务器响应必须回填同一个 TxID
3. **参与 XOR**（IPv6 地址编码）和 **参与 HMAC**（如果有 MESSAGE-INTEGRITY）

## 3. 为什么要 `XOR-MAPPED-ADDRESS` 而不是明文 IP？

这是 STUN 设计里最精巧的一笔。

### 问题背景：NAT 的 ALG 会改写 payload

早期很多 NAT 设备带 **应用层网关 (ALG)**——它会扫描经过的 UDP/TCP 包，如果 payload 里出现看起来像内网 IP 的四字节，就把它改写成 NAT 映射后的外网 IP。这是为了让 FTP、SIP 等协议在 NAT 后能工作（因为它们会把 IP 写在协议字段里）。

现在问题来了：STUN 服务器想把"你的公网 IP:port"填到响应里发回——但这个 IP 恰好会被客户端这侧的 NAT ALG 认出来（因为它匹配了 NAT 表里的外网地址），然后**改写成内网 IP**。客户端拿到的就是自己的内网 IP，完全失去 STUN 的意义。

### 解决：XOR 伪装

RFC 5389 引入 `XOR-MAPPED-ADDRESS`：把 IP 和端口跟 Magic Cookie (IPv4) 或 Cookie+TxID (IPv6) 做 XOR 再塞进报文。

- **NAT ALG 看见 payload，扫不到 IP 模式**（XOR 后的字节是乱的）
- 客户端收到后自己 XOR 回来，得到真实地址
- 老的 `MAPPED-ADDRESS` 属性保留但**不再使用**，纯历史兼容

XOR 计算：

```python
MAGIC_COOKIE = 0x2112A442

def xor_addr(family, port, ip, txid):
    x_port = port ^ (MAGIC_COOKIE >> 16)      # 端口和 Cookie 的高 16 bit XOR
    if family == 1:  # IPv4
        x_ip = ip ^ MAGIC_COOKIE              # IP 和完整 Cookie XOR
    else:            # IPv6: 跟 Cookie ++ TxID 拼接后 XOR
        key = MAGIC_COOKIE.to_bytes(4) + txid  # 16 bytes
        x_ip = bytes(a ^ b for a, b in zip(ip, key))
    return x_port, x_ip
```

> [!info] 经典工程适配
> 这是一个经典的"对协议侵入性的适配"——不改 NAT 设备、不靠任何标准，单靠一个 XOR 就绕开了。

## 4. 认证

### 4.1 Short-term credential (WebRTC 用)

ICE 协商时双方交换 **ufrag + password**（通过信令通道，比如 SDP 的 `a=ice-ufrag` / `a=ice-pwd`）。后续的 STUN Binding 请求：

- 属性 `USERNAME` = `remote-ufrag:local-ufrag`
- 属性 `MESSAGE-INTEGRITY` = HMAC-SHA1(pwd, 整个报文到当前 attr 之前的字节)
- 可选 `MESSAGE-INTEGRITY-SHA256` (RFC 8489 新增，推荐)
- `FINGERPRINT` (CRC32 ^ `0x5354554E`) 放在最末尾，让多协议解复用时二次确认

> [!warning] HMAC 覆盖范围
> HMAC 计算时**报文头的 Length 字段要先改成"包含到 MESSAGE-INTEGRITY 为止"的长度**，但 FINGERPRINT 属性不包含进来——否则验证时先算 HMAC 又要扣除自己占的字节，逻辑乱套。

### 4.2 Long-term credential（传统 TURN 服务器用）

首次请求不带认证 → 服务器返回 `401 Unauthorized` + `REALM` + `NONCE` → 客户端重试，`USERNAME` + `REALM` + `NONCE` + MESSAGE-INTEGRITY（key = MD5(username:realm:password)）。

`NONCE` 防重放；`REALM` 用来隔离不同"域"。TURN 自建时你通常配的是 `static-auth-secret`——那其实是生成 ephemeral credential 的 HMAC key，不是直接密码。

## 5. NAT 类型检测：RFC 3489 vs RFC 5389

### RFC 3489 的经典四分类
- **Full Cone**：内网 A 给任何外网发包，都用同一映射；任何外网可以回包
- **Address-Restricted Cone**：只有之前发过包的外网 IP 才能回（端口不限）
- **Port-Restricted Cone**：必须 IP + 端口都匹配才能回
- **Symmetric**：给不同外网地址发包，映射不同（外部看到的端口会变）

RFC 3489 定义了 `CHANGE-REQUEST` 属性让 STUN 服务器从**不同 IP/端口**发响应来推断 NAT 类型。

### 为什么 RFC 5389 废弃了这种检测

实际 NAT 行为非常不规律：
- 很多 NAT 是**混合行为**：轻载时 Cone、高载时 Symmetric
- 有些 NAT 在**端口用完后回退**到 Symmetric
- 企业级 NAT/CGNAT 的映射算法非标
- RFC 3489 的探测只反映测试那一瞬间的状态，测完下一秒就变

**实际工程做法**：**不做 NAT 分类，直接 ICE 跑候选配对**。ICE 对所有 NAT 类型统一处理：收集所有候选 → 两端做连通性检查 → 通了就用，没通就回退 TURN。比起猜 NAT 类型，直接试成功率更高。

## 6. STUN 如何实现"打洞"

STUN 本身不打洞，是**发送 STUN 请求的副作用**打了洞。

```
Client A (NAT A)             Client B (NAT B)
   │                              │
   │── STUN Binding Req ──────────│ (B 发给 A 的公网地址)
   │   (A 的 NAT：没见过这个源    │
   │    地址，丢包 ← RESTRICTED)  │
   │                              │
   │── STUN Binding Req ──────────│── (A 同时也发)
   │                              │   A 的 NAT 记录：
   │                              │     出站到 B.pub → 允许入站
   │                              │
   │  ← STUN Req from B ──────────│ (B 第二次发过来)
   │     现在 A 的 NAT 放行！      │
```

核心：**双方同时往对方公网地址发 UDP**，各自的 NAT 都会创建"出站会话"条目。只要双方几乎同步，两端的入站包都能穿过对方的 NAT。

**Symmetric NAT 为什么打不通**：A 给 B 发 STUN 请求，NAT 分配端口 `P1`（这个映射只对 B.pub 有效）；B 收到请求看到 A 的外部端口是 `P1`。但 B 往 A 发包时，走的是 **STUN 服务器**告诉的 A 的"到 STUN 服务器那一侧的映射端口 `P0`"（因为 B 从 STUN 服务器学到的是 A 的 `P0`）。`P0 ≠ P1`，所以包发过去 A 的 NAT 没对应入站条目，丢弃。

Symmetric + Port-Restricted 的组合是最难穿的场景；RFC 7712 (**Port-Prediction**) 尝试预测下一个端口，但成功率不稳，实务里基本投降用 TURN。

## 7. 从零实现一个最小 STUN Client

以下 Python 代码能跑，无依赖：

```python
import socket, struct, os, sys

MAGIC = 0x2112A442

def build_binding_request():
    msg_type = 0x0001                    # Binding Request
    msg_len  = 0                         # 无 attributes
    txid     = os.urandom(12)
    return struct.pack('>HHI', msg_type, msg_len, MAGIC) + txid

def parse_xor_mapped(attr_value, txid):
    # attr_value: 1B reserved + 1B family + 2B x-port + 4B x-ip (IPv4)
    _, family, x_port = struct.unpack('>BBH', attr_value[:4])
    port = x_port ^ (MAGIC >> 16)
    if family == 0x01:  # IPv4
        x_ip = struct.unpack('>I', attr_value[4:8])[0]
        ip = x_ip ^ MAGIC
        return f"{(ip>>24)&255}.{(ip>>16)&255}.{(ip>>8)&255}.{ip&255}:{port}"
    else:  # IPv6
        key = struct.pack('>I', MAGIC) + txid
        x_ip = attr_value[4:20]
        ip = bytes(a ^ b for a, b in zip(x_ip, key))
        return f"[{socket.inet_ntop(socket.AF_INET6, ip)}]:{port}"

def parse_response(pkt, txid):
    msg_type, msg_len, magic = struct.unpack('>HHI', pkt[:8])
    assert magic == MAGIC, 'not a STUN packet'
    assert pkt[8:20] == txid, 'txid mismatch'
    
    i = 20
    while i < 20 + msg_len:
        attr_type, attr_len = struct.unpack('>HH', pkt[i:i+4])
        val = pkt[i+4:i+4+attr_len]
        if attr_type == 0x0020:          # XOR-MAPPED-ADDRESS
            return parse_xor_mapped(val, txid)
        i += 4 + attr_len + (-attr_len & 3)  # 4-byte align padding

def main(server='stun.l.google.com', port=19302):
    req = build_binding_request()
    txid = req[8:20]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    sock.sendto(req, (server, port))
    resp, _ = sock.recvfrom(2048)
    print("Public address:", parse_response(resp, txid))

if __name__ == '__main__':
    main()
```

运行：
```
$ python stun_client.py
Public address: 203.0.113.7:52678
```

50 行代码能把整个 STUN 协议的 80% 跑起来。这就是为什么 Pion / aiortc 等非浏览器栈都能自己实现 STUN——它真的不复杂。

## 8. STUN 服务器实现要点

要写服务器也很简单（Go 示例，核心 30 行）：

```go
func handleSTUN(conn *net.UDPConn) {
    buf := make([]byte, 1500)
    for {
        n, addr, _ := conn.ReadFromUDP(buf)
        if n < 20 { continue }
        if binary.BigEndian.Uint32(buf[4:8]) != 0x2112A442 { continue }
        
        msgType := binary.BigEndian.Uint16(buf[0:2])
        if msgType != 0x0001 { continue }   // 只处理 Binding Request
        
        txid := buf[8:20]
        resp := buildXorMappedResponse(txid, addr.IP, addr.Port)
        conn.WriteToUDP(resp, addr)
    }
}
```

真正复杂的是：
- **高性能**（coturn 用 libevent/epoll，百万级 QPS）
- **认证**（长期凭证）
- **多协议共存**（TURN 在同一端口）
- **Anycast 部署**（Cloudflare/Google 的 STUN 都是 anycast）
- **速率限制**（防滥用 amplification 攻击——STUN 响应比请求大，理论上可做反射攻击，虽然放大倍数不大）

## 9. 安全与反滥用

### 9.1 放大攻击 (Amplification Attack)
STUN Binding 响应比请求稍大（多了 XOR-MAPPED-ADDRESS 属性约 12 字节）。攻击者伪造源 IP = 受害者 IP，向 STUN 服务器喷请求，响应会堆到受害者。虽然放大倍数很小（约 1.5x），但公网 STUN 部署时：
- 限速（每 IP QPS 限制）
- 只响应符合格式的请求
- 禁用 `CHANGE-REQUEST` 等可能放大响应的属性（RFC 5389 已建议禁用）

### 9.2 FINGERPRINT 反多协议串扰
同端口跑 STUN + DTLS + SRTP 时，理论上可以构造一个 DTLS/SRTP 包恰好头字节长得像 STUN。`FINGERPRINT` 属性 (CRC32 ^ `0x5354554E`) 放在报文末尾，作为"这真的是 STUN 报文"的确认——没有它接收方可能误判。WebRTC 场景强制使用 FINGERPRINT。

## 10. 一句话总结

STUN = **一个无状态的"镜子服务"** + **一套精心设计的报文格式**（XOR 防 ALG 改写、Magic Cookie 多协议区分、TLV 属性可扩展）。它不直接做 NAT 穿透，但提供了**打洞所需的地址信息**，是 ICE 框架的基石。现代实现不做 NAT 类型检测，直接靠 ICE 候选配对 + TURN fallback 解决所有场景。

## 参考资料

- [RFC 8489 STUN (2020, 取代 RFC 5389)](https://datatracker.ietf.org/doc/html/rfc8489)
- [RFC 5389 STUN (历史版本)](https://datatracker.ietf.org/doc/html/rfc5389)
- [RFC 3489 STUN (已废弃，含 NAT 类型检测)](https://datatracker.ietf.org/doc/html/rfc3489)
- [RFC 8445 ICE](https://datatracker.ietf.org/doc/html/rfc8445)
- [RFC 8656 TURN](https://datatracker.ietf.org/doc/html/rfc8656)
- [coturn](https://github.com/coturn/coturn) — 业界标准 STUN/TURN 实现
- [Pion turn (Go)](https://github.com/pion/turn) — 纯 Go 实现

## 相关页面

- [[WebRTC 技术栈调研]] — STUN 所在的完整 WebRTC 协议栈
- [[Docker 网络模式与旁路由]] — NAT 相关
- [[Infra MOC|基础设施]]
