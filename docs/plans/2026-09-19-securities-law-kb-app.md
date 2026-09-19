# 证券法律法规知识库 AI Agent — 实施 Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 建设一个基于公开官方来源的证券法律法规知识库 AI Agent，支持 RAG 精确检索法条、LLM 关联图谱 wiki、以及带引用溯源的大模型对话研究。

**Architecture:** 四层架构——采集层（Python 爬虫，多官方来源、严格限频）→ 知识层（SQLite 单文件：法条级 RAG + 关联关系图谱 + wiki 缓存）→ 服务层（FastAPI 提供 REST API）→ 交互层（无框架静态 HTML 单页）。对话采用 OpenAI 兼容 API + tool calling 实现 Agent 自主多轮检索。

**Tech Stack:** Python 3.14（项目 `.venv`，已创建并装好 fastapi/uvicorn/httpx/bs4/lxml）、SQLite（FTS5 + 向量 JSON 存储）、FastAPI、原生 HTML/CSS/JS。**不使用**：torch/transformers（本地嵌入）、任何前端框架、外部 CDN。

**关键设计决策（已与用户确认）：**
1. 大模型接入：OpenAI 兼容 API（地址/Key/模型名均可在页面配置）
2. 技术栈：FastAPI + 静态 HTML
3. RAG：API embedding + FTS5 关键词混合检索（无本地嵌入模型）
4. LLM wiki：关联图谱批量生成 + 法规解读按需生成并缓存
5. 反爬：每站串行、默认 ≥5s 间隔 + 随机抖动、指数退避、每日增量检查

---

## 项目文件结构（最终形态）

```
法律法规知识库查询/
├── .venv/                      # 已创建（Python 3.14.5）
├── AGENTS.md                   # 项目入口说明
├── requirements.txt            # 依赖清单
├── config.example.json         # 模型配置样例（真实 config.json 不入库）
├── docs/plans/2026-09-19-securities-law-kb-app.md   # 本计划
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 入口 + 静态文件挂载
│   ├── db.py                   # SQLite 连接、建表、schema 迁移
│   ├── models.py               # Pydantic 请求/响应模型
│   ├── config.py               # config.json 读写（API 地址/Key/模型名）
│   ├── llm.py                  # OpenAI 兼容 API 客户端（httpx 直调，chat + embedding）
│   ├── search.py               # 混合检索（向量余弦 + FTS5，RRF 融合）
│   ├── embed.py                # 批量 embedding 生成与入库
│   ├── relations.py            # 关联图谱生成（LLM 结构化抽取）
│   ├── wiki.py                 # wiki 解读按需生成与缓存
│   └── chat.py                 # Agent 对话循环（tool calling，多轮检索）
├── crawler/
│   ├── __init__.py
│   ├── fetcher.py              # 限频 HTTP 客户端（退避、抖动、robots 检查）
│   ├── parsers.py              # 各来源页面/正文解析（含 PDF 文本提取）
│   ├── sources.yaml            # 官方来源清单（可编辑）
│   ├── sync.py                 # 增量同步编排（CLI: python -m crawler.sync）
│   └── full_import.py          # 首次全量入库编排
├── web/
│   ├── index.html              # 单页应用
│   ├── style.css
│   └── app.js
├── data/                       # 运行时生成，gitignore
│   ├── knowledge.db
│   └── raw/                    # 原始抓取留档（按来源/日期）
└── tests/
    ├── test_parsers.py
    ├── test_search.py
    └── test_chat.py
```

## 数据库 Schema（`app/db.py` 实现）

