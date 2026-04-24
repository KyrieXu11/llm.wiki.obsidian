---
tags:
  - agents
  - claude-code
  - mcp
  - claude-agent-sdk
created: "2026-04-22"
updated: "2026-04-23"
aliases:
  - 用 MCP 替换内置 Bash
  - cc-webui Bash MCP
---

# Claude Agent SDK 自定义 Bash MCP

## 概述

`@anthropic-ai/claude-agent-sdk` 内置的 `Bash` / `BashOutput` / `KillBash` 三件套在 SDK 层是"半黑盒"——能拿到 `canUseTool` 的审批钩子，但**拿不到后台任务的状态**（`backgroundedByUser` / `backgroundTaskId` 等字段只在只读 schema 里，没有可调控制通道），也无法让前端主动把前台进程转后台。

替换方案：用 `createSdkMcpServer` 自定义一个 `bash` MCP server，`disallowedTools` 禁用内置的三件套，系统提示引导模型走 MCP 版本。这样整个生命周期（spawn / 输出 / 杀进程 / 后台注册表）都落在自己的后端进程里，前端可以叠加任意 UI。

实践落地：[[Claude Code|cc-webui]] 项目，2026-04 完成。

## 核心要点

### 1. 同进程 MCP，不起端口

```ts
import { createSdkMcpServer, tool } from "@anthropic-ai/claude-agent-sdk";

const server = createSdkMcpServer({
  name: "bash",
  version: "0.2.0",
  tools: [runTool, outputTool, killTool],
});
```

`createSdkMcpServer` 返回的是**内存对象**，不启 stdio、不开端口。工具 handler 直接跑在后端 Node 进程里，`spawn()` 出来的子进程继承后端的 uid/env/cwd——安全边界等于后端进程。

### 2. 禁用内置工具 + 系统提示引导

```ts
query({
  options: {
    mcpServers: { bash: server },
    disallowedTools: ["Bash", "BashOutput", "KillBash"],
    systemPrompt: {
      type: "preset",
      preset: "claude_code",
      append:
        "SHELL TOOLS: Built-in Bash/BashOutput/KillBash are DISABLED. " +
        "Use mcp__bash__run (same schema + run_in_background). " +
        "Poll with mcp__bash__output, terminate with mcp__bash__kill.",
    },
    canUseTool: async (toolName, input, opts) => { ... },
  },
});
```

三层保险：`disallowedTools` 物理拦截、`mcpServers` 提供替代、系统提示明确告诉模型走哪条路。缺一个都会出现模型反复尝试内置 Bash 然后被拒的情况。

### 3. ⚠️ PermissionResult 的 ZodError 陷阱

`canUseTool` 的返回类型在 `sdk.d.ts` 里标注：

```ts
export declare type PermissionResult =
  | { behavior: "allow"; updatedInput?: Record<string, unknown>; ... }
  | { behavior: "deny"; message: string; ... };
```

看上去 `updatedInput` 是可选的。但 **SDK 运行时用的 Zod schema 与 TS 类型不一致**——对 MCP 工具的 `allow` 分支，`updatedInput` 是必填的：

```
ZodError: [
  { code: "invalid_union", errors: [[
    { path: ["updatedInput"], message: "expected record, received undefined" },
    ...
  ]]}
]
```

修正做法：allow 时透传原始 input：

```ts
const decision = await awaitPermission(id, opts.signal);
if (decision.behavior === "allow") {
  return { behavior: "allow", updatedInput: input };
}
return decision;  // deny 分支已经带 message，不动
```

推测原因：内置工具的 input 由 CLI 直接持有，MCP 工具必须经由 `updatedInput` 显式回传给 MCP server，所以运行时把它收紧成必填。**TS 类型不能信，要以 zod 报错为准**。

### 4. 前端 UI 别名

MCP 工具的名字是 `mcp__<server>__<tool>`，直接显示成 `mcp__bash__run` 用户体感很差。在前端事件处理器里做别名映射：

```ts
const TOOL_ALIAS: Record<string, string> = {
  "mcp__bash__run": "Bash",
  "mcp__bash__output": "BashOutput",
  "mcp__bash__kill": "KillBash",
};
```

在 `applySDKMessage` / 会话回放 / 权限卡片三处都要 normalize，否则权限弹窗显示的工具名和时间线里的步骤会对不上。

