---
tags:
  - agents
  - claude-code
  - claude-agent-sdk
  - streaming
  - sse
created: "2026-04-23"
aliases:
  - 中途刷新续流
  - attach reconnect
  - claude-agent-sdk 流式续接
---

# Claude Agent SDK 流式对话断线续流

## 问题

浏览器刷新 / 切换会话 / 关 tab —— 这些是 webui 日常动作，但对流式 SDK 对话是灾难。

朴素实现：`POST /chat` → SSE 流 → 客户端 `for-await` 应用 delta 到 UI。
- **刷新瞬间** HTTP 连接断 → 后端 `stream.onAbort` 触发，如果顺手调了 `ac.abort()` SDK 请求跟着取消
- SDK 被取消 → assistant turn 跑到一半 → jsonl 只落了 user message，没落 assistant
- 刷新后客户端读 jsonl → 只看到自己发的那条气泡，助手回复**凭空消失**

实践落地：[[Claude Code|cc-webui]]，2026-04 commit `00b6e1a`。

## 核心设计：SDK 生命周期与 HTTP 连接解耦

把 SDK 请求从单次 HTTP 请求的生命周期里剥离 —— 让它跑在后端进程的独立协程里。HTTP 连接只是一个**订阅者**，断开只是少一个订阅者，SDK 继续。

```
┌─────────── Node 进程 ──────────────────────────────────────────┐
│                                                                │
│   POST /chat                                                   │
│       │                                                        │
│       ├─ 创建 InFlightChat entry, 注册进 activeChats           │
│       ├─ (async () => { SDK 迭代 + fanout })()  ← 分离         │
│       └─ return streamSSE(attach to entry as subscriber)       │
│                                                                │
│   activeChats: Map<sessionId, InFlightChat>                    │
│   activeChatsByClientTurn: Map<clientTurnId, InFlightChat>     │
│                                                                │
│   InFlightChat {                                               │
│     messages: BufferedMsg[]        ← 所有 fanout 过的消息       │
│     subscribers: Set<Subscriber>   ← 当前连着的写出者            │
│     status: running | done | error                             │
│   }                                                            │
│                                                                │
│   GET /chat/attach?sessionId=X&clientTurnId=Y                  │
│       │                                                        │
│       └─ 找到 entry → replay messages + 继续 fanout            │
└────────────────────────────────────────────────────────────────┘
```

规则：
- 浏览器断开 → 订阅者 size-- → SDK 不动
- 浏览器刷新后 → 连 attach → 先 replay 已有 buffer + 再 live 后续 → UI 像没断过

## 关键点 1：稳定的 client turn id

第一次 `POST /chat` 时 `sessionId` 可能是 `null`（新对话）。此时还没法用 sessionId 作为 activeChats 的 key。而 SDK `system:init` 把真正的 `session_id` 回传过来得等**几秒后**（subprocess 启动 + 第一次 API call）。如果用户在这几秒里刷新 → attach 无法定位 entry。

解法：客户端在发送时生成 `clientTurnId`（UUID）+ 存 localStorage。`/chat` 和 `/chat/attach` 都接受这个 id。服务端 activeChats 做双索引：

```ts
interface InFlightChat {
  reqId: string;
  clientTurnId: string | undefined;
  sessionId: string | undefined;
  messages: BufferedMsg[];
  subscribers: Set<Subscriber>;
  status: "running" | "done" | "error";
  errorMsg?: string;
}

const activeChats = new Map<string, InFlightChat>();              // by sessionId
const activeChatsByClientTurn = new Map<string, InFlightChat>();  // by clientTurnId
```

SDK 回传 session_id 后自然迁移 map 的 key，两个 id 任一命中都能接回 entry。

## 关键点 2：持久化未完成的 user prompt

SDK 把 user message 写进 jsonl 是**整个 assistant turn 完成后**的事，不是发出 prompt 时。如果刷新的瞬间 user message 还在内存没落盘，刷新后读 jsonl 连自己的 prompt 都看不到。

客户端在发送前把 turn 状态写进 localStorage：

```ts
type ActiveTurn = {
  clientTurnId: string;
  cwd: string;
  sessionId: string | null;
  prompt: string;
  startedAt: number;
};
```

刷新后恢复步骤：
1. 先渲染 jsonl 里的历史轮次
2. 用 `ensureActiveTurnUserEvent(events, active, cwd)` 给当前 turn 补出 user 气泡（按 prompt 文本去重，避免 jsonl 已有时重复）
3. `connectAttach({sessionId, clientTurnId})` 续流