```sql
CREATE TABLE laws (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,            -- 《证券法》
    doc_number TEXT,                -- 主席令第X号
    issuer TEXT,                    -- 发文机关
    level TEXT NOT NULL,            -- 法律/行政法规/部门规章/规范性文件/自律规则/司法解释
    topic_tags TEXT,                -- 逗号分隔主题标签
    issue_date TEXT, published_at TEXT, effective_date TEXT,
    status TEXT DEFAULT '现行有效', -- 现行有效/已修订/已废止
    source_name TEXT, source_url TEXT,
    content_hash TEXT UNIQUE,       -- 正文哈希，去重与变更检测
    full_text TEXT,
    created_at TEXT, updated_at TEXT
);

CREATE TABLE articles (             -- 法条 = RAG 检索单元
    id INTEGER PRIMARY KEY,
    law_id INTEGER NOT NULL REFERENCES laws(id),
    article_no TEXT,                -- 第X条 / 第X条第X款
    text TEXT NOT NULL,
    embedding TEXT                  -- JSON 数组，NULL=尚未向量化
);
CREATE INDEX idx_articles_law ON articles(law_id);

CREATE VIRTUAL TABLE articles_fts USING fts5(
    text, article_no, content='articles', content_rowid='id', tokenize='unicode61'
);

CREATE TABLE relations (
    id INTEGER PRIMARY KEY,
    from_id INTEGER NOT NULL REFERENCES articles(id),
    to_id INTEGER NOT NULL REFERENCES articles(id),
    rel_type TEXT NOT NULL,         -- 引用/修订替代/上位法/同一事项/程序衔接
    note TEXT, created_at TEXT,
    UNIQUE(from_id, to_id, rel_type)
);

CREATE TABLE wiki_pages (
    id INTEGER PRIMARY KEY,
    law_id INTEGER NOT NULL UNIQUE REFERENCES laws(id),
    markdown TEXT NOT NULL,
    model_used TEXT, generated_at TEXT
);

CREATE TABLE sync_log (
    id INTEGER PRIMARY KEY,
    source_name TEXT, started_at TEXT, finished_at TEXT,
    new_count INTEGER, updated_count INTEGER,
    status TEXT, detail TEXT        -- ok/partial/failed + 失败明细
);
```

---

# 阶段 M1：项目骨架与服务层

### Task 0: git 初始化与依赖清单

**Files:** Create `requirements.txt`、`.gitignore`

**Step 1:** `git init`，创建 `.gitignore`：

```text
.venv/
data/
__pycache__/
*.pyc
config.json
```

**Step 2:** 创建 `requirements.txt`（全部已装或标准库，零新增）：

```text
fastapi==0.141.1
uvicorn[standard]
httpx==0.28.1
beautifulsoup4
lxml
python-multipart
```

**Step 3:** 验证 FTS5 可用（决定检索方案是否成立）：

```bash
.venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute(\"CREATE VIRTUAL TABLE t USING fts5(x)\"); print('FTS5 OK')"
```

Expected: `FTS5 OK`。若失败则改用 LIKE 降级方案并在 plan 中记录。

**Step 4:** `git add -A && git commit -m "chore: init project skeleton"`

### Task 1: 数据库层 `app/db.py`

**Files:** Create `app/__init__.py`、`app/db.py`；Test `tests/test_db.py`

**Step 1: 写失败测试**

```python
# tests/test_db.py
import pytest
from app.db import get_db, init_db

def test_init_and_insert_law(tmp_path):
    db = get_db(tmp_path / "test.db")
    init_db(db)
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("证券法", "法律", "abc123"))
    law_id = cur.lastrowid
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第一条", "为了规范证券发行和交易行为…"))
    row = db.execute("SELECT title FROM laws WHERE id=?", (law_id,)).fetchone()
    assert row[0] == "证券法"
```

**Step 2:** 运行 `pytest tests/test_db.py -v`，预期 FAIL（No module named app.db）

**Step 3: 实现** `app/db.py`：`get_db(path)` 返回连接（`row_factory=sqlite3.Row`，开启外键），`init_db(db)` 执行上述全部建表 SQL；FTS5 触发器同步 articles 增删改（AFTER INSERT/UPDATE/DELETE 三组 trigger）。

**Step 4:** 运行测试，预期 PASS

**Step 5:** Commit: `feat(db): sqlite schema with fts5 and triggers`

### Task 2: 配置层 `app/config.py`

**Files:** Create `app/config.py`、`config.example.json`

**Step 1: 实现**（配置读写，Key 只落本地 `data/config.json`）：

```python
import json, pathlib

DEFAULT = {
    "api_base": "https://api.example.com/v1",
    "api_key": "",
    "chat_model": "",
    "embedding_model": "",
    "temperature": 0.1,
    "request_interval_sec": 5,      # 爬虫全局最小间隔
    "daily_max_per_source": 500,
}

def load_config(data_dir: pathlib.Path) -> dict:
    p = data_dir / "config.json"
    cfg = dict(DEFAULT)
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg

def save_config(data_dir: pathlib.Path, cfg: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
```