### 5. 后台任务三件套

| 工具                  | 作用                                  | 入参                                                          |
| ------------------- | ----------------------------------- | ----------------------------------------------------------- |
| `mcp__bash__run`    | 前台执行，或 `run_in_background=true` 入后台 | `command`, `timeout?`, `description?`, `run_in_background?` |
| `mcp__bash__output` | 增量读取后台任务输出                          | `bash_id`                                                   |
| `mcp__bash__kill`   | SIGKILL 后台任务                        | `bash_id`                                                   |

数据模型：

```ts
interface BackgroundTask {
  id: string;                  // "bg-" + uuid.slice(0, 8)
  sessionId: string | undefined;  // 会话绑定，见 §7
  command: string; cwd?: string;
  startedAt: number; finishedAt: number | null;
  proc: ChildProcess;
  // stdout/stderr 是 rolling 窗口：最多 MAX_OUTPUT_BYTES 字符，满了从头砍掉。
  // *Dropped 累计被砍掉的字符数，offset 语义配套（见下）。
  stdout: string;
  stderr: string;
  stdoutDropped: number;
  stderrDropped: number;
  exitCode: number | null;
  status: "running" | "completed" | "killed" | "failed";
  // readStdoutOffset / readStderrOffset 存"从流开始到现在读过多少字符"
  // （cumulative），不是当前 buffer 里的下标 —— 这样滚动之后还能正确切片。
  readStdoutOffset: number;
  readStderrOffset: number;
  truncated: boolean;          // 至少发生过一次滚动
  endReason: string | null;
  outputSubscribers: Set<TaskOutputSubscriber>;  // SSE 推送，见 §9
}

const tasks = new Map<string, BackgroundTask>();  // 模块级，跨请求共享
```

**注意点**：
- 注册表必须是**模块级（进程级共享）**，不能每次请求新建。`createBashMcpServer()` 是 per-request，但模型在第二轮调 `output` 时需要能查到第一轮的 task。
- `kill` 时**先置 `status = "killed"` 再 `SIGKILL`**，防止紧随其后的 `close` 回调把 status 覆写回 `completed`。
- `output` 增量读取走 cumulative 偏移量：服务端 `slice(readStdoutOffset - stdoutDropped)` 取新内容，把 offset 推进到 `stdoutDropped + stdout.length`。如果轮询慢了、某些字符在这段时间被滚出窗口，会额外附加一行 `[N chars rolled off before this poll]` 提示 LLM 有多少丢了。
- **head-keep vs tail-keep 的取舍**：选了 tail（保留末尾），因为长跑任务的错误栈、最终 exit 信息通常在末尾。代价是开头的 boilerplate 会被丢。如果某个场景需要保开头，改 `appendOut` 的一行即可。

### 6. output / kill 自动放行

父 `run` 已经过用户授权才会返回 `bashTaskId`，后续的 `output` / `kill` 如果每次都弹权限卡片，体验极差。在 `canUseTool` 最开头做白名单：

```ts
if (toolName === "mcp__bash__output" || toolName === "mcp__bash__kill") {
  return { behavior: "allow", updatedInput: input };
}
```

## 7. 会话级隔离

多会话场景下，进程级 `tasks` Map 会让不同 session 看到彼此的 `bash_id`。解决办法是把 sessionId 作为 task 的一等字段，读取时按 session 过滤。

### 7.1 数据结构 + 过滤

```ts
export function listBackgroundTasks(filter?: {
  sessionId?: string | null;
}): BackgroundTask[] {
  const all = Array.from(tasks.values());
  if (!filter || !("sessionId" in filter)) return sorted(all);
  return sorted(all.filter((t) => t.sessionId === (filter.sessionId ?? undefined)));
}
```

REST 加 `?sessionId=` 查询串，客户端根据当前 `sessionId` 过滤；`sessionId` 为 `null` 时直接本地短路返回空列表，不发请求。

### 7.2 跟随会话迁移：getter + relabel

首轮 `/chat` 的 sessionId 是 `undefined`（还没 resume），SDK `system:init` 回传后才有真 id。如果在 spawn 时就把 id 写死到 task，init 之前的那几个 task 永远被过滤掉，显示不出来。两步方案：

**（a）spawn 时用 getter 读最新值**，这样 SDK init 之后再调 `mcp__bash__run` 会拿到真 id：

