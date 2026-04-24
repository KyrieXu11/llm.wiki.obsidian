---
tags:
  - infra
  - network
  - webrtc
  - rtc
created: "2026-04-20"
aliases:
  - WebRTC
  - Real-Time Communication
  - RTC
---

# WebRTC 技术栈调研

## 概述

WebRTC (Web Real-Time Communication) 是 W3C/IETF 定的一套**浏览器端实时通信**标准，让两个浏览器（或 App）之间可以直接传音频、视频、任意数据，不用经过服务器中转。

三个核心 JS API：
- `getUserMedia` — 拿摄像头/麦克风流
- `RTCPeerConnection` — 建立 P2P 连接，负责编解码、NAT 穿透、拥塞控制
- `RTCDataChannel` — 任意二进制数据通道（P2P 版的 WebSocket）

底层依赖 [[STUN 实现原理|STUN]] / TURN / ICE 做 NAT 穿透，这套框架也被 TeamViewer、Tailscale、Parsec 等非浏览器产品复用。

## TL;DR（2026-04 现状）

- **WebRTC 1.0** 在 2025 年被 W3C 更新再次发布；"WebRTC-NV" 不是单体规范，而是 Insertable Streams、WebRTC-SVC、Encoded Transform 等一组扩展
- **WHIP (RFC 9725, 2025-03 正式)** 和 **WHEP** 在直播领域蚕食 RTMP，OBS 30.1+ 原生支持 WHIP
- **Aggressive Nomination 在 RFC 8445 被废弃**；新实现一律 Regular Nomination + Trickle ICE
- **AV1 实时编码**在屏幕共享路径落地（Meet/Zoom/Teams 都上了），摄像头路径硬编覆盖还不够
- **WebTransport + WebCodecs + MoQ** 是"替代 WebRTC"叙事的核心——但 Safari 零支持 WebTransport，二者会长期共存
- **对称 NAT 下 P2P 基本必须 TURN**；经验：无 TURN 约 85% 成功率，TURN over TLS/443 能拉到 99.5%+；约 **20-30% 会话走 TURN 中继**
- **TURN 账单**：Cloudflare Realtime 首 1TB 免费、后续 $0.05/GB；规模化是主要基础设施成本
- **SFU 主流**：mediasoup、LiveKit、Janus、Jitsi、Pion；Meta 2026 "Escaping the Fork" 揭示 libwebrtc fork 维护多难

## 1. 协议栈细节

### 1.1 ICE (RFC 8445 / 8838 / 8863)

**候选类型**：

| 类型 | 获取方式 | Priority 段 |
|------|---------|------------|
| Host | 本机网卡 | ~2130706431（最高） |
| srflx | STUN Binding | ~1694498815 |
| relay | TURN Allocate | ~16777215（最低） |
| prflx | 对端连通性检查时发现 | 动态 |

一条典型 ICE candidate：

```
candidate:842163049 1 udp 1677729535 198.51.100.7 52678 typ srflx
  raddr 192.168.1.5 rport 52678 generation 0 ufrag 8nC/ network-id 1
```

Priority 公式 (RFC 8445 §5.1.2)：
```
priority = (2^24)*type_pref + (2^8)*local_pref + (256 - component_id)
```

**连通性检查**：Controlling agent 发 STUN Binding Request 带 `ICE-CONTROLLING`，Controlled 带 `ICE-CONTROLLED`，冲突时靠 tiebreaker 裁决。**STUN 请求本身就是打孔**——这是 STUN 能穿 Full Cone/Restricted NAT 的本质。

**Nomination**：
- **Regular（默认）**：跑完检查，Controlling 对选定 pair 单独发一次带 `USE-CANDIDATE` 的 STUN "钦定"
- **Aggressive (RFC 5245，已废弃)**：每个检查都带 `USE-CANDIDATE`，第一个成功就是赢家——RFC 8445 明确废弃

**Trickle ICE**：候选不等收齐就边发边协商。现代栈几乎必开。

```javascript
pc.onicecandidate = (event) => {
  if (event.candidate) signaling.send({ type: 'candidate', candidate: event.candidate });
  else { /* null = end-of-candidates */ }
};
```

**ICE Restart**：网络切换 (WiFi↔4G)、链路退化、`iceConnectionState === 'failed'` 时用。新 ufrag/pwd 触发重新配对，但**不重建 DTLS/SRTP**，媒体连续性保持。

```javascript
const offer = await pc.createOffer({ iceRestart: true });
```

**NAT 穿透成功率（经验值）**：

| NAT 类型 | STUN only | STUN+TURN |
|---------|-----------|-----------|
| Full Cone | ~99% | ~99% |
| Port Restricted | ~85% | ~99% |
| Symmetric | ~0% | ~99% |
| 双方 Symmetric | **必须 TURN** | ~99% |