**Step 2:** 快速验证：`python -c "from app.config import load_config, save_config; ..."` roundtrip 后断言字段一致。

**Step 3:** `config.example.json` 写 DEFAULT 内容（`api_key` 留空），Commit: `feat(config): local json config with defaults`

### Task 3: FastAPI 服务骨架 `app/main.py`

**Files:** Create `app/main.py`、`app/models.py`；Create `web/index.html`（占位）

**Step 1: 实现** 最小服务：

```python
# app/main.py
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="证券法律法规知识库")
WEB = Path(__file__).resolve().parent.parent / "web"

@app.get("/api/health")
def health():
    return {"status": "ok"}

app.mount("/static", StaticFiles(directory=WEB), name="static")

@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
```

**Step 2:** 启动验证：

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

浏览器打开 `http://localhost:8000`（占位页 OK），`http://localhost:8000/api/health` 返回 `{"status":"ok"}`。

**Step 3:** Commit: `feat(api): fastapi skeleton serving static web`

---

# 阶段 M2：采集层（来源配置、限频爬虫、解析入库）

### Task 4: 官方来源清单 `crawler/sources.yaml`

**Files:** Create `crawler/sources.yaml`

**Step 1:** 结构化来源配置（用户可随时增删）：

```yaml
sources:
  - name: 国家法律法规数据库
    base_url: https://flk.npc.gov.cn
    list_url: https://flk.npc.gov.cn/index.html
    level: 法律
    method: flk            # 解析器标识，对应 parsers.py 中的实现
    interval_sec: 8
    daily_max: 300
    enabled: true
  - name: 中国证监会
    base_url: http://www.csrc.gov.cn
    list_url: http://www.csrc.gov.cn/csrc/c101969/zfsgg_list.shtml
    level: 部门规章
    method: csrc
    interval_sec: 6
    daily_max: 200
    enabled: true
  - name: 上海证券交易所
    base_url: https://www.sse.com.cn
    list_url: https://www.sse.com.cn/lawandrules/sselaws/
    level: 自律规则
    method: sse
    interval_sec: 6
    enabled: true
  - name: 深圳证券交易所
    list_url: https://www.szse.cn/lawandrules/index/
    level: 自律规则
    method: szse
    enabled: true
  - name: 北京证券交易所
    list_url: https://www.bse.cn/flfg/
    level: 自律规则
    method: bse
    enabled: true
  - name: 最高人民法院
    base_url: https://www.court.gov.cn
    level: 司法解释
    method: generic_link_list
    enabled: false          # 暂缓启用，先保核心来源
```

> **注意**：各官网 URL 可能随改版变化。执行本 Task 时先用 httpx 逐个核实 list_url 现状，失效的在 sync_log 中标记并调整。flk.npc.gov.cn 为国家法律法规数据库，是法律/行政法规权威底本。

**Step 2:** Commit: `feat(crawler): editable official sources registry`

### Task 5: 限频抓取客户端 `crawler/fetcher.py`

**Files:** Create `crawler/__init__.py`、`crawler/fetcher.py`；Test `tests/test_fetcher.py`

**Step 1: 写失败测试**（monkeypatch 模拟时间，验证限频逻辑不需真实等待）：

```python
# tests/test_fetcher.py
from crawler.fetcher import RateLimiter

def test_rate_limiter_enforces_interval(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: now["t"])
    slept = []
    def fake_sleep(s):
        slept.append(s); now["t"] += s
    monkeypatch.setattr("time.sleep", fake_sleep)
    rl = RateLimiter(default_interval=5.0)
    rl.wait("site_a")
    now["t"] += 1.0          # 距上次请求只过了 1s
    rl.wait("site_a")
    assert slept and slept[-1] >= 3.7   # 应补足等待（4s 减去抖动下限0.3s）
```

**Step 2:** 运行 `pytest tests/test_fetcher.py -v`，预期 FAIL（No module named crawler.fetcher）

**Step 3: 实现核心逻辑**：