```ts
// chat.ts
let currentSessionId = body.sessionId;
const bashMcp = createBashMcpServer({
  cwd,
  getSessionId: () => currentSessionId,
});
```

**（b）init 到达时，把 previously-tagged 的 task 批量迁移**：

```ts
// bash-mcp.ts
export function relabelTasksSessionId(
  from: string | undefined,
  to: string
): void {
  let changed = false;
  for (const t of tasks.values()) {
    if (t.sessionId === from) { t.sessionId = to; changed = true; }
  }
  if (changed) notifyList();
}

// chat.ts — 在 for await 循环里遇到 system:init 时
const emittedId = (msg as any).session_id;
if (emittedId && emittedId !== currentSessionId) {
  relabelTasksSessionId(currentSessionId, emittedId);
  currentSessionId = emittedId;
}
```

同样模式的 `sessionAllowances`（per-tool "本次会���都允许"缓存）共用同一段迁移代码，一处触发覆盖两套状态，避免不一致。

## 8. 生命周期清理

后端进程退出时，若不主动杀子进程，macOS/Linux 的 bash 子进程**不会自动死**——没 `detached:true` / `nohup` 也不会，因为父子关系只决定继承和等待，不决定终结。`tsx watch` 每次改动文件触发 SIGTERM → 重启 Node，如果不清理，每次重启都泄漏一批 bash。

模块级一次性安装 signal handler：

```ts
let shutdownHandlersInstalled = false;
function installShutdownHandlersOnce(): void {
  if (shutdownHandlersInstalled) return;
  shutdownHandlersInstalled = true;

  const onSignal = (sig: NodeJS.Signals) => {
    killAllRunningTasks(`server shutdown (${sig})`);
    const code = sig === "SIGINT" ? 130 : sig === "SIGHUP" ? 129 : 143;
    process.exit(code);
  };

  process.once("SIGTERM", onSignal);
  process.once("SIGINT", onSignal);
  process.once("SIGHUP", onSignal);
  process.on("beforeExit", () => killAllRunningTasks("process beforeExit"));
}
installShutdownHandlersOnce();
```

细节：
- **`once` 注册**而不是 `on`：避免热重载 / 同文件被多次 import 时 handler 累积。
- **显式 `process.exit(code)`**：一旦挂了 SIGTERM handler，Node 默认行为被接管，不 exit 就不退。code 沿用 POSIX 惯例（130=SIGINT, 129=SIGHUP, 143=SIGTERM）。
- **`beforeExit` 兜底**：Node 正常退出（event loop 空）不走 signal 路径，这里再扫一次。
- **`killAllRunningTasks` 里也 emit 终态事件**：不是给自己看的（进程马上死），是给还连着的 SSE 客户端最后一推，让 UI 能显示 `killed` 而不是停在 `running`——只要 SSE writer 在 exit 前把这一帧冲出去就赢。

## 9. 两条 SSE 流：list + per-task output

`mcp__bash__output` 是给 **LLM** 的增量读取路径（按 per-task 偏移量切片）。前端要的是**实时推送** —— 当初为了赶工用了 2~5s 轮询整段 buffer，任务打满 256KB 后每次 2s 就是 256KB 全量，既慢又浪费。改成两条 SSE 流：

| 路由 | 用途 | 事件 |
|---|---|---|
| `GET /api/bash/tasks/stream?sessionId=X` | list 级订阅：添加 / 状态跳变 / kill | `snapshot`（每次全量 summary 列表） |
| `GET /api/bash/tasks/:id/stream` | 选中任务的输出流 | `init`、`stdout`、`stderr`、`status`、`done` |

### 9.1 服务端订阅者机制

在 `bash-mcp.ts` 里加两层 fanout：

```ts
// list 级
const listSubscribers = new Set<() => void>();
export function subscribeListChanges(sub: () => void): () => void {
  listSubscribers.add(sub);
  return () => { listSubscribers.delete(sub); };
}
function notifyList() {
  for (const s of listSubscribers) try { s(); } catch {}
}

// task 级
export type TaskOutputEvent =
  | { type: "stdout"; chunk: string }
  | { type: "stderr"; chunk: string }
  | { type: "status"; status: TaskStatus; exitCode: number | null; ... }
  | { type: "done" };

// 写在 BackgroundTask 上
outputSubscribers: Set<(ev: TaskOutputEvent) => void>;
```