生产全量数据：无 TURN ~85% / 加 TURN+UDP ~97% / 加 TURN over TLS/443 ~99.5-99.8%；约 **20-30% 会话走 relay**。

### 1.2 STUN (RFC 5389 → RFC 8489)

详见 [[STUN 实现原理]]。

关键属性：
- **XOR-MAPPED-ADDRESS**：公网 `IP:port` 与 magic cookie `0x2112A442` XOR，规避老 NAT 对 payload 做 IP rewriting 的坑
- **MESSAGE-INTEGRITY**：HMAC-SHA1 或 SHA256（8489 新增）
- **FINGERPRINT**：CRC32，用于同端口多协议解复用（和 RTP 区分）
- **USERNAME/REALM/NONCE**：long-term credential 用

WebRTC 用 **ICE short-term credential**，ufrag 当 USERNAME，pwd 当 key。

### 1.3 TURN (RFC 5766 → RFC 8656)

**消息类型**：
- `Allocate` → 申请 relay 地址，返回 `XOR-RELAYED-ADDRESS`，默认 lifetime 600s
- `Refresh` → 续期
- `CreatePermission` → 白名单对端 IP（TURN 不是无条件转发）
- `Send/Data indication` → 封装 peer 数据（高开销）
- `ChannelBind/ChannelData` → 4 字节极精简帧头，替代 Send/Data

**TURN over TCP/TLS/443 的伪装** 是生产关键：
1. UDP/3478（默认）
2. TCP/3478 (RFC 6062)
3. **TLS/443**——看起来像 HTTPS，几乎所有防火墙放行
4. 极端场景还要走 HTTP CONNECT

> [!tip] 实战数据
> 某 telehealth 产品加 TURN over TLS 后连接成功率 72% → 99.8%。

**TURN 带宽账单 (2026)**：双向计费，1v1 视频 ~2 Mbps ≈ 900MB/人·小时

| 服务商 | 定价 |
|--------|------|
| Cloudflare Realtime TURN | 1TB/月免费，后 $0.05/GB |
| Xirsys | $10-500/月 阶梯 |
| Metered | 500MB 免费，$99/月起 |
| 自建 coturn (AWS/GCP) | egress ~$0.09/GB（**比托管更贵**） |

### 1.4 DTLS-SRTP (RFC 5763 / 5764)

**流程**：
1. ICE 通连通性
2. 同一 5-tuple 上跑 DTLS 握手（libwebrtc 已切 **DTLS 1.3 / RFC 9147**，部分老实现仍 1.2）
3. 交换自签证书，**比对 SDP 里的 `a=fingerprint` 和实际证书哈希**——防 MITM 的唯一机制（所以信令必须可信）
4. 握手完成后派生 SRTP key/salt

```
a=fingerprint:sha-256 62:2A:58:2F:9A:0B:1F:...
a=setup:actpass     # active/passive/actpass，决定 DTLS 角色
```

**Profile**：优先 `SRTP_AEAD_AES_128_GCM`（AEAD，省 HMAC 开销）。

**为什么不用 SDES**：老的 SDES 直接把 SRTP key 写在 SDP 里，信令被嗅探就破防。DTLS-SRTP 让密钥从不离开端点。

### 1.5 SCTP over DTLS (DataChannel, RFC 8831 / 8832 / 8841)

栈：`App → DataChannel API → SCTP → DTLS → UDP → IP`

三种可靠性模式：

```javascript
// 1. 可靠+有序（默认）
pc.createDataChannel('chat');

// 2. 无序，最多重传 3 次
pc.createDataChannel('game-state', { ordered: false, maxRetransmits: 3 });

// 3. 类 UDP：不重传
pc.createDataChannel('telemetry', { ordered: false, maxRetransmits: 0 });
```

`maxRetransmits` 和 `maxPacketLifeTime` 互斥，只能设一个。

**消息大小**：libwebrtc 默认最大 256KB；生产建议自己分块 16-64KB，避免跨实现兼容问题。

## 2. 媒体引擎

### 2.1 Codec 现状 (2026 Q1)

| Codec | 强制? | 硬解 | 硬编 | 实况 |
|-------|-------|------|------|------|
| **Opus** | ✓ | — | — | 6-510 kbps VBR，NB/WB/SWB/FB，DTX |
| G.711 | ✓ | — | — | 仅 PSTN 互通 |
| **VP8** | ✓ | 高 | 中 | 全支持 |
| **VP9** | 否 | 中高 | 低 | Safari 15.4+ 部分 |
| **H.264** | ✓（兜底） | 高 | 高 | iOS 强制首选 |
| **H.265** | 否 | 高 | 中 | Safari 17+ 实验，专利贵 |
| **AV1** | 否 | 2024+ 新设备高 | 低 | Meet/Zoom/Teams/Webex 屏共首选 |

