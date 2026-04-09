---
aliases:
  - 记忆系统
tags:
  - MOC
  - memory
---

# 记忆系统

## 核心概念
- [[LLM Memory]] — 记忆分类、生命周期、存储方案综述

## 产品实现
- [[LLM Memory#主流产品的实现|主流产品对比]] — ChatGPT / Claude / Gemini 的记忆方案

## Memory 框架
- [[LLM Memory#Mem0|Mem0]] — 自我编辑式记忆，ADD/UPDATE/DELETE 操作
- [[LLM Memory#Zep（Graphiti）|Zep / Graphiti]] — 时间感知知识图谱
- [[LLM Memory#Letta（原 MemGPT）|Letta / MemGPT]] — 操作系统范式，自主换页
- [[LLM Memory#LangChain / LangGraph Memory|LangChain Memory]] — 开源组件化构建块
- [[LLM Memory#SuperLocalMemory|SuperLocalMemory]] — 本地优先，零云依赖

## 相关页面
- [[Context Management|上下文管理]] — 上下文工程与压缩策略
- [[Agents MOC|Agent 与工具]]
- [[MCP (Model Context Protocol)]]