写路径（`appendOut` / `proc.on("close")` / `proc.on("error")` / `kill` / `killAll`）统一调用 `notifyTaskOutput` 和 `notifyList`。扇出点只有这几个，容易 audit。

### 9.2 streamSSE 消费循环：queue + wake

Hono 的 `streamSSE(c, async (stream) => {...})` 在回调返回时关流。挑战在于：订阅者被**同步调用**（从 `proc.stdout.on("data", ...)` 回调里），但 `stream.writeSSE` 是 async。直接 `await` 会阻塞订阅者链。

解决方法：订阅者只 push 到队列 + 唤醒 waiter，drain 循环在协程里串行 `writeSSE`：

```ts
route.get("/:id/stream", (c) => streamSSE(c, async (stream) => {
  const queue: Array<{event: string; data: string}> = [];
  let closed = false;
  let resolveNext: (() => void) | null = null;
  const wake = () => {
    if (resolveNext) { const r = resolveNext; resolveNext = null; r(); }
  };
  const push = (event: string, data: string) => {
    queue.push({ event, data }); wake();
  };

  // 1. 先推 init 快照，带当前全量 stdout/stderr
  await stream.writeSSE({
    event: "init",
    data: JSON.stringify({ ...summary, stdout, stderr }),
  });

  // 2. 已完成任务：直接发 done 结束
  if (task.status !== "running") {
    await stream.writeSSE({ event: "done", data: "" });
    return;
  }

  // 3. 订阅增量
  const unsubscribe = subscribeTaskOutput(id, (ev) => {
    if (ev.type === "stdout" || ev.type === "stderr")
      push(ev.type, JSON.stringify({ chunk: ev.chunk }));
    else if (ev.type === "status") push("status", JSON.stringify(ev));
    else if (ev.type === "done") { push("done", ""); closed = true; wake(); }
  });
  const keepAlive = setInterval(() => push("ping", ""), 25_000);
  stream.onAbort(() => {
    closed = true; unsubscribe(); clearInterval(keepAlive); wake();
  });

  // 4. drain 循环
  while (!closed || queue.length > 0) {
    if (queue.length === 0) {
      await new Promise<void>((r) => { resolveNext = r; });
      continue;
    }
    await stream.writeSSE(queue.shift()!);
  }
  clearInterval(keepAlive);
}));
```

关键点：
- **`init` 带全量 buffer**：客户端刚订阅时已经错过了之前的 chunk，必须一次性 rehydrate。之后的 `stdout`/`stderr` 都是真正的增量 delta。
- **已完成任务也要能订阅**：`init` + `done` 就走完，客户端能看到最终状态，不会卡在"加载中"。
- **25s keep-alive ping**：反向代理 / vite proxy 往往有 idle 超时（60s 左右），`sleep` 类长任务没输出会被切，ping 成本可以忽略。
- **`onAbort` 清理**：客户端关页 / 切 tab → EventSource 断开，服务端必须解订 + 清 interval，否则 `listSubscribers` / `outputSubscribers` 集合会永远扩大。

list 流更简单，因为 list 变化粒度粗——有变动就重推快照。`subscribeListChanges` 触发时 set `dirty` 标志，drain loop 把 dirty 合并成一次 `writeSSE`，避免雪崩（多个状态跳变紧接着来时只推一次）。

### 9.3 客户端 EventSource

```ts
export function subscribeTaskStream(
  id: string,
  onEvent: (ev: TaskStreamEvent) => void
): () => void {
  const es = new EventSource(`/api/bash/tasks/${encodeURIComponent(id)}/stream`);
  es.addEventListener("init", (e) =>
    onEvent({ type: "init", payload: JSON.parse((e as MessageEvent).data) }));
  es.addEventListener("stdout", (e) =>
    onEvent({ type: "stdout", chunk: JSON.parse((e as MessageEvent).data).chunk }));
  // stderr / status 同理
  es.addEventListener("done", () => { onEvent({ type: "done" }); es.close(); });
  return () => es.close();
}
```

注意 `EventSource` 默认**自动重连**（Retry 字段控制间隔）—— 这对 dev HMR 友好（tsx 重启服务时客户端自动续上），但要小心：`init` 事件在每次重连都会重新推全量 buffer，客户端 `setState(payload)` 覆盖当前状态即可，**不要 append**。

