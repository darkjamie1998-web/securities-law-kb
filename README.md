# 证券法律法规知识库查询

通过公开官方来源采集和沉淀证券业务相关法律法规，建设 AI Agent 知识库：
**RAG 法条检索 + LLM 关联图谱 wiki + 带引用溯源的大模型对话研究**。

完整设计与实施计划见 `docs/plans/2026-09-19-securities-law-kb-app.md`。

## 快速开始

```bash
# 1. 虚拟环境（已建好；如需重建）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install pyyaml pdfplumber pytest

# 2. 启动服务，浏览器打开 http://localhost:8000
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000

# 3. 首次同步法规入库（限频爬虫，耐心等待）
.venv/Scripts/python.exe -m crawler.sync --source 深圳证券交易所 --limit 20   # 小规模试跑
.venv/Scripts/python.exe -m crawler.sync                                      # 全部启用来源

# 4. 配置大模型：页面右上 ⚙ 配置（OpenAI 兼容 API 地址/Key/模型名）
#    或复制 config.example.json 为 data/config.json 手动编辑

# 5. 检索增强（配置好模型后）
.venv/Scripts/python.exe -m app.embed                          # 法条向量化
.venv/Scripts/python.exe -m app.relations --pending 10         # 关联图谱生成
```

## 日常运维

```bash
.venv/Scripts/python.exe -m crawler.sync                        # 每日增量（建议每日 1 次）
.venv/Scripts/python.exe -m crawler.targeted_import --dry-run   # 定向补充法律（清单 crawler/targets.yaml）
.venv/Scripts/python.exe -m app.embed                           # 新法条向量化
relations.bat                                                   # 图谱全量生成（推荐：独立窗口，日志 data/relations_forever.log）
.venv/Scripts/python.exe -m app.relations --pending 10          # 小批量关系抽取
.venv/Scripts/python.exe -m pytest tests/ -v                    # 运行测试（59 个）
```

**定向补充**（`crawler/targeted_import.py`）：按 `crawler/targets.yaml` 的官方刊载页 URL 清单抓取入库（标题锚定解析 + 限频合规），适合补充交易所栏目未覆盖的通用商法经济法。找到新的全文页 URL 后加入清单重跑即可，`--dry-run` 先验证解析效果。

`run_relations_forever.py`（relations.bat 调用它）内置可靠性机制：**实例文件锁**（同机防双实例重复烧 LLM）、**看门狗**（连续 45 分钟无产出自动退出，可配合外部重启）、**完成标记表**（天然无关系的法规抽取一次即完成，不再无限重试；失败的法规最多自动重试 3 次）。中断后重跑自动续。

## 功能地图

| 功能 | 入口 |
|------|------|
| 法规浏览（效力级别/状态筛选） | 左侧导航树 |
| 法条搜索（关键词 + 向量混合，RRF 融合；≥3 字走 FTS5 trigram，短词自动 LIKE 兜底） | 顶部搜索框 |
| 法规详情 + 条文 + 双向关联（分组/类型筛选） | 点击法规卡片 |
| 知识图谱（全景 + 16 个业务板块子图，径向分层 Canvas） | 左侧"探索 → 🕸 知识图谱"，顶部板块 chips 切换 |
| Wiki 解读（LLM 按需生成，缓存复用） | 详情页"生成/刷新解读" |
| 对话研究（Agent 多轮检索 + 引用溯源） | 右下角 💬 悬浮对话（可折叠/拖动） |
| 大模型配置（OpenAI 兼容） | 右上 ⚙ 配置 |

图谱数据完整性可审计：`python -m app.audit`（对称冗余/孤立节点/幽灵标记，
`--fix` 可修复历史丢边痕迹）。

## 反爬与合规

- 所有抓取走 `crawler/fetcher.py`：每站串行、默认 ≥5s 间隔 + 随机抖动、指数退避重试、robots.txt 检查、单来源单日上限、连续失败熔断
- 增量同步每日最多跑 1 次即可满足更新需求
- 仅采集公开发布的法律法规文本；来源清单在 `crawler/sources.yaml`，可自行增删

## 目录结构

```
app/       服务层（FastAPI、检索、对话、wiki、配置）
crawler/   采集层（来源配置、限频爬虫、解析、同步）
web/       前端单页（原生 HTML/CSS/JS，无外部依赖）
data/      运行时数据（knowledge.db、config.json、原始留档）— 不入 git
tests/     50 个单元测试
```

## 已知限制

- 深交所已适配（PDF 正文）；证监会/上交所/北交所/国家法律法规数据库的专属解析器待真实页面适配（通用解析兜底）
- 对话非流式输出（Agent 多轮检索后一次性返回，含检索轨迹和引用）
- 当前网关（`x-llm-channel: workmate`）**不支持 embeddings 接口**：向量检索路自动跳过，检索为纯关键词（trigram FTS，中文效果良好）；如后续网关支持，在 `data/config.json` 填 `embedding_model` 并跑 `python -m app.embed` 即可启用
- 网关内容安全过滤会拦截部分法规原文的 LLM 处理（如反洗钱法、网络安全法条文触发 SensitiveContentDetected）：relations 抽取对这类法规自动跳过，wiki 生成会返回失败提示，属网关限制而非知识库缺陷
- 关联图谱依赖库内法规覆盖度：目标法规不在库内时关系记录为 unmatched（不强行写入），全量入库后自然改善