再加一条 **startedAt 超过 1 小时**的 stale turn 自动丢弃，避免上次没收完的 turn 污染新视图。

## 关键点 3：applySDKMessage 必须幂等（以及什么是正确的事件 id）

attach replay 会把 `entry.messages` 里的消息**再发一遍**。如果 `applySDKMessage` 不是幂等的，同一条 `content_block_start` 第二次到来就会再创建一个新事件 → 重复 React key → 渲染异常，甚至"内容消失"（React 对重复 key 的处理往往是只渲染其中一个）。

所以每个能产生事件的 msg 类型都要按"先找再更新，找不到才新建"的模式写。

但最坑的是**选什么做 id**。

### ⚠️ 陷阱：`msg.uuid`

```ts
// 错误示范
const id = `a-${msg.uuid}-${ev.index}`;
```

`msg.uuid` 是 **SSE 投递 envelope 的 uuid**，不是 SDK 语义消息层的 id。**同一个 assistant message 的多个 stream_event**（`content_block_start` / `content_block_delta` / `content_block_stop`）由 SDK 逐个 envelope 发出，每个 envelope 的 `uuid` 都是新的。

所以用 envelope uuid 当主键，`content_block_start` 创建的事件和紧跟的 `content_block_delta` 根本对不上 —— 即使第一次投递就是错乱的。attach 场景下只是把这个错误放大。

### ✓ 正解：SDK 消息语义层的稳定 id

```ts
function streamBlockId(prefix: "a" | "t", msg: any, index: number): string {
  const base =
    msg.stream_message_id ??       // SDK 顶层稳定 id，首选
    msg.event?.message?.id ??      // Anthropic API message_start 事件里的 message id
    msg.message?.id ??             // 最终 assistant 消息层
    msg.uuid ??                    // envelope id 兜底，最弱
    "unknown";
  return `${prefix}-${base}-${index}`;
}
```

`stream_message_id` 和 `event.message.id` 对**同一个 assistant message 的所有 content_block_* 事件保持一致**。用它 + block index (`ev.index`) 做主键才稳定。

### 使用模式

```ts
// content_block_start (text)
if (block?.type === "text") {
  const id = streamBlockId("a", msg, ev.index);
  const idx = events.findIndex((e) => e.id === id);
  if (idx >= 0) {
    // 重复投递 → 把 text 重置为空，后续 delta 重建
    return [...events.slice(0, idx), { ...events[idx], text: "" }, ...events.slice(idx + 1)];
  }
  return [...events, { id, type: "assistant", text: "" }];
}

// content_block_delta (text_delta)
if (ev.delta?.type === "text_delta") {
  const id = streamBlockId("a", msg, ev.index);
  const idx = events.findIndex((e) => e.id === id);
  if (idx >= 0) {
    const target = events[idx];
    if (target.type === "assistant") {
      return [...events.slice(0, idx),
              { ...target, text: target.text + ev.delta.text },
              ...events.slice(idx + 1)];
    }
  }
  return events;
}
```

thinking / tool_use 同理。tool_use 的稳定 id 更简单 —— 直接用 `block.id`（SDK 保证在消息内唯一且跨投递稳定）。

## 关键点 4：最终 assistant 消息的兜底匹配

SDK 在流尾部会再发一条 `msg.type === "assistant"` 的聚合消息，content 是完整 blocks 数组。这个聚合消息的 `message.id` 和前面 stream_events 的 `stream_message_id` **可能对不上**（不同 SDK 版本行为有差异）。

如果严格按 id 匹配，对不上就变成"一份 stream 构造的 + 一份最终聚合的"两份重复。

兜底策略 `findCompatibleTextEventIndex`：按文本前缀匹配 —— 找一个现有 assistant 事件，如果它的 text 是最终 text 的前缀（或反之），认为是同一块，合并；否则才新建。

```ts
function findCompatibleTextEventIndex(events, type, text): number {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.type !== type) continue;
    if (!ev.text || text.startsWith(ev.text) || ev.text.startsWith(text)) return i;
  }
  return -1;
}
```

## 关键点 5：subscriber + fanout 的 race-free 挂载

每个连接（POST 初始 / GET attach）是一个 subscriber。entry.subscribers 是个 Set。SDK 产出新消息时：

```ts
const fanout = (event: string, data: string) => {
  entry.messages.push({ event, data });       // 存档
  for (const sub of entry.subscribers)        // 扇出
    sub.write(event, data);
};
```

