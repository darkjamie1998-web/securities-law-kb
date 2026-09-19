# 证券法律法规知识库查询 — 项目说明

通过公开官方来源采集和沉淀证券业务相关法律法规，建设 AI Agent 知识库（RAG 检索 + LLM 关联 wiki + 对话研究）。

## 核心文档

- **实施计划**：`docs/plans/2026-09-19-securities-law-kb-app.md` — 架构、数据库 Schema、M1-M4 全部任务与验证步骤。做任何开发前先读它。

## 技术栈与关键决策

- Python 3.14，虚拟环境 `.venv`（Windows: `.venv/Scripts/python.exe`）
- FastAPI + 原生 HTML/JS 单页（无前端框架、无 CDN 依赖）
- 大模型走 OpenAI 兼容 API（httpx 直调，配置存 `data/config.json`，勿提交）
- RAG = API embedding + SQLite FTS5 混合检索（RRF 融合），无本地嵌入模型
- wiki = LLM 关联图谱（批量）+ 法规解读（按需生成缓存）

## 常用命令

```bash
.venv/Scripts/python.exe -m pytest tests/ -v                            # 测试
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000            # 启动服务
.venv/Scripts/python.exe -m crawler.sync                                # 每日增量同步
.venv/Scripts/python.exe -m app.embed                                   # 新法条向量化
.venv/Scripts/python.exe -m app.relations --pending 10                  # 关系抽取
```

## 硬约束

- **反爬**：对官方站点一律走 `crawler/fetcher.py` 限频客户端（默认 ≥5s 间隔 + 抖动、串行、退避重试），严禁绕过；增量同步每日最多 1 次
- **合规**：仅采集公开发布的法律法规文本，来源清单在 `crawler/sources.yaml`，修改来源先确认其官方性质
- **数据**：`data/` 与 `config.json` 不入库（.gitignore）
- **引用**：LLM 回答必须带法规名+条号溯源，不得无依据输出
