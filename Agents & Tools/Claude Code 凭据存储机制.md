---
tags:
  - agents
  - claude-code
  - security
created: "2026-04-07"
aliases:
  - Claude Code Credentials
  - Claude Code 登录存储
---

# Claude Code 凭据存储机制

## 概述

Claude Code 登录后将认证信息分两处存储：**OAuth 令牌**存入系统安全存储（macOS Keychain / Linux 文件），**账户元信息**存入配置文件。切换账号（如 [[cc-account-switcher|ccswitch]]）时需同时替换这两处。

## 存储结构

### 1. OAuth 令牌（敏感凭据）

**macOS**：存储在 Keychain 中

```
Service:  "Claude Code-credentials"
Account:  <系统用户名>
```

**Linux / WSL**：存储在文件中

```
~/.claude/.credentials.json    (权限 600)
```

内容结构（JSON）：

```json
{
  "claudeAiOauth": {
    "accessToken":      "sk-ant-oat01-***",
    "refreshToken":     "sk-ant-ort01-***",
    "expiresAt":        1775575162762,
    "scopes": [
      "user:file_upload",
      "user:inference",
      "user:mcp_servers",
      "user:profile",
      "user:sessions:claude_code"
    ],
    "subscriptionType": "team",
    "rateLimitTier":    "default_raven"
  }
}
```

| 字段                 | 说明                                          |
| ------------------ | ------------------------------------------- |
| `accessToken`      | API 访问令牌，前缀 `sk-ant-oat01-`，用于实际的 API 调用    |
| `refreshToken`     | 刷新令牌，前缀 `sk-ant-ort01-`，accessToken 过期后用此续期 |
| `expiresAt`        | accessToken 过期时间（Unix 毫秒时间戳）                |
| `scopes`           | 授权范围：推理、文件上传、MCP 服务器、用户资料、claude_code 会话    |
| `subscriptionType` | 订阅类型：`free` / `pro` / `team`                |
| `rateLimitTier`    | 速率限制等级，如 `default_raven`                    |

**读取命令**（macOS）：

```bash
security find-generic-password -s "Claude Code-credentials" -w
```

**写入命令**（macOS）：

```bash
security add-generic-password -U -s "Claude Code-credentials" -a "$USER" -w "$CREDENTIALS_JSON"
```

### 2. 账户元信息（非敏感配置）

存储在 `~/.claude.json`（部分版本为 `~/.claude/.claude.json`）：

```json
{
  "oauthAccount": {
    "accountUuid":          "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "emailAddress":         "user@example.com",
    "organizationUuid":     "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "organizationName":     "Org Name",
    "organizationRole":     "user",
    "workspaceRole":        null,
    "displayName":          "username",
    "billingType":          "stripe_subscription",
    "accountCreatedAt":     "2026-03-20T07:36:12.890154Z",
    "subscriptionCreatedAt":"2026-03-20T03:49:42.043152Z",
    "hasExtraUsageEnabled": false
  }
}
```

| 字段 | 说明 |
|------|------|
| `accountUuid` | 账户唯一标识 |
| `emailAddress` | 登录邮箱 |
| `organizationUuid` | 所属组织 ID |
| `organizationName` | 组织名称 |
| `organizationRole` | 组织角色：`owner` / `admin` / `user` |
| `billingType` | 计费类型：`stripe_subscription` 等 |
| `hasExtraUsageEnabled` | 是否启用额外用量 |

> `~/.claude.json` 同时包含大量非认证字段（UI 偏好、统计、迁移状态等），切换账号时只需替换 `oauthAccount` 部分，其余保持不变。

## 平台差异

| 项目         | macOS                                                            | Linux / WSL                             |
| ---------- | ---------------------------------------------------------------- | --------------------------------------- |
| OAuth 令牌存储 | Keychain（加密）                                                     | `~/.claude/.credentials.json`（文件权限 600） |
| 账户元信息      | `~/.claude.json`                                                 | `~/.claude.json`                        |
| 读取令牌       | `security find-generic-password -s "Claude Code-credentials" -w` | `cat ~/.claude/.credentials.json`       |
| 写入令牌       | `security add-generic-password -U -s ... -w ...`                 | 直接写文件 + `chmod 600`                     |

## 账号切换原理

多账号切换工具（如 ccswitch）的工作流程：

```
切换前：备份当前账号的 credentials + oauthAccount
                    ↓
切换时：写入目标账号的 credentials（Keychain/文件）
       + 合并目标账号的 oauthAccount 到 .claude.json
                    ↓
切换后：重启 Claude Code 使新认证生效
```

备份存储位置：`~/.claude-switch-backup/`

```
~/.claude-switch-backup/
├── configs/
│   ├── .claude-config-1-user1@example.com.json
│   └── .claude-config-2-user2@example.com.json
├── credentials/                                    # Linux only
│   ├── .claude-credentials-1-user1@example.com.json
│   └── .claude-credentials-2-user2@example.com.json
└── sequence.json                                   # 账号顺序和状态
```

macOS 上每个账号的 credentials 备份也存在 Keychain 中：

```
Service: "Claude Code-Account-{num}-{email}"
```

## 安全注意事项

- macOS Keychain 中的令牌受系统加密保护，首次读取时系统会弹窗确认
- `~/.claude.json` 权限应为 `600`，不含实际密钥
- `refreshToken` 是最敏感的凭据，泄漏后可持续获取新的 accessToken
- 切换工具的备份文件（`~/.claude-switch-backup/`）权限设为 `700`

## 参考资料

- [cc-account-switcher](https://github.com/ming86/cc-account-switcher) — 多账号切换工具
- [[Claude Code]] — Agent 框架
