# 证券法律法规知识库 — 全方位诊断与优化设计

- 日期：2026-10-22
- 状态：待评审
- 基线：master@663c91e，87 个测试通过，879 部法规 / 21,084 条法条 / 5,062 条关系
- 目标：在不改变「FastAPI + 原生前端 + SQLite 单文件」总体架构的前提下，修复核心检索缺陷，补齐数据治理、合规、兼容性与工程化能力。

## 1. 诊断结论

### 1.1 核心功能缺陷（P0）

**中文自然语言问句检索大量 0 结果。** 19 个典型问句中 5 个完全无结果、2 个结果明显不准：

| 查询 | 现状 |
|---|---|
| 内幕交易的构成要件和处罚 | 0 条 |
| 内幕交易怎么处罚 | 0 条 |
| 上市公司股东减持新规有什么要求 | 0 条 |
| 短线交易禁止 | 0 条 |
| 证券法第五十条 | 0 条 |

根因：`app/search.py` 没有中文分词。FTS5 trigram 只对整串做 phrase 匹配；无空格长问句不会进入 OR 分支；LIKE 兜底同样按整串匹配。实测改为 jieba 分词 + FTS 后，"内幕交易怎么处罚"可准确命中《刑法》第 180 条、《证券法》第 191 条、《期货和衍生品法》第 126 条。

**法规名+条号查询没有结构化解析。** "证券法第五十条"被当作普通关键词，返回的是引用该条的其他文件。

### 1.2 合规与兼容性违规（P1）

1. `crawler/targeted_import.py:130` 构造 `BrowserFetcher()` 时未传 `RateLimiter` 和 `daily_max`，且浏览器路径不检查 robots.txt，违反 AGENTS.md「所有抓取一律走限频客户端」硬约束。
2. `web/style.css:421` 的 `inset: 0` 无旧值回退；flex `gap` 在 Chromium <84 失效。项目要求兼容内网 Chromium <80。

### 1.3 数据质量（P1）

- 至少 10+ 组标题重复（最多 3 份），同一法规从不同栏目重复入库，检索结果与图谱节点重复。
- 16 部法规全文 < 200 字，19 条法条正文 < 5 字。
- 879 部全部为「现行有效」；`sync` 更新时原地覆盖旧版，历史版本丢失，"已修订/已废止"筛选形同虚设。
- 591 条法条无条号（整篇入库），其中部分可能是切分失败。

### 1.4 工程化债务（P2）

- `relations` 缺 `to_id` 索引，`sync_log` 无索引；SQLite 为 `journal_mode=delete`，服务与图谱长跑进程并发写易 busy。
- 每个请求新建 SQLite 连接且从不关闭。
- 无 schema 迁移机制（`CREATE TABLE IF NOT EXISTS` 无法加列）。
- 测试缺 API 端点、中文检索回归、sync 更新流程；README 的测试数量过期。
- `build_portable.py` 硬编码本机路径、把 `.git` 打进包、直接复制在线 DB 而非一致性快照。

## 2. 目标与非目标

### 目标

1. 中文问句检索命中率达到 17/19 以上，精确条号查询 top1 正确。
2. 建立可增量维护的中文分词检索索引，检索 P95 < 300ms。
3. 数据治理：重复法规、空法规、空条文可识别、可回溯、默认不出现在列表/图谱。
4. 法规更新支持版本快照，旧版标记「已修订」并保留原文。
5. 全部抓取路径纳入限频 + robots 检查。
6. 旧浏览器 CSS 兼容回退。
7. 数据库 schema 可迁移、WAL 并发、连接正确关闭、关键索引补齐。
8. 测试覆盖检索质量、API、迁移、版本化；便携包构建可移植且数据一致。

### 非目标

- 不引入向量嵌入或本地嵌入模型（当前网关不支持 embeddings）。
- 不引入前端框架、构建工具或 CDN。
- 不做多用户鉴权、权限系统（本地单人工具）。
- 不做分布式爬虫、代理池。
- 不重写前端视觉设计；只做兼容性与必要交互修正。

## 3. 总体方案