**AI 语音编码**：
- **Lyra v2** (Google OSS, Apache 2.0)：3.2/6/9 kbps，但没进主流栈
- **Satin** (MS 专有)：Teams 内部用，6-17 kbps，未开源
- **现状：Opus 仍是 2026 事实标准**

### 2.2 拥塞控制

- **GCC (Google Congestion Control)**：接收端用 Kalman/trendline filter 估 OWD 梯度 + 发送端 loss-based，取小
- **Transport-CC / TWCC (RFC 8888)**：把拥塞信号收敛到发送端。每个包带 `transport-wide sequence number`，接收端 50-100ms 回一次 RTCP feedback 列出每个包的 arrival time。**所有媒体流共用一个估计器**，SFU 场景关键。Meta 2024 在这基础上上了 ML 估带宽

### 2.3 丢包恢复

| 机制 | 成本 | 场景 |
|------|------|------|
| **NACK** (RFC 4585) | 1 个 RTT | 低丢包首选 |
| **RTX** (RFC 4588) | 丢的包带宽 ×2 | 和 NACK 配 |
| **ULPFEC** (RFC 5109) | +20-50% | 固定延迟预算的音频 |
| **FlexFEC** (RFC 8627) | 类似灵活 | Chromium 部分启用 |
| **RED** (RFC 2198) | +一份 | Opus 音频常开 |
| **PLI** | 一个 I-frame | decoder state 破坏时 |

实战：**音频** Opus in-band FEC + RED + NACK；**视频** NACK+RTX 为主，PLI 保底。FEC 在 SFU 里非默认，因为转发时每一层要重算。

### 2.4 Simulcast vs SVC

**Simulcast**：同源编码多路不同分辨率/码率，独立 SSRC，SFU 选流。所有 codec 支持。

**SVC (Scalable Video Coding)**：一路编码内多层（空间+时域）。W3C webrtc-svc 规范定义 `scalabilityMode`：

```
L1T1       → 无 SVC
L1T3       → 1 空间层 × 3 时域层（15/30fps）
L3T3       → 3 × 3 = 9 子流
L3T3_KEY   → 每空间层关键帧独立
```

配置：
```javascript
sender.setParameters({
  encodings: [{ scalabilityMode: 'L3T3_KEY', maxBitrate: 2_500_000 }]
});
```

| 维度 | Simulcast | SVC |
|------|-----------|-----|
| 编码成本 | 高（N 路并行） | 低（1 路多层） |
| SFU 转发 | 简单 | 需解析 payload 头 |
| 质量切换 | 有 I-frame 抖动 | 平滑 |
| Codec 支持 | 所有 | VP9/AV1 原生，H.264 仅时域 |

> [!note] 现代实践
> `L1T3` 成本极低、生产默认开；`L3T3_KEY` + AV1 是 2024-2026 的组合拳——AV1 的 SVC 是规范内建 core feature，开销比 VP9 低。

### 2.5 Jitter Buffer

**NetEQ (libwebrtc 音频)**：自适应目标延迟（追踪 IAT 95% 分位）+ 时间伸缩（WSOLA 做 accelerate/decelerate）+ PLC（LPC 预测假帧）。

**视频 buffer**：等完整帧、追踪 render time (`abs-capture-time` RTP ext)、动态调整延迟。对话 ~30-80ms，WHEP 直播 200-500ms，云游戏 <10ms。

## 3. SDP 与协商

### 3.1 Offer/Answer (JSEP, RFC 8829)

```javascript
// Caller
pc.addTrack(audioTrack, stream);
pc.addTrack(videoTrack, stream);
const offer = await pc.createOffer();
await pc.setLocalDescription(offer);
signaling.send({ sdp: offer });

// Callee
await pc.setRemoteDescription(offer);
const answer = await pc.createAnswer();
await pc.setLocalDescription(answer);
signaling.send({ sdp: answer });
```

### 3.2 Unified Plan vs Plan B
- **Plan B（老）**：一个 `m=video` 段携带多个 SSRC
- **Unified Plan（标准）**：每个 track 一个 `m=` section，对应 `RTCRtpTransceiver`

**2026 现状：Plan B 已彻底死亡**，Chromium 2022 删除，所有现代实现只有 Unified Plan。

### 3.3 Transceiver

```javascript
const transceiver = pc.addTransceiver('video', {
  direction: 'sendrecv',
  streams: [stream],
  sendEncodings: [
    { rid: 'q', scaleResolutionDownBy: 4 },
    { rid: 'h', scaleResolutionDownBy: 2 },
    { rid: 'f' }
  ]
});
```