```python
# crawler/fetcher.py 核心
class RateLimiter:
    """每 host 独立串行限频：上次请求时间 + interval + uniform(0.3, 1.5) 抖动"""
    def wait(self, host: str) -> float: ...

class Fetcher:
    """httpx 包装：限频 + 指数退避重试(3次: 10s/30s/60s) + 标准 UA
    + robots.txt 检查(启动时对每个 host 查一次并缓存) + 单次运行计数上限
    + 原始响应留档 data/raw/<source>/<date>/"""
    def get(self, url: str, source: str) -> httpx.Response: ...
    def download_pdf(self, url: str, dest: Path, source: str) -> Path: ...
```

要点：全局 `httpx.Client(timeout=30)`；`headers={"User-Agent": "LawKB-Research/1.0 (compliance study; contact: local)"}`；429/503 直接进入退避；连续 3 次失败标记该来源本次运行终止。

**Step 4:** 测试 PASS 后，Commit: `feat(crawler): rate-limited fetcher with backoff and robots check`

### Task 6: 解析器 `crawler/parsers.py`

**Files:** Create `crawler/parsers.py`；Test `tests/test_parsers.py`

**Step 1: 写失败测试**（纯本地字符串，不联网）：

```python
# tests/test_parsers.py
from crawler.parsers import split_articles

def test_split_articles_china_law_style():
    full_text = "第一章 总则\n第一条 为了规范证券发行和交易…\n第二条 在中华人民共和国境内…"
    arts = split_articles(full_text)
    assert len(arts) == 2
    assert arts[0][0] == "第一条"
    assert "证券发行" in arts[0][1]

def test_split_articles_skips_falsy():
    assert split_articles("本决定自公布之日起施行。") == []
```

**Step 2:** 预期 FAIL 后实现：

- `split_articles(full_text)`：正则 `第[一二三四五六七八九十百零\d]+条` 按行首匹配切分法规全文为 `(article_no, text)` 列表；无"条"结构的（如监管问答）整篇作为单条 article，article_no 留空。
- `parse_law_html(html, source)`：bs4 + lxml 提取标题/发文机关/日期/正文，按 sources.yaml 的 `method` 分派（`parse_csrc` / `parse_flk` / `parse_sse` / …）；PDF 用 `pdfplumber`（需 `pip install pdfplumber`）提取文本后走同一 `split_articles`。
- `content_hash`：`hashlib.sha256(full_text.encode()).hexdigest()`，title+hash 联合判重。

**Step 3:** 测试 PASS，Commit: `feat(crawler): law parsers with article splitting`

### Task 7: 入库与增量同步 `crawler/sync.py`

**Files:** Create `crawler/sync.py`、`crawler/full_import.py`

**Step 1: 实现同步 CLI**：

```bash
.venv/Scripts/python.exe -m crawler.sync --source 证监会        # 单来源增量
.venv/Scripts/python.exe -m crawler.sync                        # 全部启用来源
.venv/Scripts/python.exe -m crawler.full_import --source 证监会 --limit 20   # 首次小规模试跑
.venv/Scripts/python.exe -m crawler.full_import --source 证监会  # 首次全量（翻页到底）
```

逻辑：读 sources.yaml → 拉 list 页解析条目链接 → 逐条抓正文（走 Fetcher 限频）→ 与库中 content_hash 对比 → 新增 INSERT laws+articles / 变更 UPDATE（旧版标 `status='已修订'`）→ 写 sync_log。

**Step 2: 验证**：对单个来源（建议先深交所或国家法律法规数据库）跑 `--limit 20` 试导入；用 sqlite 查询：

```bash
.venv/Scripts/python.exe -c "import sqlite3; db=sqlite3.connect('data/knowledge.db'); print(db.execute('SELECT count(*) FROM laws').fetchone(), db.execute('SELECT count(*) FROM articles').fetchone())"
```

Expected：laws ≥ 15、articles > laws 计数（法条已切分）。

**Step 3:** 再跑一次同命令，验证 `new_count=0`（去重生效）。Commit: `feat(crawler): incremental sync and full import pipelines`

（M2 完成检查点：至少 2 个来源成功入库 ≥50 部法规，法条切分正确，重复运行第二次 new_count=0。）

---

# 阶段 M3：检索层（embedding、混合检索、关联图谱）

