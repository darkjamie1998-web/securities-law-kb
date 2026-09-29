# 证券法律法规知识库查询 — 便携版

通过公开官方来源采集和沉淀证券业务相关法律法规，建设 AI Agent 知识库：
**RAG 法条检索 + LLM 关联图谱 wiki + 带引用溯源的大模型对话研究**。

这是**全离线自包含便携包**：内嵌完整 Python 运行时（`runtime\`，Python 3.14.5 + 全部依赖）
和知识库数据库（`data\knowledge.db`，已含全部已采集法规）。目标机器**无需安装 Python、无需联网下载依赖**。

## 快速开始（三分钟）

```text
1. 双击 环境自检.bat    → 三项检查：运行时 / 数据库 / 大模型配置
2. 双击 启动面板.bat    → 启动服务，浏览器自动打开 http://localhost:8000
3. 开始使用：左侧导航浏览法规，顶部搜索框检索，右下角 💬 对话研究
```

**runtime\ 目录丢失或未随包分发？** 环境自检会自动转配：检测到本机装有 Python 3.9+（`py` / `python` 命令任一）时，
自动在**项目文件夹内**创建 `.venv` 虚拟环境并安装全部依赖（经 `tools/setup_env.py`，需要网络；
内网可加镜像参数）。**全程不修改系统 PATH、不装全局包**。本机连 Python 都没有时，
自检会给出两条出路（从 zip 恢复 runtime\ 目录，或先装 Python 再重跑自检）。

**首次配置大模型**（可选）：复制 `config.example.json` 为 `data\config.json`，填入 OpenAI 兼容 API
的地址/Key/模型名（或启动后在页面右上 ⚙ 填写）。

- 未配置时：**关键词检索、法规浏览、知识图谱完全可用**（纯本地 SQLite FTS5）
- 配置后：语义检索（向量混合）、LLM 问答、wiki 解读、关联图谱增量生成

## 把它当作 Agent 工作空间

本文件夹可直接作为任意 Agent（Claude Code / Cursor / Codex / Gemini CLI 等）的工作空间：

- **`AGENTS.md`** — Agent 第一入口：项目硬约束、可靠性机制、环境探测方法、常用命令（Cursor/Codex/Gemini 原生读取）
- **`CLAUDE.md`** — Claude Code 入口（内容为 `@AGENTS.md` 自动内联）
- **`docs/plans/`** — 完整架构设计与实施计划（开发前先读 `2026-09-19-securities-law-kb-app.md`）
- **`.git/`** — 完整版本历史，含全部事故教训记录；Agent 可继续提交
- Agent 执行命令统一用 `runtime\python.exe`（详见 AGENTS.md「运行环境探测」）

验证环境是否健康：`runtime\python.exe -m pytest tests/ -v`（全绿即环境完好）。

## 离线边界（诚实说明）

| 能力 | 是否离线 |
|------|---------|
| 关键词检索、法规浏览、条文详情、知识图谱（已生成部分） | ✅ 完全离线 |
| 测试、代码修改、审计 | ✅ 完全离线 |
| LLM 问答 / wiki 解读 / 关系抽取 | 需访问 LLM 网关（`data/config.json` 配置的 API） |
| 语义向量检索 | 需网关支持 embeddings 接口（当前网关不支持则自动跳过） |
| `render` 来源的增量同步 | 首次需 `runtime\python.exe -m playwright install chromium`（一次性联网装浏览器） |
| 普通来源增量同步 | 需访问对应官方网站（爬虫自带限频合规） |

## 目录结构

```text
runtime\    内嵌 Python 3.14.5 运行时 + 全部依赖（约 350MB，勿动）
app\        服务层（FastAPI、检索、对话、wiki、配置、图谱、审计）
crawler\    采集层（来源配置、限频爬虫、解析、同步、定向补充）
web\        前端单页（原生 HTML/CSS/JS，无外部依赖）
data\       运行数据（knowledge.db；config.json 需自建，含密钥勿外传）
docs\       设计文档与实施计划
tests\      单元测试
环境自检.bat / 启动面板.bat / relations.bat
```

## 故障排查

| 症状 | 处理 |
|------|------|
| 自检提示"未找到本地运行时" | 正常现象：有系统 Python 时会**自动转配**项目内 `.venv`（需网络）；无 Python 则按提示恢复 `runtime\` 或先装 Python |
| 自动配置时依赖安装失败（无网络/内网源不可达） | `python tools/setup_env.py --mirror <内网pip镜像地址>`；或从 zip 恢复 `runtime\`（零网络） |
| 自检依赖导入失败 | `runtime\Lib\site-packages` 被破坏时删除 `runtime\` 走自动配置；`.venv` 场景删除 `.venv\` 后重跑自检 |
| 页面能开但搜索报错 | 检查 `data\knowledge.db` 是否存在（自检会查）；从原包恢复 |
| LLM 问答无响应 | 检查 `data\config.json` 的 api_base/api_key；网关需内网可达 |
| 关联图谱很久不更新 | `relations.bat` 需要配置好 LLM 且网关可用；看 `data\relations_forever.log` |
| 端口 8000 被占用 | 编辑 启动面板.bat，把 `--port 8000` 改成其他端口 |

## 合规声明

仅采集公开发布的法律法规文本；全部抓取走限频客户端（≥5s 间隔 + 抖动、串行、退避重试、
robots.txt 检查、单来源单日上限）。来源清单见 `crawler/sources.yaml`。增量同步每日最多 1 次。

---
完整设计文档：`docs/plans/` · 完整版本历史：`git log` · Agent 指令：`AGENTS.md`