### 3.4 BUNDLE + rtcp-mux

典型 SDP：
```
a=group:BUNDLE 0 1 2              ← 音频+视频+data 共享一个传输

m=audio 9 UDP/TLS/RTP/SAVPF 111 103 104
a=rtcp-mux                        ← RTP+RTCP 同端口
a=mid:0
a=ice-ufrag:abc
a=ice-pwd:xyz
a=fingerprint:sha-256 ...
a=setup:actpass
a=rtpmap:111 opus/48000/2
a=rtcp-fb:111 transport-cc

m=video 0 UDP/TLS/RTP/SAVPF 96 97
a=mid:1
a=rtcp-mux
a=rtpmap:96 VP8/90000
a=rtcp-fb:96 nack pli
a=fmtp:97 apt=96                  ← 97 是 96 的 RTX

m=application 0 UDP/DTLS/SCTP webrtc-datachannel
a=mid:2
a=sctp-port:5000
a=max-message-size:262144
```

- **BUNDLE (RFC 9143)**：所有 m-line 共享一个 5-tuple，省端口+握手
- **rtcp-mux (RFC 5761)**：RTP/RTCP 同端口，生产一律开

### 3.5 Renegotiation 触发
- 加/删 track
- 改 transceiver direction
- ICE restart
- 切换 codec (`setCodecPreferences`)
- 首次加 DataChannel

JS 层通过 `onnegotiationneeded` 感知。多次 reneg 时用 **perfect negotiation pattern** 防竞态。

## 4. 架构模式

### 4.1 P2P Mesh
- N 人 = N(N-1)/2 连接
- 上行吃不消：N-1 路视频并发
- **上限 4-6 人**
- 唯一优势：无服务器、E2E 天然

### 4.2 SFU (Selective Forwarding Unit，主流)

**原理**：SFU 收每参与者 RTP，按需转发。**不解码不重编码**（成本低），但能做：路由、Simulcast 层选择、RTX/NACK 中继、带宽估计、E2EE 透明转发。

**主流对比 (2026)**：

| SFU | 语言 | 许可 | 特点 | 适用 |
|-----|------|------|------|------|
| **mediasoup** | Node + C++ worker | ISC | 最灵活，API 正交 | Node 生态，深度定制 |
| **Janus** | C | GPLv3 | 插件化 (videoroom/sip/streaming) | SIP 互通，老牌稳定 |
| **Jitsi Videobridge** | Java/Kotlin | Apache 2.0 | 开箱即用 + Jicofo | 企业内部，容易部署 |
| **LiveKit** | Go | Apache 2.0 | SFU+信令+SDK 全家桶，Cloud 版 | 快速产品、AI agent |
| **Pion**（库） | Go | MIT | 纯 Go 无 cgo，灵活度高 | IoT、自建 SFU |
| **ion-sfu** | Go (on Pion) | MIT | Pion 生态的 SFU | 想用 Go 又不要 LiveKit |
| **Medooze** | C++ | GPL | 支持 SVC、E2EE、录制 | 欧洲用得多 |

**选型经验**：
- Go 完全可控 → LiveKit 或 Pion 拼
- Node + 深度定制 → mediasoup（文档极差但最灵活）
- SIP/Gateway 混合 → Janus
- 快速上线开源 → Jitsi Meet + JVB

### 4.3 MCU (Multipoint Conferencing Unit，混流)
- 服务端**解码 → 合成布局 → 重编码**
- CPU 成本高：每路解码 + 合成 + 每个用户独立编码
- 现代只在两个场景用：
  1. 弱终端（老电话、SIP、RTMP 接收）——需拉单流
  2. 直播推流输出（合成推 CDN）
- 很多产品是**混合**：SFU 核心 + MCU worker 做录制/直播

### 4.4 Cascading / 多区域 SFU

单机房 SFU 带宽贵、延迟差。解决：**SFU 级联**——同一 room 跨多个 SFU 节点。

- **Jitsi Octo**（老牌）
- **LiveKit distributed mesh**（FlatBuffers 自定义协议在 SFU 间转发）
- **Cloudflare Calls** 极端案例：**anycast + 每 PoP 都是 SFU**，DTLS/ICE 走 anycast IP，BGP 决定连哪个 PoP，room 状态放 Durable Objects 全球同步——把 SFU cascading 做进了 edge network

## 5. 信令（不在标准内）

### 5.1 常见方案

| 方案 | 优缺 |
|------|------|
| WebSocket (wss) | 最通用，自研信令首选 |
| SIP over WebSocket (RFC 7118) | 成熟，能接 PSTN；历史包袱重 |
| Matrix | 去中心化，学习曲线陡 |
| MQTT | IoT 场景 |

