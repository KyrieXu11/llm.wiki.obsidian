---
tags:
  - agents
  - claude-code
  - claude-agent-sdk
  - streaming
  - ux
created: "2026-04-23"
aliases:
  - 停止生成
  - 前后台互转
  - Ctrl+B detach
  - 交互生命周期信号
---

# Claude Agent SDK 运行时干预：取消与前后台互转

## 问题

[[Claude Code|cc-webui]] 里用户会做两类"非正常"打断 / 重塑 in-flight 生成的操作：

1. **停止生成**：模型正在流式输出 thinking/text，用户觉得够了或跑歪了，想立即终止（ChatGPT 那种红色 Stop 按钮）。
2. **前台 → 后台转移**：模型在 foreground 跑一个 blocking bash（比如 `sleep 60` / 长时间 build），用户不想让它卡在原地等，按 Ctrl+B 把它丢到后台继续跑，模型先干别的。

SDK 本身没直接 API：
- `query()` options 里**没有**接外部 `abortSignal` 的字段
- `tool()` 的 handler 只能通过 resolve Promise 产出一个 CallToolResult，没办法从外部"提前 resolve"

下面分别讲解法。都是 2026-04 在 cc-webui 落地的。

## 1. 停止生成：iterator.return() 出 for-await

### ac 是个摆设

最早版本里 `chat.ts` 有：

```ts
const ac = new AbortController();
stream.onAbort(() => { aborted = true; ac.abort(); });
```

但 `ac.signal` 从没传给 `query()` —— SDK 的 options 压根没这个入口。所以 `ac.abort()` 实际是 **no-op**，真正让 SDK 停的是 `for await` 的 `break` + 异步迭代器被 GC。

### 真正的 off switch

异步迭代器有 `.return()`：调用后 pending 的 `next()` resolve 成 `{done: true}`，for-await 自然退出。SDK 的 `query()` 返回的就是一个 async iterator。

```ts
interface InFlightChat {
  // ... existing fields ...
  cancelRequested: boolean;
  cancelIterator?: () => Promise<void>;  // 调用 response.return()
}

// detached task setup:
const response = query({ ... });
entry.cancelIterator = async () => {
  try { await (response as any).return?.(); } catch { /* ignore */ }
};

for await (const msg of response) {
  if (entry.cancelRequested) break;
  // ... fanout ...
}
```

flag + iterator.return() 一起上 —— flag 让下一次循环立刻 break，iterator.return() 让 pending 的 await 立刻 resolve（否则要等当前那个 SDK event 到达才会 break）。

### 取消路由

```ts
chat.post("/chat/cancel", async (c) => {
  const { sessionId, clientTurnId } = await c.req.json();
  const entry =
    (clientTurnId && activeChatsByClientTurn.get(clientTurnId)) ??
    (sessionId && activeChats.get(sessionId));
  if (!entry) return c.json({ ok: false, reason: "not_found" }, 404);
  if (entry.status !== "running")
    return c.json({ ok: false, reason: `already_${entry.status}` });
  entry.cancelRequested = true;
  entry.cancelIterator?.().catch(() => {});  // fire-and-forget
  return c.json({ ok: true });
});
```

**不等** iterator.return() 完成 —— 路由立刻返回 200。实际终止发生在 detached task 的下一次微任务，几百毫秒以内。

### 客户端和 UI

- Composer 的 send 按钮 `disabled=true` 时切换成红色 ■ 按钮（`Composer.tsx`）
- 点击调 `POST /api/chat/cancel`
- **客户端不需要做额外处理**：后端 iterator 退出后走原有 `fanout("done", "")` 路径 —— 客户端像正常 turn 结束一样收到 `event: done`，状态回到 idle。已经流出的 assistant 文本原样保留在 UI 上。

### 取消语义 = 丢尾

SDK iterator 被 return() 后，整个 turn **不会被写进 jsonl**（SDK 要完整 turn 才提交）。所以：
- UI 上能看到的部分文本：保留
- 刷新 / 重开 session：这段文本不见了

