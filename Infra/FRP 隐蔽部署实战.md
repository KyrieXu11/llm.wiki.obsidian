---
tags:
  - infra
  - network
  - frp
  - tunnel
  - reverse-proxy
  - websocket
created: "2026-04-24"
aliases:
  - FRP wss 部署
  - FRP 伪装
---

# FRP 隐蔽部署实战

## 概述

把 [fatedier/frp](https://github.com/fatedier/frp) 包装成"一个普通 HTTPS 站点 + 一个看起来无关的系统服务"，让它在腾讯云这类**会主动检测代理协议**的国内 VPS 上存活，且不暴露 FRP 典型特征。

场景：家里 → 公司 Mac（穿透公司网络限制），用 wss over 8443 做入口，证书走 DNS-01。

## 最终架构

```
家 PC → TCP → VPS:8443 (TLS 1.3 + ALPN http/1.1)
                 ↓
               nginx 反代
                 ├─ location /                  → 伪装静态页
                 └─ location /api/v1/stream     → 127.0.0.1:7700 (frps WebSocket)
                                                    ↓
                                                 frps (别名 app-sync)
                                                    ↓
                                                  STCP 隧道
                                                    ↓
                                             公司 Mac frpc → SSH:22
```

关键点：
- **入口只有 443 以外的一个 TLS 端口**（8443 或你喜欢的其它），伪装成 HTTPS
- 国内 VPS 不能用 80/443（未备案域名被拦）
- frps 走 nginx 反代的 WebSocket 路径，前面的 TLS 是正规 Let's Encrypt 证书
- frps/frpc 的 WebSocket 默认路径 `/~!frp` 带特征，**必须改源码**

## 为什么选 wss + nginx

| 方案 | 端口 | 被动特征 | 适合国内 VPS |
|---|---|---|---|
| 直连 `tcp` | 7000 | FRP 明文头，PoC 一扫就中 | 否 |
| `tls` | 7000 | 看起来像 TLS 但 0x17 首字节异常 | 一般 |
| `quic` | UDP | UDP 在某些 IDC 直接被限速 | 否 |
| **`wss` + Nginx** | 443/8443 | **和普通 HTTPS 站点一致** | ✅ |
| REALITY/trojan | 443 | 零信任强伪装 | 腾讯云上会被探测封 |

## VPS 端部署

### 1. 签证书（acme.sh + Cloudflare DNS-01）

80/443 在国内未备案 VPS 不可用，必须走 DNS-01。

```bash
# 装 acme.sh
curl https://get.acme.sh | sh
export CF_Token="你的 Cloudflare API Token"  # 权限：Zone.DNS:Edit 对应 zone
export CF_Account_ID="..."

# 通配符证书，一张顶所有子域
acme.sh --issue --dns dns_cf \
  -d example.com \
  -d '*.example.com' \
  --server letsencrypt

# 安装到 nginx 路径
acme.sh --install-cert -d example.com \
  --key-file       /etc/ssl/frp/key.pem \
  --fullchain-file /etc/ssl/frp/cert.pem \
  --reloadcmd      "nginx -s reload"
```

⚠️ `'*.example.com'` **必须单引号**，否则 shell 展开。通配符不含根域 `example.com`，两个 `-d` 都要写。

自动续期 acme.sh 安装时默认注册 cron（`crontab -l | grep acme` 可查）。

### 2. frps 配置（`config.toml`）

```toml
bindAddr = "127.0.0.1"           # 只监听本地，nginx 是唯一入口
bindPort = 7700
proxyBindAddr = "0.0.0.0"        # ⚠️ 必须！对外代理端口绑所有网卡

webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "admin"
webServer.password = "<32 字节随机>"

log.to = "/var/log/app-sync/frps.log"
log.level = "warn"
log.maxDays = 7

allowPorts = [
  { start = 10000, end = 11000 }
]

# ⚠️ [section] 块必须在所有顶级字段之后
[auth]
method = "token"
token = "<64 字节 hex 随机>"
```

**TOML 坑**：`[auth]` 必须在最后。如果把 `allowPorts = [...]` 写在 `[auth]` 后面，TOML 解析器会把它当成 `auth.allowPorts`，报 `json: unknown field "allowPorts"`。

**bindAddr vs proxyBindAddr**：
- `bindAddr` = frpc 控制连接进入的地址（设 127.0.0.1 让 nginx 独占入口）
- `proxyBindAddr` = 对外 proxy 端口绑定地址（**不设会跟 bindAddr 一样**，导致 10022 等 proxy 端口只监听 127.0.0.1，外网打不进来）

### 3. 改 frp 源码消除 WebSocket 路径特征

`pkg/util/net/websocket.go`：

```go
const (
    // FrpWebsocketPath = "/~!frp"   // ← 默认值，典型特征
    FrpWebsocketPath = "/api/v1/stream"  // 改成任意正常样子的路径
)
```

改完交叉编译（macOS → Linux amd64）：
```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -trimpath -ldflags "-s -w" \
  -tags "frps,noweb" -o /tmp/app-sync ./cmd/frps
```

`-trimpath` 去掉路径信息，`-s -w` 去符号。

### 4. 进程/文件伪装

| 维度 | 原版 | 伪装后 |
|---|---|---|
| 二进制名 | `frps` | `app-sync` |
| 路径 | `/usr/local/bin/frps` | `/opt/app-sync/bin/app-sync` |
| 运行用户 | root | 专用低权限账号 `app-sync` |
| systemd unit | `frps.service` | `app-sync.service` |
| 日志目录 | `/var/log/frps/` | `/var/log/app-sync/` |

`app-sync.service` systemd 硬化示例：

```ini
[Unit]
Description=Application Sync Service
After=network.target

[Service]
Type=simple
User=app-sync
Group=app-sync
ExecStart=/opt/app-sync/bin/app-sync -c /opt/app-sync/conf/config.toml
Restart=on-failure
RestartSec=5

# 硬化
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/log/app-sync
AmbientCapabilities=CAP_NET_BIND_SERVICE   # 只在需要绑 <1024 时加

[Install]
WantedBy=multi-user.target
```

### 5. Nginx 配置

```nginx
server {
    listen 8443 ssl http2;
    server_name app-sync.example.com;

    ssl_certificate     /etc/ssl/frp/cert.pem;
    ssl_certificate_key /etc/ssl/frp/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    access_log /var/log/nginx/frp_access.log;

    # 伪装首页：放一个真实的静态页，被扫到时返回正常内容
    location / {
        root /var/www/html;
        index index.html;
    }

    # frp 的 WebSocket 接入点
    location /api/v1/stream {
        proxy_pass http://127.0.0.1:7700;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_read_timeout    86400s;
        proxy_send_timeout    86400s;
        proxy_buffering       off;
    }
}

# 建议再加一个默认 server，直 IP 扫到时静默关闭
server {
    listen 8443 ssl default_server;
    ssl_certificate     /etc/ssl/frp/cert.pem;
    ssl_certificate_key /etc/ssl/frp/key.pem;
    return 444;   # nginx 专用：直接断连
}
```

### 6. 腾讯云防火墙

轻量服务器有**两层防火墙**，控制台面板 + 机器 iptables。控制台默认只开 22/80/443，自定义端口必须手动加：

| 协议 | 端口 | 用途 |
|---|---|---|
| TCP | 8443 | wss 入口 |
| TCP | <随机端口> | 每个公开 proxy 一个 |

⚠️ **端口号不要选 10022** 这类明显（22+prefix）的值，容易被针对性扫描。用 `jot -r 1 10000 11000` 生成随机端口。

## 客户端 frpc

### 配置

```toml
serverAddr = "app-sync.example.com"
serverPort = 8443
loginFailExit = false   # 默认 true，一次失败就退出；调试时关掉

[auth]
method = "token"
token = "<与 frps 一致>"

[transport]
protocol = "wss"

[[proxies]]
name = "mac-ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 10133
```

### 开机自启（macOS LaunchAgent）

选 LaunchAgent 而不是 LaunchDaemon 的理由：
- **不需要 sudo**，权限最小化
- 工位 Mac 基本永远保持登录状态（锁屏 ≠ 登出），GUI 上下文一直存在
- LaunchAgent **不会**被 SSH 登录二次触发——它只在 `gui/<uid>` 上下文加载，SSH 走的是 `user/<uid>` 上下文
- Label 单实例，launchd 天然去重

plist 路径：`~/Library/LaunchAgents/com.xuqiang.app-sync.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xuqiang.app-sync</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/xuqiang/code/frp/bin/frpc</string>
        <string>-c</string>
        <string>/Users/xuqiang/code/frp/bin/frpc.toml</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/xuqiang/code/frp/bin</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/Users/xuqiang/Library/Logs/app-sync.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/xuqiang/Library/Logs/app-sync.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
```

关键字段：
- `RunAtLoad = true` — 加载立刻跑
- `KeepAlive = true` — 进程退出自动重启
- `ThrottleInterval = 10` — 重试最小间隔 10s，防配置错误导致的 CPU 飙升
- `ProcessType = Background` — 告诉 macOS 这是后台服务
- `WorkingDirectory` — 没这个 frpc 找不到 `frpc.toml` 相对路径

### LaunchAgent 服务 CRUD

| 操作                       | 命令                                                                                   |
| ------------------------ | ------------------------------------------------------------------------------------ |
| **加载（首次）**               | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xuqiang.app-sync.plist` |
| **查看状态**                 | `launchctl list \| grep app-sync` — 三列：PID、上次退出码（0=正常）、Label                         |
| **查看详情**                 | `launchctl print gui/$(id -u)/com.xuqiang.app-sync`                                  |
| **查看日志**                 | `tail -f ~/Library/Logs/app-sync.log`                                                |
| **重启**（改 frpc.toml 用）    | `launchctl kickstart -k gui/$(id -u)/com.xuqiang.app-sync`                           |
| **临时停**（KeepAlive 会立刻拉起） | `launchctl kill SIGTERM gui/$(id -u)/com.xuqiang.app-sync`                           |
| **彻底卸载**（开机不自启）          | `launchctl bootout gui/$(id -u)/com.xuqiang.app-sync`                                |
| **改完 plist 重新加载**        | `launchctl bootout ...` 再 `launchctl bootstrap ...`（launchd 不会 auto-reload plist）    |

说明：
- `bootstrap` / `bootout` 是现代 launchctl（10.11+），替代老的 `load -w` / `unload -w`
- `kickstart -k` 等价于先 kill 再 start，一条命令搞定重启
- 设了 `KeepAlive = true` 的服务**不能用 kill 停住**，launchd 会立刻重拉；要真停必须用 `bootout`

### LaunchAgent 常见问题

- **改了 plist 不生效**：必须 `bootout` 后再 `bootstrap`，launchd 不会自动 reload
- **启动后立刻挂**：`launchctl list | grep LABEL` 第二列退出码非 0 就是崩溃；日志在 `StandardErrorPath` 指定的文件里
- **plist 不被识别**：权限必须 `644`，owner 必须当前用户。如果从别处复制带了 `_w` xattr，用 `xattr -c *.plist` 清掉
- **同一用户 SSH 登录会不会重复启动**：不会。LaunchAgent 只在 `gui/<uid>` 生效，SSH 进的是 `user/<uid>`。即便有歧义，launchd 按 Label 去重，单实例
- **改端口后 10022 还在监听**：是 frps 那边的 proxy 端口没释放，`launchctl kickstart -k` 触发 frpc 重连后，frps 会清理旧 proxy、绑新端口

### SSH 加固：禁用密码登录

frpc 把 Mac 的 22 端口映射到 VPS 上的公开端口（上例 10133），暴露给公网就会被爬。默认 `PasswordAuthentication yes` + 简单密码 = 被暴破。只留公钥登录是最小代价的加固。

**前提检查**：

1. 家里那台能连的机器（PC/手机/iPad）要**已经**把公钥加到 `~/.ssh/authorized_keys`
2. 对应私钥在那台机器上存在且可用
3. 改完配置后**先别关当前 SSH 会话**，新开一个窗口测通了再关，防止把自己锁外面

**配置**（用 drop-in，不动 `sshd_config` 本体）：

```bash
# drop-in 文件（按字典序加载，200-* 会覆盖系统默认的 100-*）
sudo tee /etc/ssh/sshd_config.d/200-no-password.conf > /dev/null <<'EOF'
# Disable password and keyboard-interactive; only publickey allowed.
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF

# 语法校验（失败就别重启）
sudo sshd -t

# 确认生效值
sudo sshd -T | grep -iE "^(passwordauth|kbdinteractive|pubkeyauth)"
# 期望输出：
#   passwordauthentication no
#   kbdinteractiveauthentication no
#   pubkeyauthentication yes

# 重启 sshd（macOS 方式，和 Linux 的 systemctl 不同）
sudo launchctl unload /System/Library/LaunchDaemons/ssh.plist
sudo launchctl load   /System/Library/LaunchDaemons/ssh.plist
```

**关键决策点**：

- **只关 `PasswordAuthentication` 不够**：macOS 默认 `UsePAM yes`，PAM 会通过 `KbdInteractiveAuthentication` 绕过第一条继续允许密码。必须两条都设 `no`
- **不要关 `UsePAM`**：PAM 还负责 session setup（Touch ID sudo 等功能），只需禁止 PAM 的**密码方式**即可

**验证禁用生效**：

```bash
ssh -o PreferredAuthentications=password -p 10133 xuqiang@app-sync.example.com
# 期望：Permission denied （说明密码登录真被禁了）
```

**回退**（万一锁外面）：

```bash
# 在 Mac 物理键盘前：
sudo rm /etc/ssh/sshd_config.d/200-no-password.conf
sudo launchctl unload /System/Library/LaunchDaemons/ssh.plist
sudo launchctl load   /System/Library/LaunchDaemons/ssh.plist
```

### VPS 端 systemd CRUD（frps）

对应的 systemd unit 见 [[#4-进程文件伪装]]。CRUD 对比：

| 操作 | macOS (LaunchAgent) | Linux (systemd) |
|---|---|---|
| 启动 | `launchctl bootstrap gui/$UID <plist>` | `systemctl enable --now app-sync` |
| 停止 | `launchctl bootout gui/$UID/<label>` | `systemctl disable --now app-sync` |
| 重启 | `launchctl kickstart -k gui/$UID/<label>` | `systemctl restart app-sync` |
| 状态 | `launchctl list \| grep <label>` | `systemctl status app-sync` |
| 日志 | `tail -f ~/Library/Logs/app-sync.log` | `journalctl -u app-sync -f` |
| 看是否开机自启 | `ls ~/Library/LaunchAgents/` | `systemctl is-enabled app-sync` |

## 踩过的坑速查

### 坑 1：WebSocket 测试返回 403，但 frpc 能连

`golang.org/x/net/websocket` 的 Handler **强制要求 Origin 请求头**，缺了就拒绝。curl 测试必须带：
```bash
curl --http1.1 -kv https://host:8443/api/v1/stream \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Origin: https://host:8443"            # 关键
```
真实 frpc 客户端永远会带 Origin，不会遇到这问题。

### 坑 2：curl 返回 HTTP/2 400 Bad Request

nginx 配 `listen 8443 ssl http2`，curl 通过 ALPN 协商到 HTTP/2，但 WebSocket 升级（RFC 6455）只在 HTTP/1.1 下合法。HTTP/2 里 `Connection: Upgrade` 不是有效头。

解法：**curl 加 `--http1.1`**。真实 frpc 自己就是 HTTP/1.1，不受影响。

### 坑 3：10022 在 VPS 上监听成 `127.0.0.1:10022`，外网打不进

frps 没设 `proxyBindAddr`，默认继承 `bindAddr = 127.0.0.1`。加一行：
```toml
proxyBindAddr = "0.0.0.0"
```

### 坑 4：nginx 403 但 error_log 没内容

日志里 `log_format` 默认合并，还要看每个 server 块独立定义的 `access_log/error_log`。分辨方法：
```bash
tail /var/log/nginx/access.log        # 主日志
tail /var/log/nginx/frp_access.log    # server 块专属
```
如果你的 server 块独立写日志，主 access.log 里看不到对应请求。

### 坑 5：Nginx 返回 403 + 5 字节 body

三种可能：
1. `server_name` 没改，Host 头不匹配 → 请求被默认 server 截走
2. `location /` 的 `root` 目录不存在/无读权限
3. **代理层的上游**（frps）返回了 403（见坑 1 的 Origin 问题）

排查顺序：
```bash
# A. 确认 server_name 匹配
nginx -T | grep -E "server_name|listen.*8443"

# B. 看哪条 server 匹配了（独立日志 vs 主日志）
tail /var/log/nginx/frp_access.log

# C. 跳过 nginx 直接测上游
curl -v http://127.0.0.1:7700/api/v1/stream -H "Origin: ..." ...
```

### 坑 6：frpc "i/o timeout" 连不上 VPS

先差分测试：
```bash
nc -vz VPS_IP 22      # 已知开的（做 baseline）
nc -vz VPS_IP 8443    # 目标端口
```

22 通、8443 超时 → 腾讯云防火墙没放行。**不是** nginx 或 frps 问题。

### 坑 7：sed 替换 TOML 中特殊字符

TOML 的值用双引号包住。base64 凭证含 `/` 会把默认 sed 分隔符搞坏。用 `|` 作分隔符更稳：
```bash
sed -i "s|token = \".*\"|token = \"$NEW|\"" config.toml
```
更好的做法是直接用 hex，没有特殊字符。

## 进阶：STCP 去掉公网代理端口

上面的 tcp proxy 会在 VPS 上开一个对外端口（10133），任何人扫到都能尝试 SSH——这仍然是额外攻击面。

换成 **STCP (Secret TCP)**：
- VPS 上不再开任何代理端口
- 家里也跑 frpc 当 "visitor"，用共享密钥从 wss 隧道内部获取目标
- 对外只剩 8443，完全像个普通 HTTPS 站

Mac 端配置改为：
```toml
[[proxies]]
name = "mac-ssh"
type = "stcp"
secretKey = "<随机 32 字节>"
localIP = "127.0.0.1"
localPort = 22
# 不再需要 remotePort
```

家里 PC 端（新装 frpc）：
```toml
serverAddr = "app-sync.example.com"
serverPort = 8443
[auth]
method = "token"
token = "<token>"
[transport]
protocol = "wss"

[[visitors]]
name = "mac-ssh-visitor"
type = "stcp"
serverName = "mac-ssh"         # 必须与服务端 proxy 名一致
secretKey = "<同一把密钥>"
bindAddr = "127.0.0.1"
bindPort = 22133               # 本机监听
```

家里用：`ssh -p 22133 xuqiang@127.0.0.1`

认证变成两层：**frp secretKey + SSH 公钥**。

## 速查命令

```bash
# VPS 上看 frps 状态
systemctl status app-sync
journalctl -u app-sync -f

# 看 frps 监听了哪些端口
ss -tlnp | grep app-sync

# 看 nginx 合并后的配置
nginx -T 2>&1 | grep -A 30 "listen.*8443"

# 端到端回环测试（带 Origin）
curl --http1.1 -kv --resolve host:8443:127.0.0.1 \
  https://host:8443/api/v1/stream \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Origin: https://host:8443"
# 期望：HTTP/1.1 101 Switching Protocols

# 本机 frpc 看连接状态
tail -f /tmp/frpc.log
# 成功标志：
# login to server success
# proxy added: [mac-ssh]
# start proxy success
```

## 相关页面

- [[FRP 协议层特征]] — FRP 默认流量在线上有哪些可识别特征
- [[Docker 网络模式与旁路由]]
- [[STUN 实现原理]] — 另一种穿透思路（P2P NAT 打洞）

## 参考资料

- https://github.com/fatedier/frp
- https://gofrp.org/zh-cn/docs/
- RFC 6455 - WebSocket Protocol
- https://github.com/acmesh-official/acme.sh/wiki/dnsapi#dns_cf