### 5.2 WHIP (RFC 9725, 2025-03 正式发布)

**信令 = 一次 HTTP POST**：

```http
POST /whip/live/room123 HTTP/1.1
Host: ingest.example.com
Content-Type: application/sdp
Authorization: Bearer eyJhbGc...

<SDP offer>

──────
HTTP/1.1 201 Created
Content-Type: application/sdp
Location: /whip/resource/abcxyz    ← 后续用这个 URL DELETE
ETag: "123"

<SDP answer>
```

后续 ICE trickle 走 `PATCH` 同一 Location，`Content-Type: application/trickle-ice-sdpfrag` (RFC 8840)。结束用 `DELETE`。

**2025-2026 生态**：
- **OBS 30.1+** 原生支持 WHIP 推流
- Cloudflare Stream、Dolby.io、Wowza、Amazon IVS、字节 veLive 支持 ingest
- 典型链路：**OBS → WHIP → SFU → 转码 → HLS/LL-HLS → CDN**（WebRTC SFU 打不过 CDN 规模经济）

### 5.3 WHEP (draft-ietf-wish-whep)
对称设计，低延迟播放。OBS 还没做，但 Flussonic、GStreamer、Cloudflare、Red5 Pro 都实现了。

### 5.4 MoQ (Media over QUIC)
- 仍 IETF draft (moq-transport-04+)
- 想做 CDN 友好的低延迟 pub/sub
- **Safari 不支持 WebTransport → MoQ 浏览器端半瘸**
- 现状：WebRTC 仍是会话型标准，MoQ 可能吃掉 1-to-millions 低延迟广播，二者共存 10 年

## 6. 安全

### 6.1 Insertable Streams / Encoded Transform（真 E2EE）

**背景**：SFU 下 DTLS-SRTP 是 hop-by-hop，SFU 能看明文。

**方案**：SFU 之外再加一层帧级加密。

```javascript
// Worker 内
self.addEventListener('rtctransform', (event) => {
  const { readable, writable } = event.transformer;
  readable.pipeThrough(new TransformStream({
    transform(chunk, controller) {
      // chunk 是 RTCEncodedVideoFrame / RTCEncodedAudioFrame
      // 保留 codec metadata 明文，加密 payload
      chunk.data = encryptPayload(chunk.data, sharedKey);
      controller.enqueue(chunk);
    }
  })).pipeTo(writable);
});

// 主线程
sender.transform = new RTCRtpScriptTransform(worker, { role: 'encrypt' });
```

**SFrame** (IETF draft-ietf-sframe-enc) 是配套的帧级加密协议，设计上不破坏 SFU 转发的 simulcast/SVC metadata。已在 Jitsi、Cloudflare Orange Me2eets、Google Meet 落地。

**状态 (2026 Q1)**：
- Chromium：旧 `RTCInsertableStreams` 废弃，迁移到 worker-based `RTCRtpScriptTransform`
- Firefox 138+ 2025 实现
- Safari 17+ 基础支持

### 6.2 已知攻击面

| 攻击 | 缓解 |
|------|------|
| IP 泄露（candidate 暴露本机私有 IP） | **mDNS 候选**（Chrome 76+）：本地 IP 用 `<uuid>.local` 替代 |
| SDP 注入/信令 MITM | 强制 wss + 服务端校验 fingerprint |
| TURN relay 滥用当代理 | Short-term credential + 速率限制 + 按用户配额 |
| ICE flood DoS | candidate 数量上限（libwebrtc 默认 100） |

## 7. 近年演进 (2024-2026)

### 7.1 WebRTC-NV 状态

**2025 年**：W3C 再次发布 **WebRTC 1.0 Updated Recommendation**（不是 2.0）。

WebRTC-NV = 一组扩展：

| 扩展 | 状态 |
|------|------|
| webrtc-extensions | Working Draft |
| webrtc-svc | Candidate Rec |
| webrtc-encoded-transform | WD → CR |
| webrtc-stats | Recommendation 2025 |
| mediacapture-main | Recommendation 2025 Q1 |

Bernard Aboba (W3C WebRTC chair) 2024 年的定调：下一步是**不碎片化**——把 Insertable Streams、WebCodecs、WebTransport 组成完整替代栈，而不是 WebRTC 2.0。

### 7.2 WebCodecs + WebTransport 替代叙事

**组合场景**（云游戏、低延迟直播）：
```
Camera → getUserMedia → VideoEncoder (AV1) 
  → WebTransport datagrams/streams → Server 
  → WebTransport → VideoDecoder → Canvas/Video
```