分六个阶段实施，每阶段独立可验证、可回滚：

| 阶段 | 内容 | 主要文件 |
|---|---|---|
| M1 检索 v2 | jieba 分词索引、精确条号解析、融合排序、检索质量测试 | `app/search.py`、`app/seg_index.py`、`app/legal_terms.txt` |
| M2 数据模型与迁移 | schema v2、迁移器、版本快照、索引、WAL | `app/db.py`、`app/migrations.py`、`crawler/sync.py` |
| M3 数据治理 | 备份、去重、空数据标记、清洗 CLI | `tools/data_cleanup.py` |
| M4 性能与可靠性 | 连接生命周期、图谱缓存、列表搜索改造、错误处理 | `app/main.py`、`app/graph.py`、`app/chat.py` |
| M5 合规与兼容 | 浏览器抓取限频/robots、CSS 回退 | `crawler/targeted_import.py`、`crawler/browser_fetcher.py`、`web/style.css` |
| M6 测试、构建与文档 | API/迁移/版本化测试、便携包重构、文档更新 | `tests/`、`build_portable.py`、`README.md`、`AGENTS.md` |

## 4. 关键设计

### 4.1 检索 v2

#### 4.1.1 精确条号解析

新增 `parse_citation(query) -> (law_title, article_no) | None`，识别：

- `《证券法》第五十条`、`证券法第五十条`、`证券法第50条`
- `中华人民共和国证券法 第一百九十一条`

解析后直接查库：

1. `laws.title` 精确匹配；
2. 否则 `laws.title LIKE '%<简称>%'`，按标题长度升序取最短匹配（"证券法"优先命中《中华人民共和国证券法》，而不是《证券投资基金法》）；
3. `articles.article_no` 规范化后精确匹配（中文数字与阿拉伯数字互转）。

精确命中结果置顶，并标记 `match_type="exact_article"`；其余语义结果作为补充。

#### 4.1.2 分词索引

新增持久化 FTS5 表：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS articles_seg USING fts5(
    seg, article_no, tokenize='unicode61'
);
```

- `seg` = `jieba.cut_for_search(text)` 以空格连接。
- `rowid` 与 `articles.id` 一致。
- 查询时对 query 做同样分词，构造 `"词1" OR "词2" ...`，按 FTS5 BM25 (`rank`) 排序。
- 2 字词（内幕、交易、减持）在 unicode61 下是完整 token，可正常命中，解决 trigram 最小 3 字限制。
- 长文档由 BM25 长度归一化抑制，避免整篇标准类文档霸榜。

索引维护 `app/seg_index.py`：

- `ensure_schema(db)`：建表。
- `reindex_law(db, law_id)`：sync 更新后重建该法规全部分词行。
- `rebuild_all(db)`：CLI `python -m app.seg_index --rebuild`。
- `ensure_fresh(db)`：启动时比较 `articles` 与 `articles_seg` 行数，缺失则增量补齐；补齐在后台线程执行，不阻塞服务启动。
- 索引版本写入 `meta` 表；分词器/词典变更时提示重建。

#### 4.1.3 词典与停用词

- `app/legal_terms.txt`：jieba 用户词典，收录证券法律术语（内幕交易、短线交易、减持、注册制、退市整理期、关联交易、独立董事、融资融券、要约收购、合格投资者、债券违约、股权激励、信息披露义务等）。
- 启动时把库内法规标题（去书名号/年份后缀）动态加入 jieba 词典，提升法规名整体成词率。
- `app/stopwords.txt`：疑问词与虚词（的、了、和、与、及、怎么、什么、如何、哪些、规定、要求、应当、可以、关于、以及、进行、相关、有关等）。
- 词典加载惰性执行并缓存，首次查询约 0.5s，后续 < 5ms。

#### 4.1.4 融合排序

`hybrid_search` 新流程：

1. 精确条号解析命中 -> 权重 10.0，置顶。
2. `articles_seg` BM25 召回 top 50。
3. 原 trigram 整串 phrase 召回 top 50，作为精确短语加分（权重 1.5）。
4. 可选向量路保留（当前无 embedding，跳过）。
5. RRF 融合后，按 `命中词覆盖率` 与 `法规标题匹配` 微调。
6. 结果字段新增 `match_type`、`score`，保持原有字段兼容前端。

`_like_search` 保留为最终兜底（seg 索引缺失或单字查询），但不再作为主路径。

#### 4.1.5 列表搜索

`GET /api/laws?q=` 不再对 `full_text` 做 `LIKE '%q%'`：

- 标题：`title LIKE '%q%'`（879 行，开销可忽略）。
- 全文：`articles_seg MATCH` 命中 `law_id` 集合，与标题结果合并去重。
- 默认排除 `duplicate_of IS NOT NULL` 与 `quality_flag IN ('empty_text','duplicate')`。

### 4.2 Schema v2 与迁移

使用 `PRAGMA user_version` 管理版本，`app/migrations.py` 提供幂等迁移：

```sql
-- v1: 法规质量与版本字段
ALTER TABLE laws ADD COLUMN quality_flag TEXT;      -- NULL/empty_text/short_text/duplicate/unsplit
ALTER TABLE laws ADD COLUMN duplicate_of INTEGER;   -- 指向保留法规
ALTER TABLE laws ADD COLUMN superseded_by INTEGER;  -- 指向新版本
ALTER TABLE laws ADD COLUMN version_label TEXT;     -- 如 2025年修正