这和 ChatGPT / Claude.ai 的 Stop 行为一致，用户一般能接受。想要保留需要自己去 jsonl 补写，复杂度陡升，不做。

## 2. 前台 → 后台转移：proc 移交 + Promise 手动 resolve

### 为什么是个难题

`runForeground` 的常规结构：

```ts
function runForeground(...): Promise<CallToolResult> {
  return new Promise((resolve) => {
    const proc = spawn(...);
    proc.on("close", (code) => { resolve({...}); });
    // 各种 append / timeout / abort 监听器
  });
}
```

Promise 只能在 `close` 回调里 resolve。SDK 的 tool call 一路 await 到这里。要**中途 resolve** 让 SDK 认为工具完成了，必须在这个 Promise 的执行器里挂一个外部可调用的"detach 方法"。

### 登记表 + detach 闭包

```ts
interface ForegroundInvocation extends ForegroundSummary {
  detach: () =>
    | { ok: true; bashTaskId: string }
    | { ok: false; reason: string };
}
const foregroundInvocations = new Map<string, ForegroundInvocation>();

function runForeground(command, cwd, timeout, signal, sessionId, onForegroundEvent) {
  return new Promise((resolve) => {
    const fgId = "fg-" + randomUUID().slice(0, 8);
    const proc = spawn(...);
    let detachedTo: BackgroundTask | null = null;
    // ... stdout/stderr/truncated 本地状态 ...

    const detach = () => {
      if (settled || detachedTo) return { ok: false, reason: "already_X" };

      // 1. 创建 BackgroundTask，把当前 proc / 累积 buffer / truncated / dropped
      //    全搬过去
      const task: BackgroundTask = {
        id: "bg-" + randomUUID().slice(0, 8),
        sessionId, command, cwd, startedAt,
        proc,                        // 同一个子进程
        stdout, stderr,              // 已经收到的内容
        stdoutDropped, stderrDropped, truncated,
        // ... 其余字段 ...
        outputSubscribers: new Set(),
      };
      tasks.set(task.id, task);
      detachedTo = task;
      notifyList();

      // 2. 取消 fg 自己的 timer + abort handler（task 有自己的生命周期）
      clearTimeout(timer);
      signal?.removeEventListener("abort", abortHandler);

      // 3. 立刻 resolve，SDK 的 tool call 拿到 "Detached to bg-XXX..."
      settled = true;
      foregroundInvocations.delete(fgId);
      onForegroundEvent?.("foreground_ended", JSON.stringify({
        type: "foreground_ended", fgId, reason: "detached", bashTaskId: task.id,
      }));
      resolve({ content: [{ type: "text", text: `Detached to background. bashTaskId: ${task.id} ...` }] });
      return { ok: true, bashTaskId: task.id };
    };

    foregroundInvocations.set(fgId, { fgId, command, cwd, sessionId, startedAt, detach });
    onForegroundEvent?.("foreground_started", JSON.stringify({
      type: "foreground_started", fgId, sessionId, command, cwd, startedAt,
    }));

    // ... 正常的 appendOut / close / error handlers，加上 detachedTo 分支 ...
  });
}
```

外部的路由：

```ts
route.post("/foreground/:fgId/detach", (c) => {
  const fgId = c.req.param("fgId");
  const result = detachForegroundToBackground(fgId);
  if (!result.ok && result.reason === "not_found") return c.json({ error: "not_found" }, 404);
  return c.json(result);
});
```

### 数据路径的切换：`detachedTo` flag

proc 的 `data` / `close` / `error` 监听器是在 fg 阶段挂的。detach 之后还是这些监听器在跑 —— 但应该写到 task 而不是 fg 本地。在 appendOut 顶部加一行：

```ts
const appendOut = (buf, target) => {
  if (detachedTo) {
    appendToTask(buf, target);   // 用 BackgroundTask 的 rolling window + notifyTaskOutput
    return;
  }
  // 原本的 fg rolling 逻辑
};
```