**优势**：
- 协议栈更薄（不走 SCTP、不走 SFU 特定逻辑）
- 直接拿 QUIC 做中继
- 云游戏：直接硬解渲染 WebGL/WebGPU

**劣势**：
- 无 ICE/TURN（NAT 穿透自己搞）
- 无 TWCC 针对媒体调优的拥塞控制
- 无 E2EE 标准
- **Safari 零支持 WebTransport——跨浏览器产品死结**

**定位**：
- 双向会话（call/AI voice/agent）→ WebRTC
- 单向低延迟广播 → WebCodecs+WebTransport (+ MoQ)

### 7.3 AV1 实时编码
- 2024 年 Zoom/Meet/Teams/Webex 全上 AV1，但**主要在屏幕共享**路径（screen content tools 省 50%+）
- 摄像头路径：软编 CPU 3-5 倍于 VP9。硬编：Intel Arc (2023+) / Snapdragon 8 Gen 3+ / Apple M3+ / NVIDIA 40 系
- 消费级硬编普及约 2027
- Meet 的"隐藏 AV1 礼物"：低带宽下默认切 AV1，质量比 VP9 好明显

### 7.4 ML 能力

| 能力 | 做法 | API |
|------|------|-----|
| 噪声抑制 | RNNoise → WASM；或 Krisp/DeepFilterNet；Chrome M120+ 内置 ML NS | `noiseSuppression: true` |
| 回声消除 | libwebrtc AEC3；Teams/Zoom ML AEC | `echoCancellation: true` |
| 背景虚化 | MediaPipe Selfie Segmentation + Canvas；Chromium Media Effects 实验 | 无标准（**`backgroundBlur`** 2024 实验） |
| 超分 | NVIDIA Maxine / Intel RTX Video | 无 |

Chromium 2024-2025 引入 `MediaStreamTrack` enhancement (backgroundBlur, eyeContact, faceFraming)，走 OS API (macOS CoreImage / Windows Studio Effects)——**API 先行，算法 OS 级**。

## 8. 生产级实现与生态

### 8.1 libwebrtc 维护噩梦
- Google 参考实现，**唯一浏览器级完整栈**
- `https://webrtc.googlesource.com/src/`，和 Chromium 共用 base/build/depot_tools
- 问题：只发 Chrome release branch、无稳定 C++ API 承诺、依赖锁死 Chromium 主干
- **Meta 2026 "Escaping the Fork"** 博客：花几年把内部 fork 迁回 upstream，让 50+ 产品共享同一 libwebrtc。经验教训：**绝大多数团队扛不起 fork 的维护成本**
- 社区解决：LiveKit / Flutter-WebRTC 的软 fork——不改核心，定期 rebase

### 8.2 非浏览器实现

| 项目 | 语言 | 位置 |
|------|------|------|
| **Pion** | Go 纯，无 cgo | IoT、云游戏、SFU 后端首选；缺 codec 要外挂 |
| webrtc-rs | Rust | 基本照 Pion 结构重写 |
| aiortc | Python asyncio | 研究/爬虫/测试工具 |
| werift | TS/Node | 纯 TS 实现，和 pion/aiortc 互通 |
| libdatachannel | C++ | 只 DataChannel + ICE + DTLS，嵌入式/游戏 |
| GStreamer webrtcbin | C | Pipeline 风格，媒体服务器/IoT |

### 8.3 商业云服务 (2026)

| 厂商 | 定价 | 定位 |
|------|------|------|
| **LiveKit Cloud** | bandwidth based | 全家桶 + Agent framework |
| Agora | $0.99-3.99/1000min | 亚洲、SDK 最全 |
| **Twilio Video** | **2024-12-05 EOL** | 退出市场 |
| Daily | 分钟计费 | 开发者友好 |
| Vonage | 分钟/订阅 | 老牌 CPaaS |
| AWS Chime SDK | 分钟+attendee | 企业、AWS 集成 |
| 阿里云 RTC | 分钟 | 中国合规 |
| 腾讯云 TRTC | 分钟 | 中国社交/教育首选 |
| **Cloudflare Realtime** | TURN $0.05/GB | anycast 独一档 |

> [!warning] 市场信号
> Twilio 退出是信号：WebRTC PaaS 市场从"大厂都做"转向"要么深度集成要么专精"。

## 9. 工程痛点

### 9.1 对称 NAT 与 TURN 依赖
- ~10-20% 终端在对称 NAT 后（CGNAT、企业网关、某些移动运营商）
- T-Mobile/Verizon 大量 CGNAT，IPv6-only 下要 IPv6 STUN/TURN
- **ICE Restart 在移动切网时是关键**——没有它 WiFi↔4G 直接挂