-- 索引
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id);
CREATE INDEX IF NOT EXISTS idx_laws_title ON laws(title);
CREATE INDEX IF NOT EXISTS idx_laws_status ON laws(status);
CREATE INDEX IF NOT EXISTS idx_laws_dup ON laws(duplicate_of);
CREATE INDEX IF NOT EXISTS idx_articles_law_no ON articles(law_id, article_no);
CREATE INDEX IF NOT EXISTS idx_sync_log_source ON sync_log(source_name, id);

-- 元数据
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
```

迁移规则：

- 只做加列/加表/加索引，不删列、不改列类型。
- 每一步用 `PRAGMA user_version` 判定，重复执行安全。
- 旧库（user_version=0）升级后，`quality_flag/duplicate_of` 为 NULL，行为与现状一致，随后由数据治理 CLI 回填。
- 迁移失败整体回滚，并在日志中输出可读原因。

### 4.3 数据治理

新增 `tools/data_cleanup.py`，所有操作先备份：

```bash
python tools/data_cleanup.py --backup-only
python tools/data_cleanup.py --dedupe --mark-quality
python tools/data_cleanup.py --report
```

- **备份**：`sqlite3.Connection.backup()` 写入 `data/backups/knowledge-YYYYmmdd-HHMMSS.db`；备份成功才继续。
- **去重**：
  - 规范化标题：去 `【...】` 前缀、书名号、`（YYYY年修订/修正）` 后缀、首尾空白。
  - 同规范化标题分组；用 `difflib.SequenceMatcher`（截取前 20k 字）计算正文相似度。
  - 相似度 ≥ 0.95：保留来源权威性最高、正文最长的一条，其余 `duplicate_of=keep_id`、`quality_flag='duplicate'`。
  - 来源权威性顺序（高到低）：国家法律法规数据库 > 中国人大网/中国政府网 > 最高人民法院 > 中国证监会 > 上交所/深交所/北交所 > 定向补充/其他。
  - 相似度 < 0.95 且日期不同：视为版本，按 `issue_date/effective_date` 保留最新为「现行有效」，旧版 `status='已修订'`、`superseded_by=keep_id`。
- **质量标记**：
  - `full_text` 长度 < 200 -> `quality_flag='empty_text'`。
  - `articles.text` 去空白后 < 5 字 -> 删除该条（FTS 触发器同步），并记录。
  - 无条号但正文正常的法规 -> `quality_flag='unsplit'`，仅报告，不删除。
- **报告**：输出重复组、空法规、空条文、unsplit 数量与示例，写入 `data/cleanup-report-YYYYmmdd.json`。
- 默认列表、搜索、图谱排除 `duplicate_of IS NOT NULL` 与 `quality_flag IN ('duplicate','empty_text')`；详情页仍可直接访问（带质量提示）。

### 4.4 同步版本化

`crawler/sync.py` 的 `upsert_law` 更新分支调整为：

1. 按 `source_name + title + superseded_by IS NULL + duplicate_of IS NULL` 查找当前版本。
2. 内容 hash 变化时，先把当前行复制为**旧版快照**：
   - 新 `laws` 行：相同 title/issuer/level/source，`status='已修订'`、`superseded_by=当前 id`、保留旧 `full_text/content_hash`。
   - 把当前 `articles` 复制到快照 `law_id` 下。
   - 旧版快照不复制 relations/wiki（避免重复展示）。
3. 再更新当前行内容、`updated_at`、`version_label`（从标题年份后缀提取）。
4. 删除当前行旧 relations/wiki/抽取标记，重建 articles，并调用 `seg_index.reindex_law`。
5. 入向边引用方的抽取标记失效逻辑保持不变。

边界：

- `content_hash` UNIQUE 冲突时（同一内容已存在于其他法规），跳过快照并记录警告，不让同步失败。
- 来源未提供 `status` 时默认「现行有效」。

### 4.5 性能与可靠性

- `app/db.py`：
  - 文件库开启 `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=30000`、`cache_size=-64000`。
  - `:memory:` 库跳过 WAL。
  - `init_db` 调用迁移器。
- `app/main.py`：
  - 改为 `create_app(data_dir: Path | None = None)` 工厂，默认生产目录；测试可注入临时目录。
  - 端点统一用 `Depends(get_conn)`，`yield` 后 `close()`。
  - `lifespan` 初始化 DB、迁移、词典、seg 索引后台补齐。
- `app/graph.py`：按 `laws/relations` 的 `max(updated_at)` + 行数做 60s 缓存，避免每次请求 41ms 全量计算。
- `app/chat.py`：
  - `tc["function"]`、`tc["id"]` 做防御性取值；未知工具返回可读错误。
  - 单次请求的 `messages` 限制为最近 40 条、单条 8k 字，防止超长输入。
- 错误处理：`_fts_search` 的裸 `except Exception` 改为记录 `sqlite3.OperationalError` 并降级；`relations.extract_relations` 的 JSON 提取改为「首个 `{` 到最后一个 `}` + 失败重试一次非贪婪匹配」。

### 4.6 合规与兼容

- `crawler/browser_fetcher.py`：
  - 接受 `rate_limiter`、`daily_max`、`robots_checker`。
  - `render()` 前调用 robots 检查（注入 `Fetcher._check_robots` 或独立实现），拒绝时抛 `PermissionError`。
- `crawler/targeted_import.py`：
  - 浏览器与 httpx 共用同一个 `RateLimiter` 实例。
  - 传入 `cfg["daily_max_per_source"]`。
  - 记录浏览器请求计数。
- `web/style.css`：
  - `inset: 0` 前补 `top/right/bottom/left: 0`。
  - 为 flex `gap` 增加 `@supports not (gap: 1px)` 的 margin 回退，覆盖 topbar、law-card meta、modal-actions、chat-input-row、relation-list 等关键布局。
- 前端 JS 保持 ES2017 及以下；新增代码不得出现 `??`/`?.`/`trimEnd`/`flat`/`matchAll`/`replaceAll`。

### 4.7 测试与构建

#### 测试

- `tests/conftest.py`：`tmp_db`、`app_client`（`create_app(tmp_path)`）。
- `tests/test_search_quality.py`：
  - 精确条号：`证券法第五十条` top1 为《证券法》第五十条；
  - 自然语言：`内幕交易怎么处罚`、`上市公司股东减持新规`、`短线交易禁止` top5 含预期法规；
  - 短词/单字兜底：`税` 有结果；
  - 空查询、超长查询、特殊字符不崩。
- `tests/test_api.py`：health/stats/laws/detail/search/settings 的 200/404/422 行为；临时 DB 不污染生产库。
- `tests/test_migrations.py`：v0 -> v1 幂等；重复执行不报错；旧数据可读。
- `tests/test_sync_versions.py`：mock fetcher，内容变化时生成旧版快照、状态为「已修订」、旧条文保留、当前条文更新、关联标记失效。
- `tests/test_seg_index.py`：增量索引、重建、缺失补齐。
- 目标：测试数量 ≥ 120，全部通过。

#### 构建

`build_portable.py` 重构：

- `SRC = Path(__file__).resolve().parent`，不再硬编码 `D:\...`。
- Python home：优先 `runtime/python.exe`，否则 `sys.base_prefix`；不存在则明确报错。
- `COPY_DIRS` 移除 `.git`（新增 `--with-git` 才复制）。
- 数据库用 `sqlite3.Connection.backup()` 生成一致性快照，复制前检查 WAL checkpoint。
- 排除 `data/raw`、`data/backups`、`*.log`。
- zip 名带日期：`法律法规知识库查询-YYYYMMDD.zip`。
- 验证阶段增加 `import jieba`、`articles_seg` 行数检查。
- `requirements.txt` / `requirements-full.txt` 增加 `jieba==0.42.1`。

#### 文档

- `README.md`：更新测试数量、检索能力（jieba）、数据治理命令、版本化说明。
- `AGENTS.md`：补充 seg 索引维护、数据清洗、备份位置、构建新参数。
- `docs/技术亮点.md`：更新检索架构描述。

## 5. 验收标准

| 项 | 标准 |
|---|---|
| 检索命中 | 19 个典型问句中 ≥ 17 个有相关结果；5 个原 0 结果查询全部命中 |
| 精确条号 | `证券法第五十条`、`《证券法》第一百九十一条` top1 为对应条文 |
| 检索延迟 | 本地单查询 P95 < 300ms（不含首次词典加载） |
| 数据治理 | 默认列表/图谱无重复法规；空法规/空条文有质量标记且可回溯 |
| 版本化 | 内容变更生成旧版快照，旧版状态「已修订」，旧条文可查 |
| 合规 | 定向导入浏览器路径有限频与 robots 检查（单测验证） |
| 兼容 | CSS `inset` 有回退；ES2017 扫描无违规 |
| 数据库 | WAL 生效；关键索引存在；连接请求结束即关闭 |
| 测试 | ≥ 120 个测试全部通过 |
| 构建 | 便携包在非作者路径下可构建；DB 快照一致；jieba 可导入 |

## 6. 风险与回滚

| 风险 | 应对 |
|---|---|
| jieba 索引体积增大（预计 DB +40~60MB） | 便携包体积可接受；提供 `--rebuild`/删除重建；构建时校验 |
| 去重误判不同版本 | 相似度阈值 0.95 + 日期判断；只标记不物理删除；备份可恢复 |
| 版本快照导致数据量增长 | 只对内容变化生成快照；旧版默认不参与列表/图谱 |
| 迁移失败 | 迁移前自动备份；迁移事务化；失败不提升 user_version |
| 检索改造成果不稳定 | 保留原 FTS/LIKE 兜底；固定查询集回归测试 |
| 便携包构建路径差异 | 去掉硬编码；构建前自检；失败时输出明确路径 |

回滚：

- 代码：`git revert` 对应提交。
- 数据库：从 `data/backups/` 恢复（`--backup-only` 生成的快照）。
- 分词索引：`DROP TABLE articles_seg` 后重建；检索自动降级到原 FTS/LIKE。

## 7. 实施顺序与检查点

1. **M2 数据模型/迁移**（先打地基，其他阶段依赖新列/索引）
2. **M1 检索 v2**（核心价值，依赖 seg 表）
3. **M3 数据治理**（依赖新列；先备份再生产数据上执行）
4. **M4 性能与可靠性**（依赖迁移器）
5. **M5 合规与兼容**（独立）
6. **M6 测试、构建与文档**（收尾）

每个阶段完成后运行全量测试；M1、M3 完成后额外运行检索质量基准；M6 完成后执行一次便携包构建验证。