`close`/`error` 类似分叉：detach 之后就把 task 的 status 更新 + emitStatus + emitDone + notifyList。

没有 removeListener / addListener 的重挂，因为同一个 proc 对象。只是把写入目标切走。

### 为什么不调 runBackground？

��过：detach 时 `runBackground(command, cwd, sessionId)` 重建一个后台任务，不就可以复用 runBackground 的全部 append/close/error 逻辑？

**不行**：runBackground 会 `spawn()` **一个新的 bash 子进程**，原来已经跑了 30 秒的进程就成孤儿了。detach 的本质是**同一个 proc 换个 lifecycle owner**，所以只能手工把 proc 塞进新 task 结构里，自己挂 notify/emit 调用。

## 3. UI 信号：运行时状态要对客户端可见

### foreground_started / foreground_ended 事件

`createBashMcpServer({ ..., onForegroundEvent: fanout })` —— 把 chat.ts 的 `fanout` 直接传进去。runForeground 在 start / detach / finalize 三处发事件。这些事件：

- 走 chat SSE 路径，客户端 `for-await streamChat(...)` 能收到
- 也进 `entry.messages`，**attach 刷新续流时会被 replay**

### 客户端状态

```ts
const [activeForegrounds, setActiveForegrounds] = useState<Array<{fgId, command}>>([]);

// handleSend 循环 + connectAttach onMsg 双路径都做 intercept：
if (msg?.type === "foreground_started" && msg.fgId) {
  setActiveForegrounds(prev => prev.some(f => f.fgId === msg.fgId) ? prev : [...prev, {...}]);
  continue;
}
if (msg?.type === "foreground_ended" && msg.fgId) {
  setActiveForegrounds(prev => prev.filter(f => f.fgId !== msg.fgId));
  continue;
}
```

attach useEffect 的 cleanup 里 reset `activeForegrounds = []`，切 session 时防止残留。

### Ctrl+B 绑定

```ts
if (e.ctrlKey && !e.metaKey && e.key.toLowerCase() === "b") {
  if (activeForegrounds.length === 0) return;  // 静默 no-op
  e.preventDefault();
  const latest = activeForegrounds[activeForegrounds.length - 1];
  detachForeground(latest.fgId).catch(console.error);
}
```

两条原则：
- **无 fg 时静默 no-op**：用户按了也看不到"错误"，不打断思路
- **取最新一个**：复数个 fg 同时跑（罕见）时，detach 最近挂起的

### Step pending 视觉：等待 vs 执行

原来所有 `status === "pending"` 都是同一个视觉（一开始是静态虚线圈，后来我改成了转圈动画）。实际上 pending 里有两个子状态：

| 状态 | 含义 | 视觉 |
|---|---|---|
| 等待审批 | 权限卡片还没点 | 静态虚线圈 |
| 执行中 | 审批过了，真在跑 | 蓝色 1/4 弧转圈 |

区分方法：permission_request 事件附带 `toolUseId: opts.toolUseID`（SDK canUseTool 的 opts 里暴露了），客户端 MessageList 扫当前 events，把**未答复的** permission 事件的 `s-${toolUseId}` 收成一个 Set 传给 StepTimeline。StepTimeline 判断 `awaitingPermission.has(stepId)` 渲染不同图标。

`本次会话都允许` 已经生效的后续同名工具：`canUseTool` 里 `allowance.has(toolName)` 直接放行，**不 fanout permission_request** —— awaitingPermission Set 不包含这个 step，图标一上来就是转圈，符合"真在跑"的事实。

## 4. 幂等 replay：一切事件都过 fanout

```ts
const fanout = (event, data) => {
  entry.messages.push({ event, data });
  for (const sub of entry.subscribers) sub.write(event, data);
};
```

cancel / detach 触发的 `foreground_ended`（含 reason=detached）、`done`、各种 permission_request 全都走这一条路径。也就是说：

