---
tags:
  - agents
  - memory
  - context-management
created: "2026-04-09"
aliases:
  - LLM 记忆
  - Agent Memory
  - 长期记忆
---

# LLM Memory（记忆系统）

## 概述

LLM 本身是无状态的——每次对话从零开始，不保留之前的交互信息。Memory 系统的目标是让 LLM 拥有跨会话的持久记忆能力，使其能记住用户偏好、历史决策和累积知识。

Memory 不只是存储问题，更是 **结构化问题**：如何将嘈杂、非结构化的对话数据转化为高效可检索、对下游推理有效的表示。

## 记忆分类

### 按持续时间

| 类型 | 说明 | 类比 | 示例 |
|------|------|------|------|
| **工作记忆** (Working Memory) | 当前对话的上下文窗口 | CPU 寄存器 | 系统提示 + 当前对话 |
| **短期记忆** (Short-term) | 单次会话内的临时信息 | RAM | 对话摘要、FIFO 消息缓冲 |
| **长期记忆** (Long-term) | 跨会话持久化的知识 | 磁盘 | 用户偏好、历史事实、技能 |

### 按内容类型

| 类型 | 存储内容 | 示例 |
|------|---------|------|
| **语义记忆** (Semantic) | 事实和概念 | "用户是 Python 开发者" |
| **情景记忆** (Episodic) | 具体交互事件 | "上次讨论了数据库迁移方案" |
| **程序记忆** (Procedural) | 如何做事的技能 | "用户偏好的代码风格" |
| **实体记忆** (Entity) | 关键实体及其关系 | 人物、项目、工具的属性追踪 |

## 主流产品的实现

### ChatGPT Memory

OpenAI 采用 **自动化隐式** 方案：

- **触发**：AI 自动判断何时保存，每次对话自动预加载历史
- **存储**：AI 生成的记忆摘要和压缩档案（而非原始对话）
- **范围**：全局用户级，跨所有对话持续存在
- **操作**：自动提取事实 → 与已有记忆比较 → ADD / UPDATE / DELETE / NOOP
- **优点**：无缝个性化体验，"魔法般"记住一切
- **缺点**：上下文泄漏风险、全局范围不适合企业隔离、不透明

### Claude Memory

Anthropic 采用 **显式透明** 方案：

- **触发**：用户显式调用时才激活，对话始于空白状态
- **存储**：原始对话历史（无 AI 摘要/压缩），保证信息保真
- **检索**：通过两个工具函数实现
  - `conversation_search`：搜索过去对话找相关上下文
  - `recent_chats`：检索最近聊天，支持时间过滤和项目筛选
- **项目记忆**：可编辑的关键事实和指令总结（项目级隔离）
- **CLAUDE.md 模式**：Git 版本控制的上下文注入
- **优点**：高级控制、可预测、透明，适合受管制环境
- **缺点**：用户认知负担高，仅依赖人工管理增长缓慢

### Gemini Memory

Google 采用 **渐进式自动** 方案：

- 早期：用户可主动要求 Gemini "记住"某些偏好
- 现在：自动记忆，无需提示即可回忆关键细节和偏好
- 介于 ChatGPT（全自动）和 Claude（全手动）之间

### 核心差异总结

| 维度 | ChatGPT | Claude | Gemini |
|------|---------|--------|--------|
| 触发方式 | 自动 | 显式 | 自动（可选） |
| 存储形式 | AI 摘要 | 原始对话 | AI 摘要 |
| 隔离粒度 | 全局 | 项目级 | 全局 |
| 透明度 | 低 | 高 | 中 |

## Memory 框架生态

### Mem0

- **架构**：每次交互执行提取 → 与已有记忆比较 → ADD/UPDATE/DELETE/NOOP
- **存储**：三层隔离——用户级、会话级、Agent 级
- **特色**：自我编辑模型，冲突时更新而非创建重复；Pro 版支持图记忆
- **融资**：$24M
- **适用**：聊天机器人、个人助手、团队共享记忆

### Zep（Graphiti）

- **架构**：时间感知知识图谱引擎（Graphiti），动态合成非结构化对话和结构化业务数据
- **特色**：追踪事实的时间维度——不仅记录"什么"，还记录"何时"和"关系如何演变"
- **性能**：Deep Memory Retrieval 基准上 gpt-4o-mini 达 98.2%；时间检索任务 63.8%（vs Mem0 的 49.0%）
- **适用**：长时间运行的 Agent 会话、复杂工作流、需要时间推理的场景

### Letta（原 MemGPT）

- **架构**：操作系统范式——主上下文 = RAM（快但有限），外部存储 = 磁盘（慢但无限）
- **分层存储**：
  - 主上下文：静态系统提示 + 动态工作上下文 + FIFO 消息缓冲
  - 外部存储：召回存储（可搜索日志）+ 归档存储（向量语义检索）
