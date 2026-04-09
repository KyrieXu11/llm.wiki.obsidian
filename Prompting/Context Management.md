---
tags:
  - prompting
  - context-management
  - RAG
  - agents
created: "2026-04-09"
aliases:
  - 上下文管理
  - Context Engineering
  - 上下文工程
---

# Context Management（上下文管理）

## 概述

Context Management 是构建 LLM 应用时的核心工程问题：如何在有限的上下文窗口中，填入最相关、最高效的信息，使模型输出质量最大化。Andrej Karpathy 将其定义为 **"Context Engineering — the delicate art and science of filling the context window with just the right information"**。

随着模型窗口从 4K 扩展到 1M+ tokens，上下文管理的重点已从"如何塞进去"转向"如何选择、压缩、组织信息"。研究表明，即使窗口足够大，**盲目填充会导致性能下降**（Lost in the Middle 问题）和成本激增。

## 核心挑战

### Lost in the Middle 问题

LLM 对上下文中信息的利用呈 **U 型曲线**：开头和末尾的信息被有效利用，中间部分容易被"遗忘"。这意味着即使窗口足够大，信息的位置编排同样关键。

### 成本与延迟

长上下文处理带来几何级别的成本增长。策略性的缓存、压缩和上下文工程可将成本降低 50-90%。

### 信息质量 vs 数量

更多的上下文不等于更好的输出。噪声信息会稀释关键信号，降低模型推理精度。

## 六大核心技术

### 1. 截断法（Truncation）

最简单的策略：输入超限时直接截断多余 token。

- **优点**：实现简单、计算开销低、兼容所有 LLM
- **缺点**：无语义理解、可能误删关键信息
- **适用**：对信息损失容忍度高的简单文本处理

### 2. 路由到大模型（Routing to Larger Models）

检测 token 数量，超限时自动路由到更大上下文窗口的模型。

- **优点**：保留完整上下文、易集成
- **缺点**：大模型成本高、延迟不可控
- **适用**：成本充足且需要完整信息的场景

### 3. 记忆缓冲（Memory Buffering）

存储对话历史，定期总结旧消息，保留关键实体。常见实现：

- **Sliding Window**：仅保留最近 N 轮对话，简单但会丢失早期信息
- **ConversationSummaryBufferMemory**：最近对话保留原文 + 旧对话自动摘要，token 超限时最老消息被总结并合并
- **Entity Memory**：追踪对话中的关键实体及其状态

**适用**：多轮对话应用（客服、助手），需要记忆历史决策的场景

### 4. 分层总结（Hierarchical Summarization）

将文本分块逐级总结，形成金字塔结构：原文 → 段落摘要 → 章节摘要 → 全文摘要。

- **优点**：高效处理长文本、支持多粒度查询
- **缺点**：早期错误逐层传播、增加延迟
- **适用**：长篇文档处理（书籍、合同、报告）

### 5. 上下文压缩（Context Compression）

移除冗余词汇和非必要短语，保留原始语义。可实现 **40-60% token 缩减**，高级技术（总结 + 关键词提取 + 语义分块）可达 **5-20x 压缩**。

核心技术路线：
- **Prompt 压缩**：LLMLingua、LongLLMLingua 等工具自动压缩
- **关键短语提取**：提取核心概念，丢弃冗余叙述
- **语义分块**：按语义边界而非固定长度切分

**适用**：高成本模型、包含日志/转录的冗长输入

### 6. [[RAG]] 检索增强生成

将文档向量化存储，运行时仅检索与查询最相关的片段注入上下文。

- **优点**：仅获取相关信息、支持动态数据更新、可扩展至大规模数据源
- **缺点**：依赖检索质量、实现复杂度高
- **进化方向**：从简单检索到 Context Engine（混合检索 + 元数据过滤 + Reranking + 结构化分块）

Context-aware RAG 系统可在 **减少 68% 上下文大小** 的同时保留 **91% 关键信息**。

**适用**：问答系统、知识库搜索、需要引用来源的场景

## Context Engineering 四大策略