### Task 8: LLM 客户端 `app/llm.py`

**Files:** Create `app/llm.py`；Test `tests/test_llm.py`

**Step 1: 实现**（httpx 直调 OpenAI 兼容接口，不引入 openai 包）：

```python
# app/llm.py 核心接口
class LLMClient:
    def __init__(self, cfg: dict): ...          # api_base / api_key / chat_model
    def chat(self, messages, tools=None, stream=False) -> ...:
        """POST {api_base}/chat/completions；stream=True 时返回异步生成器"""
    def embed(self, texts: list[str]) -> list[list[float]]:
        """POST {api_base}/embeddings，batch<=64，文本截断至 ~3000 字"""

def cosine(a: list[float], b: list[float]) -> float: ...
```

**Step 2: 写测试**：mock httpx 响应（`respx` 或 monkeypatch），验证：请求体包含正确 model/messages；embed 返回解析正确；429 时抛出可读异常。不联网测试。

**Step 3:** 手动连通性验证（配置真实 Key 后）：

```bash
.venv/Scripts/python.exe -m app.llm --test
```

Expected：打印 chat 模型回复一句话 + embedding 维度（如 1024）。此步需要用户提供可用 API。

**Step 4:** Commit: `feat(llm): openai-compatible client for chat and embedding`

### Task 9: 向量化入库 `app/embed.py`

**Files:** Create `app/embed.py`

**Step 1: 实现**：

```bash
.venv/Scripts/python.exe -m app.embed --batch 64   # 扫描 embedding IS NULL 的法条
```

- 分批调 `LLMClient.embed`，写回 `articles.embedding`（JSON 数组）
- 速率控制：每批间隔 1s，失败单条重试 2 次后跳过并记录
- 幂等：中断后重跑只处理未向量化的行

**Step 2: 验证**：入库样本法规后运行，SQL 确认 `embedding IS NOT NULL` 比例 100%（重试跳过的除外）。

**Step 3:** Commit: `feat(embed): batch embedding backfill for articles`

### Task 10: 混合检索 `app/search.py`

**Files:** Create `app/search.py`；Test `tests/test_search.py`

**Step 1: 写失败测试**（内存库插入已知法条，mock embedding）：

```python
# tests/test_search.py
from app.search import hybrid_search

def test_keyword_hits_exact_article(tmp_db):
    # 插入《证券法》第十条（含"公开发行"），检索"公开发行 证券"
    results = hybrid_search(tmp_db, mock_llm, "公开发行必须符合什么条件？", top_k=5)
    assert any("公开发行" in r["text"] for r in results)
    assert all(r.get("score") >= 0 for r in results)
```

**Step 2:** 预期 FAIL 后实现 `hybrid_search(db, llm, query, top_k=10)`：

1. **关键词路**：`SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?`（query 加引号做 phrase + 分词 OR 两路查询）
2. **向量路**：`llm.embed([query])` → 遍历库内 embedding 算余弦（库 <10 万条法条时纯 Python 足够；超 5 万条时建 numpy 内存缓存，启动时加载）
3. **RRF 融合**：`score = Σ 1/(60 + rank_i)`，两路各取 top 50
4. 返回：article_id、law_id、title、article_no、text、score、来源 URL

**Step 3:** 测试 PASS 后手动验证：对入库数据问"内幕交易怎么处罚"，检查命中法条合理。

**Step 4:** Commit: `feat(search): hybrid retrieval with rrf fusion`

### Task 11: 检索 API 端点

**Files:** Modify `app/main.py`、`app/models.py`

**Step 1: 实现端点**：

```text
GET  /api/laws?level=&topic=&q=&page=      → 法规分页列表
GET  /api/laws/{id}                        → 法规详情 + 全部法条
GET  /api/laws/{id}/relations               → 关联法规/法条列表
GET  /api/search?q=&top_k=                 → 混合检索结果（法条级）
GET  /api/stats                             → 各级别数量、最近同步时间
```

**Step 2:** 启动服务，浏览器/curl 验证各端点返回 JSON 结构正确。

**Step 3:** Commit: `feat(api): law list/detail/search endpoints`

### Task 12: 关联图谱生成 `app/relations.py`

**Files:** Create `app/relations.py`

**Step 1: 实现**：

