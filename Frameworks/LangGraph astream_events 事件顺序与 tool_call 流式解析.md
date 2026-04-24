---
tags:
  - frameworks
  - langgraph
  - streaming
  - tool-calling
created: "2026-04-16"
aliases:
  - astream_events ordering
  - tool_call streaming
---

# LangGraph astream_events 事件顺序与 tool_call 流式解析

## 背景

在基于 [[LangGraph]] + [[Deep Agents]] 的合同审查系统中，使用 `astream_events(version="v2")` 流式处理 LLM 的 tool call 输出。需要明确事件顺序以判断是否存在竞态 bug。

## 核心结论

**`on_tool_start` 事件一定在所有 `on_chat_model_stream`（含 `input_json_delta`）之后到达，不存在竞态。**

## 事件管道

`astream_events` 的事件来自两个不同层：

### LLM 流式层（on_chat_model_stream）

模型生成 token 时逐块发出，包含：
- `tool_use` block：工具名称和 ID
- `input_json_delta` block：工具参数的 JSON 碎片
- `text_delta` block：普通文本
- `thinking_delta` block：思考内容

### 工具执行层（on_tool_start）

工具开始执行时发出，携带完整的 `tool_input`。

## 顺序保证的原因

```
on_chat_model_stream（tool_use）
on_chat_model_stream（input_json_delta chunk 1）
on_chat_model_stream（input_json_delta chunk 2）
...
on_chat_model_stream（input_json_delta chunk N）
    ↓
model node 返回（所有 token 生成结束）
    ↓
LangGraph 路由到 tools node
    ↓
on_tool_start（工具开始执行，携带完整 input）
```

三层保证：

1. **LangGraph 的 node 级顺序执行**：`model` node 和 `tools` node 是图中两个节点，`model` node 必须完整返回后 LangGraph 才路由到 `tools` node（见 `langchain/agents/factory.py` L1335-1360）

2. **model node 内部的流式完整性**：`on_chat_model_stream` 事件在 model node 的 async generator 内部产生，generator 结束后 node 才返回

3. **事件通道的 FIFO 语义**：所有事件通过 `send_stream.send_nowait()` 按序放入同一个 channel（见 `langchain_core/tracers/event_stream.py` L167-170），消费端按 FIFO 顺序读取

## 并行 tool call 的情况

当 LLM 在一次响应中发起多个 tool call 时：

```
on_chat_model_stream: tool_use(Tool A, index=1)
on_chat_model_stream: input_json_delta(index=1, chunk1)
on_chat_model_stream: input_json_delta(index=1, chunk2)
on_chat_model_stream: tool_use(Tool B, index=3)
on_chat_model_stream: input_json_delta(index=3, chunk1)
on_chat_model_stream: input_json_delta(index=3, chunk2)
    ↓ model node 完成
on_tool_start(Tool A, 完整 input)
on_tool_start(Tool B, 完整 input)
```

Anthropic API 的 content block 是顺序流式的（不交错），但不同 block 的 delta 之间可能穿插。需要按 `index` 追踪每个 tool call 的 delta 归属。

## input_json_delta 流式解码

### 追踪机制

使用 `dict[int, str]` 按 content block index 映射 tool name：

```python
_tool_name_by_index: dict[int, str] = {}

# tool_use block → 记录 index → name 映射
if block.get('type') == 'tool_use':
    _tool_name_by_index[block['index']] = block['name']

# input_json_delta → 按 index 查找对应的 tool name
if block.get('type') == 'input_json_delta':
    tool_name = _tool_name_by_index.get(block.get('index'))
```

### OutputMessage 增量 JSON 解码

`_OutputMessageStreamDecoder` 实现了从 `input_json_delta` 碎片中实时提取 `markdown` 字段值：

1. **定位阶段**：缓冲 chunk 直到匹配 `"markdown"\s*:\s*"` 前缀
2. **解码阶段**：逐字符状态机解码 JSON string，处理 `
`、`\uXXXX` 转义和 surrogate pair 跨 chunk 拼接
3. **finish 兜底**：tool call 完成时用完整 markdown 值补发差量

## 常见误解

### "表格内容瞬间出现是 bug"

不是 bug。LLM 生成结构化表格内容时（从 thinking 中已分析好的数据格式化输出），token 生成速度接近上限。Console 日志可以验证：多个小 chunk 确实在同一秒内到达，说明后端流式解析正常工作，只是 LLM 生成太快。

### "on_tool_start 可能提前到达导致 finish() 一次性 flush"

不会发生。如上所述，event ordering 有三层保证。`finish()` 在 `on_tool_start` 到达时被调用，但此时所有 delta 已经处理完毕，`finish()` 只会返回空字符串或极少的残余内容（如 pending surrogate）。

## 源码参考

| 文件 | 位置 | 内容 |
|------|------|------|
| `langchain_core/tracers/event_stream.py` | L167 | `_send()` 方法，FIFO channel |
| `langchain_core/tracers/event_stream.py` | L456 | `on_chat_model_stream` 事件发送 |
| `langchain_core/tracers/event_stream.py` | L655 | `on_tool_start` 事件发送 |
| `langchain/agents/factory.py` | L1335-1360 | model node / tools node 定义和图连接 |

## 相关笔记

- [[Deep Agents]]
- [[LangGraph]]