| 策略           | 说明                      | 示例                    |
| ------------ | ----------------------- | --------------------- |
| **Write**    | 主动生成上下文（scratchpad、思维链） | CoT 推理、Agent 的工作记忆    |
| **Select**   | 从候选池中选择最相关的信息           | RAG 检索、Few-shot 示例选择  |
| **Compress** | 压缩已有上下文减少 token         | 对话摘要、Prompt 压缩        |
| **Isolate**  | 将子任务分配给独立上下文            | 子 Agent 并行、Map-Reduce |

## Agent 上下文管理

### Observation Masking vs LLM Summarization

JetBrains Research 的研究（2025.12）在 SWE-bench Verified 上对比了两种方案：

| 方案                      | 原理                            | 效果                     |
| ----------------------- | ----------------------------- | ---------------------- |
| **Observation Masking** | 保留推理和动作历史，用占位符替换旧 observation | 5 个场景中 4 个更优，成本降低 52%  |
| **LLM Summarization**   | 用另一模型压缩交互历史                   | 理论上无限扩展，但运行时间增加 13-15% |

**结论**：简单方案（Observation Masking）往往在总体效率和可靠性方面胜出。推荐 **混合策略**：以 Observation Masking 作为第一道防线，必要时才使用 LLM 总结。

### [[MCP (Model Context Protocol)]]

MCP 作为上下文管理的开放标准，为 LLM 提供结构化、有权限控制的元数据，替代原始数据堆砌，提升上下文质量并减少 token 浪费。

## Prompt Caching

首次发送大段文本时，LLM 处理并将 attention 状态缓存至高速内存。后续请求复用缓存，**可降低 60-90% 成本**。

- Anthropic: 支持系统提示、工具定义和对话前缀的缓存
- OpenAI: 自动缓存重复前缀
- **Cache-augmented Generation**：预计算高频文档缓存，比 RAG 更快（无需检索步骤）

## 前沿研究（2025-2026）

| 技术 | 方向 |
|------|------|
| **FlashAttention-3** | H100 上达 1.3 PFLOPs/s，加速长上下文推理 |
| **Ring Attention** | 分布式扩展上下文处理 |
| **OmniKV** | 动态上下文选择，高效长上下文 LLM |
| **WindowKV** | 任务自适应 KV Cache 窗口选择 |
| **xKV** | 跨层 SVD 压缩 KV Cache |
| **TTT-E2E** | 测试时训练，2M context 场景下 35x 加速 |

## 技术选型速查

| 应用类型 | 推荐技术 |
|---------|--------|
| 多轮长对话（客服、助手） | 记忆缓冲（Sliding Window + Summary） |
| 长文本处理（书籍、合同） | 分层总结 |
| 高精度问答 + 来源追踪 | RAG |
| 高成本模型降本 | 上下文压缩 + Prompt Caching |
| 实时客服 | RAG + 压缩混合 |
| Agent 长任务 | Observation Masking + 子 Agent 隔离 |
| 医疗/法律敏感内容 | RAG（精确检索，避免总结） |

## 参考资料

- [Top techniques to Manage Context Lengths in LLMs - Agenta](https://agenta.ai/blog/top-6-techniques-to-manage-context-length-in-llms)
- [The LLM context problem in 2026 - LogRocket](https://blog.logrocket.com/llm-context-problem/)
- [Cutting Through the Noise: Smarter Context Management for LLM-Powered Agents - JetBrains Research](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [LLM Context Window Management and Long-Context Strategies 2026 - Zylos Research](https://zylos.ai/research/2026-01-19-llm-context-management)
- [Context Engineering: The Definitive 2025 Guide - FlowHunt](https://www.flowhunt.io/blog/context-engineering/)
- [Context Window Overflow in 2026 - Redis](https://redis.io/blog/context-window-overflow/)
- [Beyond the Limits: A Survey of Techniques to Extend the Context Length in LLMs - arXiv](https://arxiv.org/html/2402.02244v3)
- [[Prompting MOC|Prompt 工程]]
- [[Agents MOC|Agent 与工具]]
- [[Memory MOC|记忆系统]]
- [[RAG]]
