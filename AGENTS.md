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

## 运行环境探测（先做这一步）

本项目有两种运行形态，同一份代码，解释器不同。**执行任何命令前先探测**：

1. **便携版**：存在 `runtime\python.exe`（内嵌完整 Python 3.14.5 + 全部依赖，零安装零网络）→ 所有命令用 `runtime\python.exe`
2. **开发版**：存在 `.venv\Scripts\python.exe` → 所有命令用 `.venv\Scripts\python.exe`，依赖按 `requirements-full.txt`（完整冻结清单；旧 requirements.txt 不全，勿再用）
3. 两者都不存在 → 运行 `环境自检.bat`：有系统 Python（≥3.9）时会自动创建项目内 `.venv` 并装依赖（`tools/setup_env.py`，仅动项目文件夹，不改系统环境；内网加镜像参数 `--mirror <URL>`）；无 Python 则提示恢复 `runtime\`

下文命令统一写 `$PY`（bash 下定义 `PY=runtime/python.exe` 或 `PY=.venv/Scripts/python.exe`）。

## 常用命令

```bash
$PY -m pytest tests/ -v                            # 测试
$PY -m uvicorn app.main:app --port 8000            # 启动服务
$PY -m crawler.sync                                # 每日增量同步
$PY -m app.embed                                   # 新法条向量化
$PY -m app.relations --pending 10                  # 关系抽取
$PY -m app.audit                                   # 图谱完整性审计
```

**便携包构建**：`.venv/Scripts/python.exe build_portable.py` — 全离线自包含包（内嵌 Python 运行时 + knowledge.db），**产物固定输出到 `dist/`**（`dist/法律法规知识库查询/` + 同名 zip；脚本自动清理重建，`dist/` 不入 git）。分发一律从 dist/ 取最新产物。

## 硬约束

- **反爬**：对官方站点一律走 `crawler/fetcher.py` 限频客户端（默认 ≥5s 间隔 + 抖动、串行、退避重试），严禁绕过；增量同步每日最多 1 次
- **合规**：仅采集公开发布的法律法规文本，来源清单在 `crawler/sources.yaml`，修改来源先确认其官方性质
- **数据**：`data/` 与 `config.json` 不入库（.gitignore）
- **引用**：LLM 回答必须带法规名+条号溯源，不得无依据输出

## 可靠性机制（2026-09-23 引入，改动前先理解原因）

- **LLM 调用硬超时**（`app/llm.py`）：网关"连接活着但不回响应"时 httpx 库级超时会失效（曾致图谱进程挂死 22 小时），所以 chat/embed 都走线程池 + `fut.result(hard_timeout)` 兜底。**不要移除这层保护**
- **关系抽取标记表**（`relation_extractions`）：pending 判定看标记而非"是否有关系产出"——天然无关系的法规抽取一次即完成，防止无限重试烧 LLM。`crawler/sync.py` 更新法规内容时会连带清掉该法规的 relations/标记/wiki（FK 无 CASCADE，必须显式删）
- **图谱 runner**（`run_relations_forever.py`）：msvcrt 实例文件锁（进程死亡自动释放）+ 看门狗（45 分钟无产出自杀）。**同机只能跑一个实例**
- **前端 XSS 防线**：`web/app.js` 的 `renderInline/renderMarkdown` 内部自带 `esc()`——新增渲染路径**不要**先手动 esc 再传入（会双重转义）；外链一律过 `safeUrl()`
- **前端语法兼容底线 ES2017**（2026-09-28 搜索失效事故教训）：用户在金融内网用旧版浏览器（Chromium <80），`??`/`?.`（ES2020）、`trimEnd`（ES2019）这类语法会**挂掉整个 app.js 文件**（语法错误是文件级的，一处不支持全部交互失效）。写前端代码只用 ES2017 及以下语法；CSS 的新函数（`min()` 等）必须先写旧值回退再写新值

## Windows 环境坑

- **本项目路径含中文**：`cmd //c`、`start` 等 shell 桥接找不到中文路径下的文件（bash UTF-8 → cmd GBK 转换失败，历史上多次踩坑）。**启动独立后台进程用 Python**：`subprocess.Popen(..., creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB)`，不要用 cmd start
- **必须带 `CREATE_BREAKAWAY_FROM_JOB`**（2026-09-28 事故教训）：只用 DETACHED_PROCESS 不够——子进程仍在 agent 会话的 Job Object 里，**关闭受管浏览器（BrowserClose）或会话清理时会级联杀死服务**（表现为"服务莫名消失"、页面 fetch 全失败）。BREAKAWAY 让进程脱离作业对象，已实测 BrowserClose 后服务存活
- **bat 文件编码规范（2026-09-28 定案，start.bat 双击失败事故的教训）**：一律 **GBK 编码 + CRLF 行尾**，且**不用 `chcp 65001`**。理由：① cmd 期望 CRLF，LF-only 批处理解析不可靠；② `chcp 65001` 切换点后 cmd 按新代码页重读文件缓冲，中文行字节边界错位会产生乱码命令；GBK 是中文 Windows 原生代码页，零转换零切换。`.gitattributes` 已设 `*.bat -text`（git 不做行尾归一，GBK 字节原样入库）。**修改 bat 时用 Python 脚本生成**（参考 git 历史），不要用会写 UTF-8+LF 的工具直接编辑
- **WorkMate Bash 工具会替换 `NUL`→`/dev/null`**：bat 内容里有 `>NUL` 时不能内联在 bash 命令里（会被转换成 Unix 语法），必须经脚本文件生成
- bat 文件里 REM 注释只用 ASCII；echo 中文没问题（GBK 下正常显示）
- **bat 调 Python 输出中文必须先 `set PYTHONIOENCODING=gbk`**（2026-09-29 环境自检乱码教训）：机器若全局设了 `PYTHONUTF8=1`（或未来 Python 默认 UTF-8），`python -c "print('中文')"` 在 GBK 控制台上输出 UTF-8 字节变乱码；bat 开头统一设 GBK 后，`python -c`、setup_env.py、uvicorn 输出全部正常。Python 脚本内部再用 `sys.stdout.reconfigure(encoding='gbk')` 双保险（参考 `tools/setup_env.py`）
- `.venv\Scripts\python.exe` 在进程表里呈现父子两个 PID（launcher stub + 真实进程），判断"几个实例"时勿误判
