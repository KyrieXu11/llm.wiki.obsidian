# LLM Wiki

这是一个用 Obsidian 管理的本地 LLM 知识库，路径：`~/Documents/llm.wiki`

## 结构

```
Home.md                         — 首页知识地图
Models/Models MOC.md            — 模型总览（GPT、Claude、Llama、Qwen、DeepSeek...）
Prompting/Prompting MOC.md      — Prompt 工程（CoT、ReAct、Few-shot...）
Fine-tuning/Fine-tuning MOC.md  — 微调训练（SFT、LoRA、RLHF、DPO...）
Agents & Tools/Agents MOC.md    — Agent 架构、Function Calling、MCP
Infra/Infra MOC.md              — 基础设施（部署推理、网络、Docker、沙箱隔离）
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

## 查询方式

优先使用 Obsidian CLI 而非文件系统的 Grep/Glob 查询 wiki 内容：

```bash
# 全文搜索（利用 Obsidian 内置索引，比 Grep 更快）
obsidian vault="llm.wiki" search query="关键词" format=json

# 带上下文的搜索（返回匹配行号和内容）
obsidian vault="llm.wiki" search:context query="关键词" limit=5 format=json

# 按笔记名读取（wikilink 风格，无需完整路径）
obsidian vault="llm.wiki" read file="笔记名"

# 反向链接查询
obsidian vault="llm.wiki" backlinks file="笔记名"

# 标签列表
obsidian vault="llm.wiki" tags

# 属性查询
obsidian vault="llm.wiki" property:get name="属性名" file="笔记名"
```

只在需要正则匹配或 Obsidian 未运行时回退到 Grep/Glob。

## 插件

- Dataview：首页使用 dataview 查询展示最近更新和分类统计