React 侧消费：`useEffect` 绑定 `[selectedTaskId]`，切任务时自动解订旧的 EventSource。`subscribe` 返回的 unsubscribe 直接作为 effect cleanup。

### 9.4 延迟与流量对比

| 维度 | 轮询版 | SSE 版 |
|---|---|---|
| 输出延迟 | ≤ 2s（POLL_MS） | ~0（pipe flush 即达） |
| 每 2s 流量 | 整段 stdout+stderr（最坏 256KB） | 仅真实 delta |
| kill 状态回显 | 等下一次 poll | 立即 |
| 任务新增 / 完成 | Button 5s 才看到 | 立即 |

浏览器同源 EventSource 默认 6 个并发上限，这套方案一个 session 最多 2 条流（list + 选中 output），够用。

## 踩坑与诊断

**ZodError 的定位路径**：报错发生在 SDK 内部 `canUseTool` 的返回值校验层，堆栈不会指向用户代码。最快的诊断方法是直接在另一个 Claude Code 会话里调用这个 MCP 工具，权限弹窗触发 ZodError 时会**原样把错误冒泡到顶层**，可以看到完整的 union 两个分支的 Zod 报错——看 `path: ["updatedInput"]` 那条就是答案。

**验证内置被禁用**：模型的第一次试探通常是直接调 `Bash`，会被 SDK 挡下并返回 "tool not allowed"，然后它会再走 `ToolSearch` 加载 `mcp__bash__run` 的 schema。系统提示里明确点名能把这两步合并成一步。

**bg task 没出现在 UI**：先 `curl /api/bash/tasks` 看服务端有没有。常见根因按概率排序：
1. 模型根本没调 `mcp__bash__run`（看 `[chat X] tool_use:` 日志验证；可能它看到某条 system:status 以为 MCP 不可用，自己编造 `bashTaskId` 交差）
2. 调了但 sessionId 没对齐（比如 UI 当前 session 是 A，任务跑在 B）—— 无参数拉 `/api/bash/tasks` 能看到所有任务和它们的真实 sessionId
3. 服务重启把 Map 清了（见 §8，重启会 SIGKILL + 清空）

## 未覆盖的点（更新）

前三项 2026-04-23 已补齐，见第 7-9 节：流式 stdout、会话级隔离、进程清理。

剩余遗憾：
- **跨重启持久化**：后端重启后任务 Map 归零。要跨重启必须 `detached:true` + 磁盘 log + 重连 pid 探活——跟 Claude Code CLI "`/exit` 后 shell 也死" 的语义一致，不做。
- **流式输出背压**：客户端慢消费时，服务端 `queue` 无上限。256KB 上限 + 任务数量有限的场景下问题不大；真要做需要限制 queue 长度并丢弃旧 chunk，或切成 SSE retry 协议。
- **SSE 重连去重**：`init` 在重连时重发全量 buffer，客户端用覆盖而不是 append 保证最终一致；偶发"闪一下相同内容"可能让用户感到闪烁。想彻底解决要上 `Last-Event-ID` 做增量续传，复杂度陡升。

## 参考代码位置

cc-webui 仓库（commit `ee7424c`，2026-04-23）：
- `server/bash-mcp.ts` — MCP server + 注册表 + 三工具 + 订阅者机制 + 信号 handler
- `server/bash-tasks.ts` — REST + 两条 SSE 路由
- `server/chat.ts` — `mcpServers` / `disallowedTools` / `canUseTool` / `getSessionId` 接线 + session_id 迁移
- `src/lib/tasks.ts` — REST 客户端 + `subscribeTasksList` / `subscribeTaskStream`
- `src/lib/processor.ts` — `TOOL_ALIAS` + `summarize` 覆盖
- `src/components/TasksButton.tsx` — composer 右侧计数按钮
- `src/components/TasksModal.tsx` — 列表 + 实时输出 + kill 面板

## 相关

- [[MCP (Model Context Protocol)]]
- [[Function Calling]]
- [[Claude Code]]
- [[Tool Use 工具调用]]

- [[Claude Agent SDK 流式对话断线续流]] — 同套订阅者/fanout 模式升级到 HTTP 连接层，刷新/切 session 不再中断 SDK 生成