```bash
.venv/Scripts/python.exe -m app.relations --law 3       # 对指定法规抽取关系
.venv/Scripts/python.exe -m app.relations --pending 20  # 处理尚未抽取的法规
```

流程：对每部法规，将 标题 + 发文机关 + 全部条文（超长则抽样首尾 + 目录）交给 LLM，要求结构化输出：

```json
{"relations": [
  {"ref_title": "中华人民共和国公司法", "rel_type": "上位法",
   "note": "证券发行中的公司治理事项适用公司法"},
  {"ref_title": "上市公司信息披露管理办法", "rel_type": "同一事项", "note": "…"}]}
```

然后在库内按 title 模糊匹配定位目标法规/法条（匹配不到的存 note 待人工处理，不强行写入）。`rel_type` 枚举：引用 / 修订替代 / 上位法 / 同一事项 / 程序衔接。

**Step 2:** 验证：对《证券法》运行，人工抽查 5 条关系是否成立；`/api/laws/{id}/relations` 返回非空。

**Step 3:** Commit: `feat(relations): llm-extracted law relation graph`

（M3 完成检查点：搜索"内幕交易处罚"、"减持规则"、"信息披露"等问题命中相关法条；至少 3 部核心法规的关联图谱非空且人工抽查基本正确。）

---

# 阶段 M4：对话 Agent、wiki 与前端交互

### Task 13: Agent 对话循环 `app/chat.py`

**Files:** Create `app/chat.py`；Test `tests/test_chat.py`

**Step 1: 实现 Agent 循环**（核心：LLM 通过 tool calling 自主检索）：

```python
TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_laws",
        "description": "在证券法律法规知识库中检索法条。可多次调用，换不同关键词。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词，如：内幕交易 行政处罚"},
                "top_k": {"type": "integer", "default": 8}
            },
            "required": ["query"]
        }
    }
}]

async def agent_chat(db, llm, messages: list, max_rounds: int = 5):
    """循环：LLM 回复 → 若含 tool_calls 则执行 search_laws 把结果作为
    tool role 消息回填 → 继续下一轮；无 tool_calls 则返回最终回答。
    system prompt 要求：仅依据检索到的法条作答；每条结论标注
    【法规名·条号】；检索不到就明说，禁止编造；注意法规时效状态。"""
```

**Step 2: 端点** `POST /api/chat`（SSE 流式转发 token，结束后附 `citations` 数组：法条 id/法规名/条号/URL）。

**Step 3: 测试**：mock LLM 返回固定的 tool_calls 序列，验证循环正确执行检索、回填、终止条件（max_rounds 生效）。

**Step 4:** 配置真实 API 后手动验证三问：
- "内幕交易的构成要件和处罚？"（应引证券法第 50/191 条附近 + 刑法相关）
- "上市公司减持新规有什么要求？"（应引交易所规则）
- "知识库里没有的问题"（应明确说检索不到，不编造）

**Step 5:** Commit: `feat(chat): agent loop with tool-calling retrieval and citations`

### Task 14: 模型配置端点与测试连接

**Files:** Modify `app/main.py`

**Step 1: 实现端点**：

```text
GET  /api/settings     → 返回配置（api_key 打码显示为 sk-****abc）
PUT  /api/settings     → 保存配置到 data/config.json（含基本校验）
POST  /api/settings/test → 用当前配置发一次 1-token 请求，返回连通结果
```

**Step 2:** 验证：改错 Key → test 返回 401 类错误；改回正确 Key → 返回 ok。

**Step 3:** Commit: `feat(api): settings endpoints with connection test`

### Task 15: wiki 按需解读 `app/wiki.py`

**Files:** Create `app/wiki.py`

**Step 1: 实现**：

```text
POST /api/laws/{id}/wiki   → 若 wiki_pages 有缓存直接返回；否则用 LLM 生成
```

生成 prompt：输入法规元数据 + 全文（超长分段摘要合并），要求输出 markdown：立法目的与背景 / 核心制度框架 / 重点条文解读 / 与其他法规的衔接（利用已有 relations）/ 实务要点。生成完写入 `wiki_pages` 缓存，页面提供"重新生成"按钮。