### 9.2 TURN 账单（规模化）
假设 1000 万分钟/月视频，平均 1.5 Mbps 双向：
- 总 112.5 TB/月
- 25% 走 TURN = 28 TB → Cloudflare Realtime ≈ $1500/月
- 全走 TURN = 112 TB → $5600/月
- 自建 coturn AWS/GCP ≈ $10K+/月（egress 贵）

**省钱路径**：
1. 尽量 P2P (mDNS + IPv6 + 充分候选)
2. TURN anycast + 多区域（降延迟不降价）
3. 优先 TURN over UDP（省 handshake）
4. SFU 模式下让 SFU 直接公网可达，跳过 TURN relay 层

### 9.3 移动功耗
- 摄像头 + 编码 + 网络 300-700mAh/小时，1h 降 15-25% 电
- 省电：硬编 / 关视频 / 降分辨率 / iOS PiP / Android Foreground Service

### 9.4 Safari 坑点清单
- AV1 无硬编
- VP9 支持破碎，SDP 协商边界情况多
- Insertable Streams/ScriptTransform 落后 Chromium 1-2 年
- Screen Capture 只 Safari 17+ (macOS)，iOS 极有限
- 切后台暂停 getUserMedia，恢复要自己处理
- **iOS 17 之前所有浏览器都是 WebKit**，Chrome for iOS = Safari 水平
- DTLS 证书建议 ECDSA（某些老 Safari 对 RSA 处理差）

### 9.5 调试工具
- **`chrome://webrtc-internals`**：实时 PeerConnection 状态、每 SSRC bitrate/loss/jitter 图、ICE candidate pair 明细——**生产 bug 第一站**
- `about:webrtc`（Firefox）
- `getStats()`：标准化报告，Chrome legacy getStats 已废弃 (M127 彻底移除)

```javascript
const report = await pc.getStats();
for (const stat of report.values()) {
  if (stat.type === 'inbound-rtp' && stat.kind === 'video') {
    console.log({
      bytesReceived: stat.bytesReceived,
      packetsLost: stat.packetsLost,
      jitter: stat.jitter,
      framesPerSecond: stat.framesPerSecond,
      qpSum: stat.qpSum,          // 质量
      freezeCount: stat.freezeCount
    });
  }
}
```

生产监控栈：**getStats → WebRTCStats.js → Prometheus → Grafana**。

协议抓包：Wireshark + DTLS keylog 文件可以解密 SRTP——调协议 bug 必备。

## 10. 真实案例

### 10.1 会议 / 社交

| 产品 | WebRTC? | 架构 |
|------|---------|------|
| Google Meet | ✓（libwebrtc） | SFU + cascading，Meet 是 Chromium WebRTC 首要客户 |
| Zoom | 部分（web）；桌面 **自研 UDP** | SFU，自研协议延迟更好 |
| MS Teams | 桌面自研 SIP-like；web 端 WebRTC | MCU + SFU 混合 |
| Discord | **自研 C++ SFU**，WebRTC ICE/DTLS 定制 | 百万并发语音 |
| Slack Huddles | WebRTC（从 Chime 切过去） | audio-first |
| WhatsApp/Signal | ✓ | 1v1 P2P，group SFU |
| Telegram | 近几年 WebRTC | Voice Chat SFU |

### 10.2 云游戏

| 产品 | 协议 |
|------|------|
| Parsec | 自研 UDP (BUD)；浏览器端试验 WebRTC |
| GeForce NOW | Native NVIDIA GameStream；Web 端 WebRTC |
| Stadia（已关） | WebRTC 主力，libwebrtc 深度定制 |
| Moonlight | GameStream（非 WebRTC） |
| Shadow | WebRTC |

趋势：**WebCodecs + WebTransport** 是云游戏新势力，比 WebRTC 更贴近需求（直接 QUIC 堆）。

### 10.3 低延迟直播
- OBS 30.1+ 原生 WHIP
- Twitch 在试验 WHIP，主力仍 LL-HLS/RTMP
- Cloudflare Stream / 字节 veLive / Amazon IVS 都支持 WHIP
- 大型 OTT 仍 HLS/DASH，交互场景（连麦）切 WebRTC

### 10.4 P2P CDN
- PeerTube + WebTorrent 做 P2P 分发省 CDN 带宽
- 痛点：symmetric NAT 下 P2P ~60% 成功率；上行受限；版权复杂

## 附录：常用代码片段

### A. 建立 PeerConnection（生产配置）