- **特色**：Agent 自主决定何时换入/换出信息，类似 OS 的页面置换
- **适用**：复杂自主 Agent、需要高度定制化记忆管理的场景

### LangChain / LangGraph Memory

- **架构**：开源组件化，提供原始构建块
- **类型**：Buffer Memory、Window Memory、Summary Memory、Entity Memory、Knowledge Graph Memory
- **存储后端**：Pinecone、Chroma（向量）、Neo4j（图）、Redis（KV）
- **适用**：需要完全控制的自定义应用

### SuperLocalMemory

- **架构**：本地优先，四通道 RRF 融合（Fisher-Rao 几何 + BM25 + 实体图 + 时间衰减）
- **特色**：零云依赖，数据不离设备，Mode A 声称 EU AI Act "按架构合规"
- **性能**：Mode A 74.8%（零云），Mode C 87.7%（含云合成）
- **适用**：隐私优先场景、数据主权要求、零云成本需求

### 框架选型

| 需求 | 推荐 |
|------|------|
| 团队共享记忆 | Mem0 / Zep |
| 时间推理 + 关系查询 | Zep（Graphiti） |
| 复杂自主 Agent | Letta |
| 完全自定义 | LangChain Memory |
| 隐私 / 数据主权 | SuperLocalMemory |
| 开源 + 高性能 | SuperLocalMemory Mode C / Letta |

## LoCoMo 基准测试

| 系统                         | 得分     | 需要云 LLM |
| -------------------------- | ------ | ------- |
| SuperLocalMemory V3 Mode C | 87.7%  | 是（合成）   |
| Letta/MemGPT               | ~83.2% | 是       |
| SuperLocalMemory V3 Mode A | 74.8%  | **否**   |
| Supermemory                | ~70%   | 是       |
| Mem0（自报）                   | ~66%   | 是       |

> 需要云 LLM 的系统性能聚集在 83-92% 之间，架构差异才是决定性因素。

## 记忆生命周期

```
创建 → 编码 → 存储 → 检索 → 更新 → 遗忘
```

| 阶段 | 说明 | 技术 |
|------|------|------|
| **创建** | 从对话中提取值得记忆的信息 | LLM 抽取、规则过滤 |
| **编码** | 将信息转化为可存储的表示 | 向量嵌入、图节点、KV 对 |
| **存储** | 持久化到外部系统 | 向量 DB、图 DB、KV 存储 |
| **检索** | 根据当前上下文找到相关记忆 | 语义搜索、图遍历、时间过滤 |
| **更新** | 新信息与已有记忆合并/冲突解决 | Mem0 的 ADD/UPDATE/DELETE |
| **遗忘** | 淘汰过时或低价值记忆 | 时间衰减、使用频率、LLM 判断 |

## 存储方案

| 方案 | 优势 | 劣势 | 代表 |
|------|------|------|------|
| **向量数据库** | 语义相似度检索强 | 无关系推理能力 | Pinecone, Chroma, Qdrant |
| **知识图谱** | 关系推理、时间追踪 | 构建复杂度高 | Neo4j, Memgraph |
| **KV 存储** | 快速精确查找 | 无语义搜索 | Redis, MongoDB |
| **混合方案** | 兼顾各方优势 | 系统复杂度高 | Zep（图+向量）、Mem0 Pro |

## 行业趋势

1. **从直接共享 → RAG → 自主记忆编排**：记忆管理从被动检索走向 Agent 自主决策
2. **从非结构化 → 知识图谱**：关系推理和时间感知成为刚需
3. **从单 Agent → 多 Agent 记忆协调**：共享记忆、角色隔离、审计能力
4. **隐私合规成为选型关键**：EU AI Act（2026.8 生效）推动本地优先方案
5. **六类记忆分工协作**：Core / Episodic / Semantic / Procedural / Resource / Knowledge Vault，各由专属 Agent 管理

## 参考资料

- [Comparing the memory implementations of Claude and ChatGPT - Simon Willison](https://simonwillison.net/2025/Sep/12/claude-memory/)
- [Design Patterns for Long-Term Memory in LLM-Powered Architectures - Serokell](https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures)
- [5 AI Agent Memory Systems Compared - DEV Community](https://dev.to/varun_pratapbhardwaj_b13/5-ai-agent-memory-systems-compared-mem0-zep-letta-supermemory-superlocalmemory-2026-benchmark-59p3)
- [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory - arXiv](https://arxiv.org/abs/2504.19413)
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory - arXiv](https://arxiv.org/html/2501.13956v1)
- [The 6 Best AI Agent Memory Frameworks - MachineLearningMastery](https://machinelearningmastery.com/the-6-best-ai-agent-memory-frameworks-you-should-try-in-2026/)
- [Survey of AI Agent Memory Frameworks - Graphlit](https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks)
- [[Context Management|上下文管理]]
- [[Agents MOC|Agent 与工具]]
- [[MCP (Model Context Protocol)]]