**Step 2:** 验证：对一部法规生成 wiki，前端可渲染 markdown，二次请求秒回（缓存命中）。

**Step 3:** Commit: `feat(wiki): on-demand llm law summary with cache`

### Task 16: 前端单页 `web/index.html` + `app.js` + `style.css`

**Files:** Create `web/index.html`、`web/style.css`、`web/app.js`

**Step 1: 实现三大区块**（原生 JS，fetch 调后端，无外部依赖）：

- **顶栏**：站名 + 全局搜索框（回车 → 搜索结果视图）+ ⚙ 模型配置按钮（弹窗：api_base / api_key / chat_model / embedding_model / temperature + [测试连接] + [保存]）
- **左侧导航树**：按效力级别（法律/行政法规/部门规章/规范性文件/自律规则/司法解释）分组，`/api/laws?level=` 懒加载，附主题标签筛选
- **主区域三视图**：
  1. 法规列表（分页、状态徽章：现行有效绿/已修订黄/已废止灰）
  2. 法规详情：元数据 + 条文目录侧锚点 + 正文（关键词高亮）+ 关联法规卡片（点击跳转）+ [生成解读] 按钮（渲染 markdown wiki）
  3. 搜索结果：法条级命中卡片（法规名·条号 + 命中片段高亮 + 相关度分），点击进入该法规详情并定位到条
- **底部全局对话栏**：输入框 → `POST /api/chat` SSE 流式渲染；回答中 `【法规名·第X条】` 引用渲染为可点击 chip，点击跳转法条原文；多轮上下文保留（会话内存即可）

**Step 2:** 样式要求：系统字体栈、浅色为主、CSS 变量定义主色（深蓝），无外部 CDN/图标库（用 Unicode 符号）。

**Step 3:** 手动验证清单：
- [ ] 导航树展开/筛选正常
- [ ] 搜索"内幕交易"出结果且高亮
- [ ] 法规详情跳转与条锚点定位
- [ ] ⚙ 配置弹窗保存后 test 连接通过
- [ ] 对话流式输出、引用 chip 可点、多轮追问生效
- [ ] 生成 wiki 并渲染

**Step 4:** Commit: `feat(web): single-page ui with browse/search/chat/settings`

（M4 完成检查点：全部手动验证清单通过。）

---

# 运维与持续更新（M4 后）

### Task 17: 日常增量更新流程与文档

**Step 1:** 在 `AGENTS.md` 写明日常操作：

```bash
.venv/Scripts/python.exe -m crawler.sync      # 每日增量（建议 WorkMate 定时任务，每日 1 次）
.venv/Scripts/python.exe -m app.embed          # 新法条向量化
.venv/Scripts/python.exe -m app.relations --pending 10   # 新法规关系抽取
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000   # 启动服务
```

**Step 2（可选）:** 创建 WorkMate 定时任务：每日跑一次 `crawler.sync`，结果写入 sync_log，失败时在回复中提醒。频率每日 1 次完全满足反爬要求。

**Step 3:** `README.md`：安装、配置 API、各命令说明。

**Step 4:** Commit: `docs: daily operation guide`

---

# 风险与应对

| 风险 | 应对 |
|------|------|
| 官网改版导致解析失效 | 解析器按 method 分派隔离；sync_log 记录失败率；fixture 测试保护 |
| 反爬拦截（封 IP） | 限频 + 抖动 + UA 声明 + 每日增量小量；连续失败自动熔断该来源 |
| Python 3.14 个别库无 wheel | 已验证核心链路依赖全部可装；pdfplumber 若装不上，退回 pypdfium2 |
| LLM 幻觉 | system prompt 强制仅依据检索法条作答 + 引用溯源 + 温度 0.1 |
| 全量导入 token/时间成本 | embedding 与关系抽取均支持断点续跑、幂等重试 |
| flk.npc.gov.cn 需 JS 渲染 | 若静态抓不到，改用其手机版接口或降级为手动导入 + 证监会来源为主 |

# 明确不做（YAGNI）

- 不做用户系统/多用户权限（本地单人工具）
- 不做前端框架、构建工具、npm
- 不做本地嵌入模型、独立向量数据库
- 不做全量预生成 wiki（仅按需 + 缓存）
- 不做分布式爬虫、代理池（尊重官方站点）
