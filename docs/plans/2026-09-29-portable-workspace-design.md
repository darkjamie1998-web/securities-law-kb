# 可移植工作空间便携包 — 设计文档（2026-09-29）

## 目标

生成一个可移植的项目文件夹，任意 Agent（Claude Code / Cursor / Codex / Gemini CLI 等）拿到后：
1. **零安装零网络**从零启动（全离线自包含运行时）
2. 可直接作为工作空间接手开发（含完整版本历史与 Agent 指令）

## 关键决策

| 决策点 | 结论 | 理由 |
|--------|------|------|
| 数据范围 | 仅带 knowledge.db（89MB），不带 raw/（365MB 采集缓存） | raw/ 仅在 `crawler/sync.py:179` 附件下载时使用，查询/问答/wiki/图谱只依赖 knowledge.db |
| 环境方案 | 全离线自包含：复制 `C:\Program Files\Python314` 完整安装（134MB）+ `.venv/Lib/site-packages`（224MB）→ `runtime/` | 目标机器可能无法访问公网 pip 源；复制完整安装与已验证环境 100% 一致、路径无关、零配置（弃用 embeddable：缺标准库、需改 ._pth） |
| 交付形式 | `D:\workmate_projects\法律法规知识库查询-便携版\` 文件夹 + 同级 zip | 文件夹直接用，zip 便于传输 |
| git 历史 | 打包前 `git add -A && git commit` 快照，.git 随包带走（427KB） | AGENTS.md 多处引用 git 历史教训；Agent 可继续提交 |
| Agent 指令 | AGENTS.md 增强（便携环境章节），CLAUDE.md 一行指针 `@AGENTS.md` | AGENTS.md 是通用标准（Codex/Cursor/Zed/Gemini 原生支持）；Claude Code 用 CLAUDE.md 引用 |

## 便携包结构

```
法律法规知识库查询-便携版/
├── runtime/                    # 便携 Python 3.14.5（完整安装 + site-packages 合并）
├── app/ crawler/ web/ tests/ docs/
├── data/knowledge.db           # 不含 config.json（密钥）、raw/
├── .git/
├── AGENTS.md / CLAUDE.md / README.md
├── requirements-full.txt       # 完整依赖（修正现有 requirements.txt 缺 playwright/olefile/pdfplumber/python-docx 等）
├── config.example.json
├── 环境自检.bat / 启动面板.bat   # GBK+CRLF，用 %~dp0runtime\python.exe
└── .gitignore / .gitattributes
```

**排除**：`.venv/`、`data/raw/`、`data/config.json`、`.pytest_cache/`、`__pycache__`、日志文件。

## 启动/自检流程

`环境自检.bat` 三项检查：
1. 运行时：`runtime\python.exe -c "import fastapi, bs4, httpx, yaml"` 
2. 数据库：`data\knowledge.db` 存在且可打开
3. 配置：`data\config.json` 不存在 → 提示复制 config.example.json 并填写 LLM 网关；**未配置时 FTS5 关键词检索仍可用**，语义检索/问答需配置

`启动面板.bat`：自检通过后 `runtime\python.exe -m uvicorn app.main:app` 并打开浏览器面板。

## 离线边界（README 明示，不夸大）

- LLM 问答/嵌入：需访问 LLM 网关（内网 API）
- `render` 来源增量同步：首次需 `runtime\python.exe -m playwright install chromium`（一次性联网）
- 其余（关键词检索、浏览法规、图谱、测试）：完全离线可用

## 执行步骤

1. 本设计文档入库 + git 快照提交全部未提交修改
2. `build_portable.py` 构建脚本（robocopy + 运行时合并 + 排除清单）
3. 启动件：改造 `启动面板.bat`、新建 `环境自检.bat`（Python 脚本生成，GBK+CRLF）
4. 文档层：重写 README（便携版）、增强 AGENTS.md（便携环境章节）、新建 CLAUDE.md、生成 requirements-full.txt
5. 验证：便携包内 pytest 全绿 → 启动服务 → API 200 → 中文路径可用
6. Python zipfile 打包（保证中文文件名兼容）

## 成功判据

- 任意路径（含中文）双击启动可用
- `runtime\python.exe -m pytest tests/` 全绿
- 不依赖任何系统 Python / .venv
- `git log` 完整