- 初始 POST 的 subscriber 实时看到
- attach 的 subscriber 重放 entry.messages 时一样看到

用户刷新的时候，foreground_started 已经在 buffer 里 → 客户端续流 replay → `activeForegrounds` 重新填满 → 仍然能 Ctrl+B 转后台。SDK 的取消 / iterator return 等状态切换全部覆盖。

## 踩坑

- **canUseTool opts 里的 toolUseID**（驼峰 `ID`，不是 `Id`）是关键 plumbing 点，没有它 UI 没法区分哪张 permission 卡对应哪个 step。SDK 类型定义在 `sdk.d.ts` 搜 `CanUseTool` 能找到完整 shape。
- **ac.abort() 不等于 SDK 取消**：前面说过。删掉省心。
- **detach 不能 runBackground 再开一个**：新 proc 不是原 proc，原来的 30s 就孤儿了。只能手工搭 BackgroundTask。
- **Ctrl+B 不 gate input/textarea**：用户在 Composer 打字时也想随时 detach，加 `tag === "TEXTAREA"` 的 guard 是自找麻烦。Ctrl+B 在普通 textarea 没默认语义，preventDefault 即可。
- **Stop 按钮的样式**：红色 ■ 比灰色方块更能传达"这会丢东西"的暗示。对称的 bg-blue 和 bg-red 让 send / stop 一目了然。

## 未覆盖的点

- **按工具粒度的 cancel**：目前只能 cancel 整个 turn（SDK iterator 层级），不能"让这次 tool call 失败但 turn 继续"。要做得从 MCP 层面自己维护一个 per-tool-use-id 的 signal。
- **detach 的逆向**（后台任务拉回前台）：没做。逻辑上类似 —— 暂停 task 的 notify/subscriber，把 proc + buffer 塞回一个 foreground invocation 的 Promise，等 close resolve。但 UX 场景比较冷门。
- **multi-foreground 的 UI**：`activeForegrounds` 数组支持多个，但 UI 只在按 Ctrl+B 时取最后一个。如果真的并发出现多个 fg（模型一次 tool use 里并行开多个？罕见），应该给个选择器。

## 参考代码位置

cc-webui 仓库（commit 在 `00b6e1a` 之后的 dirty tree / 待提交）：
- `server/chat.ts` — `InFlightChat.cancelRequested` / `cancelIterator` + POST `/chat/cancel` + `onForegroundEvent: fanout` 接线 + permission_request 带 `toolUseId`
- `server/bash-mcp.ts` — `foregroundInvocations` Map + `runForeground` 重构 + `detach()` 闭包 + `detachForegroundToBackground` 导出 + `Options.onForegroundEvent`
- `server/bash-tasks.ts` — `GET /foreground` 列表 + `POST /foreground/:fgId/detach` 路由
- `src/lib/api.ts` — `cancelChat({sessionId?, clientTurnId?})`
- `src/lib/tasks.ts` — `detachForeground(fgId)`
- `src/components/Composer.tsx` — `onCancel` prop + 红色 ■ 按钮
- `src/App.tsx` — `handleCancel` + `activeForegrounds` state + Ctrl+B keybind + 双路径 (handleSend / attach) 的 foreground event intercept
- `src/components/StepTimeline.tsx` — pending 分支按 `waiting` 区分虚线圈 vs 转圈
- `src/components/MessageList.tsx` — 计算 `awaitingPermission: Set<string>` 传给 StepTimeline
- `src/lib/processor.ts` + `src/lib/types.ts` — permission 事件存 `toolUseId`

## 相关

- [[Claude Agent SDK 自定义 Bash MCP]] — MCP server 本体 + BackgroundTask 模型，本篇的 detach / Ctrl+B 建立在这套数据结构之上
- [[Claude Agent SDK 流式对话断线续流]] — `InFlightChat` + fanout + attach 的全套基础设施，本篇的事件也走同一条路径
- [[MCP (Model Context Protocol)]]
- [[Claude Code]]