```javascript
const pc = new RTCPeerConnection({
  iceServers: [
    { urls: 'stun:stun.l.google.com:19302' },
    { 
      urls: ['turn:turn.example.com:3478?transport=udp',
             'turn:turn.example.com:3478?transport=tcp',
             'turns:turn.example.com:443?transport=tcp'],
      username: 'ephemeral-user',
      credential: 'ephemeral-token'
    }
  ],
  iceCandidatePoolSize: 10,     // 预热 ICE
  bundlePolicy: 'max-bundle',
  rtcpMuxPolicy: 'require'
});

pc.oniceconnectionstatechange = () => {
  if (pc.iceConnectionState === 'failed') pc.restartIce();
};
```

### B. TURN short-term credential（服务端）

```javascript
function genTurnCred(secret, user, ttl = 3600) {
  const ts = Math.floor(Date.now() / 1000) + ttl;
  const username = `${ts}:${user}`;
  const hmac = require('crypto').createHmac('sha1', secret);
  hmac.update(username);
  return { username, credential: hmac.digest('base64') };
}
// coturn: use-auth-secret; static-auth-secret=<same-secret>
```

### C. Codec 优先级

```javascript
const caps = RTCRtpSender.getCapabilities('video');
const order = ['video/AV1', 'video/VP9', 'video/VP8', 'video/H264'];
const sorted = order.map(m => caps.codecs.find(c => c.mimeType === m)).filter(Boolean);
transceiver.setCodecPreferences(sorted);
```

## 结语：2026 年工程判断

1. **WebRTC 仍是双向实时会话事实标准**，短期无替代；WebRTC-NV 是渐进扩展不是大跃进
2. **低延迟直播分化中**：WHIP/WHEP 吃 RTMP，MoQ 吃 HLS；Safari 仍是 MoQ 最大瓶颈
3. **SFU 不会被淘汰**，但 Cloudflare Calls 证明 SFU 能云原生化——未来 SFU 更像 CDN 节点
4. **E2EE 真实可用**（SFrame + Encoded Transform），服务端信任模型迟早成为可选关闭项
5. **libwebrtc 依然是唯一完整参考实现**，Pion/Rust 非浏览器占比继续上升；Meta Escaping-the-Fork 是里程碑
6. **AV1 普及瓶颈是硬件**——2026-2028 才是全民 AV1
7. **WebRTC + LLM agents / 实时语音 agent / 实时翻译**是 2024-2026 最热方向，LiveKit/Daily/Agora 业务主要增长点在这

## 参考资料

### RFC / 规范
- [RFC 9725 WHIP](https://datatracker.ietf.org/doc/rfc9725/)
- [RFC 8445 ICE](https://datatracker.ietf.org/doc/html/rfc8445)
- [RFC 8838 Trickle ICE](https://www.rfc-editor.org/rfc/rfc8838)
- [RFC 8656 TURN](https://datatracker.ietf.org/doc/html/rfc8656)
- [RFC 5764 DTLS-SRTP](https://datatracker.ietf.org/doc/html/rfc5764)
- [RFC 8831 DataChannel](https://www.rfc-editor.org/rfc/rfc8831.html)
- [W3C WebRTC 1.0 Updated Rec (2025)](https://www.w3.org/news/2025/updated-w3c-recommendation-webrtc-real-time-communication-in-browsers/)
- [W3C webrtc-svc](https://www.w3.org/TR/webrtc-svc/)

### 工程博客
- [Meta "Escaping the Fork" (2026-04)](https://engineering.fb.com/2026/04/09/developer-tools/escaping-the-fork-how-meta-modernized-webrtc-across-50-use-cases/)
- [Meta ML Bandwidth Estimation](https://engineering.fb.com/2024/03/20/networking-traffic/optimizing-rtc-bandwidth-estimation-machine-learning/)
- [Cloudflare Calls anycast](https://blog.cloudflare.com/cloudflare-calls-anycast-webrtc/)
- [Cloudflare TURN anycast](https://blog.cloudflare.com/webrtc-turn-using-anycast/)
- [Orange Me2eets E2EE](https://blog.cloudflare.com/orange-me2eets-we-made-an-end-to-end-encrypted-video-calling-app-and-it-was/)
- [LiveKit distributed mesh](https://blog.livekit.io/scaling-webrtc-with-distributed-mesh/)
- [MoQ replacing WebRTC](https://moq.dev/blog/replacing-webrtc/)

### 实现
- [Pion WebRTC (Go)](https://github.com/pion/webrtc)
- webrtcHacks 系列（Bernard Aboba Q&A、AV1 in Meet、E2EE、Pion、Unified Plan 等）
- BlogGeek.me SFU 对比
- Meetecho 博客（SFrame、MoQ+WebRTC、AV1 simulcast）

## 相关页面

- [[STUN 实现原理]] — NAT 地址反射协议细节
- [[Docker 网络模式与旁路由]] — Docker 网络
- [[Infra MOC|基础设施]]