subscriber 内部用 **queue + wake** 模式把同步的 sub.write 和异步的 stream.writeSSE 桥起来（同 §9.2 of [[Claude Agent SDK 自定义 Bash MCP]]）。

attach 时 replay + live 的无缝衔接：

```ts
const snapshot = entry.messages.slice();   // 抓当前全量快照
entry.subscribers.add(sub);                // 注册后续 fanout 收割者
// ↑ snapshot 和 add 在同一个同步块（中间无 await），JS 单线程不可能被
//   fanout 插进来 —— 不需要 skip 计数器，不需要 dedup。每条消息要么在
//   snapshot 里（被 replay），要么之后来到（走 sub.write 进 queue）。
for (const m of snapshot) await stream.writeSSE(m);
// 之后 drain queue 里累积的 live 消息
```

**我自己踩过的坑**：我加过一个 `skip = snapshot.length` 的计数器，以为要防止"replay 和 live 重复"。实际上根本没这个窗口（见上注释），skip 反而把前 N 条 live 消息吃掉了。教训：**别为不存在的 race 写补丁**。

## 关键点 6：并发互斥用 409 拒绝

两种互斥 key，两种 409：

| 触发 | 返回 |
|---|---|
| 同 sessionId 已有 in-flight turn | `409 session_busy` |
| 同 clientTurnId 已有 in-flight turn | `409 turn_busy` |

第一种防"同 session 开两个 tab 都发消息"、第二种防"客户端因抖动重发同一 turn"。

客户端拿到 409 时插一条"[上一轮还在生成]"提示气泡，而不是红色错误。

## 踩坑合集

- **`ac = new AbortController()` 但从没把 signal 传给 `query()`** —— 存在感 0。SDK `query()` 目前未公开接外部 signal 的入口。iterator GC + subprocess 管道关闭就是事实上的清理机制。直接删掉 `ac` 更干净。
- **`msg.uuid` 不是你想要的那个 id** —— 它是 envelope 级的。跨 envelope 稳定的主键来源：`stream_message_id` / `event.message.id` / `message.id`。
- **attach useEffect 的 deps**：`[sessionId, clientTurnId, isStreaming]`；`isStreaming=true` 时早 return（POST 那条流已经是 subscriber 了）。handleSend 结束 isStreaming 变 false 后 useEffect 重跑、打开 attach，server 如果已经把 entry 删了就回 `no-inflight`，客户端关流空转一轮无害。
- **localStorage stale turn**：写 `startedAt`，加载时超过 1 小时直接丢弃。
- **Vite HMR** 触发 full page reload 时所有活动 fetch 被 abort → 如果 dev 阶段看见 `network error`，先看是不是刚改过前端导出签名。

## 未覆盖的点

- **用户主动停止生成** 暂无 UI 按钮。加一个 `POST /chat/:clientTurnId/cancel` 在 entry 上 set 一个 abort 标志 + 结束 iterator 即可。
- **entry lingering**：目前 SDK done 后立即从 map 删除。如果客户端 2s 后才刷新回来 attach，正常能靠 jsonl 续上。但 jsonl 写盘有延迟时会有短暂"刚好找不到"的窗口。给 entry 加 5~10s 的 lingering 再清能彻底消除。
- **attach rate limit**：目前同一 entry 可被无限订阅者 attach，replay 会扇出 N 份。对局部小规模 webui 不是问题。

## 参考代码位置

cc-webui 仓库（commit `00b6e1a`，2026-04-23）：
- `server/chat.ts` — `InFlightChat` / `attachStreamToEntry` / POST /chat + GET /chat/attach + 409 互斥 + fanout
- `src/lib/processor.ts` — `streamBlockId` + `findCompatibleTextEventIndex` + 所有 content_block / assistant / thinking 处理的幂等化
- `src/App.tsx` — `loadActiveTurn` / `saveActiveTurn` / `ensureActiveTurnUserEvent` + useEffect 挂载 attach
- `src/lib/api.ts` — `connectAttach({sessionId, clientTurnId})` + 409 `session_busy` / `turn_busy` 解析

## 相关

- [[Claude Agent SDK 自定义 Bash MCP]] — 同一套订阅者 / fanout / drain 模式也用在后台任务的 SSE 流；这篇把它升级成了"HTTP 连接是订阅者，而不是主宰"
- [[MCP (Model Context Protocol)]]
- [[Claude Code]]
- [[Tool Use 工具调用]]
