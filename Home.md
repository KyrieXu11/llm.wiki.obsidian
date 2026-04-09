---
aliases:
  - LLM Wiki
  - 首页
tags:
  - MOC
---

# LLM Wiki

欢迎来到我的 LLM 知识库！这里记录了大语言模型相关的学习笔记、实践经验和技术调研。

---

## 知识地图

### 基础理论
- [[Models MOC|模型总览]] — 主流 LLM 模型介绍与对比
- [[Evaluation MOC|评测基准]] — 模型评测方法与 Benchmark

### 应用实践
- [[Prompting MOC|Prompt 工程]] — 提示词设计与优化技巧
- [[Agents MOC|Agent 与工具]] — AI Agent 架构、Function Calling、MCP
- [[Memory MOC|记忆系统]] — LLM 记忆架构、Memory 框架、产品实现对比

### 工程部署
- [[Fine-tuning MOC|微调训练]] — SFT、RLHF、LoRA 等微调方法
- [[Infra MOC|基础设施]] — 部署推理、网络、Docker、沙箱隔离
- [[Frameworks MOC|工具框架]] — LangChain、LlamaIndex、vLLM 等

### 前沿研究
- [[Papers MOC|论文笔记]] — 重要论文阅读笔记

---

## 最近更新

```dataview
TABLE file.mtime AS "修改时间", file.folder AS "分类"
FROM "" AND -"Templates"
SORT file.mtime DESC
LIMIT 10
```

## 统计

```dataview
TABLE length(rows.file.name) AS "笔记数"
FROM "" AND -"Templates" AND -"Assets"
GROUP BY file.folder AS "分类"
```
