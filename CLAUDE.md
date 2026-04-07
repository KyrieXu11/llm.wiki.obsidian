# LLM Wiki

这是一个用 Obsidian 管理的本地 LLM 知识库，路径：`~/Documents/llm.wiki`

## 结构

```
Home.md                         — 首页知识地图
Models/Models MOC.md            — 模型总览（GPT、Claude、Llama、Qwen、DeepSeek...）
Prompting/Prompting MOC.md      — Prompt 工程（CoT、ReAct、Few-shot...）
Fine-tuning/Fine-tuning MOC.md  — 微调训练（SFT、LoRA、RLHF、DPO...）
Agents & Tools/Agents MOC.md    — Agent 架构、Function Calling、MCP
Deployment/Deployment MOC.md    — 部署推理、量化优化
Evaluation/Evaluation MOC.md    — 评测基准（MMLU、HumanEval...）
Frameworks/Frameworks MOC.md    — 工具框架（LangChain、vLLM...）
Papers/Papers MOC.md            — 论文阅读笔记
Templates/                      — 笔记模板（通用、论文、工具框架）
Assets/                         — 附件存放
```

## 写作规范

- 使用 Obsidian Flavored Markdown，用 `[[wikilink]]` 互联笔记
- 每篇笔记顶部需要 YAML frontmatter，至少包含 `tags` 和 `created`
- 新笔记放入对应分类文件夹，并在该分类的 MOC 页面中添加链接
- 中文为主，技术术语保留英文（如 RAG、LoRA、CoT）
- 模板位于 `Templates/`，新建笔记时应参考对应模板格式

## MOC（内容地图）

每个分类文件夹都有一个 MOC 文件作为该领域的导航入口。新增笔记后需要在对应 MOC 中添加 wikilink。

## 插件

- Dataview：首页使用 dataview 查询展示最近更新和分类统计
