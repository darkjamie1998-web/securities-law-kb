# 完全离线可跳转前端快照 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将根目录静态快照改造成单文件、完全离线、所有主要模块和证据链路均可跳转的路演工作台。

**Architecture:** 复用完整 `web/index.html`、`style.css`、`app.js` 和 `graph.js`，在原脚本前注入离线 `fetch` 适配器。构建脚本从本机 SQLite 提取统一精选数据，Playwright 在断网环境验证全部路由和交互；不修改 FastAPI 业务源码。

**Tech Stack:** Python 3.14、SQLite、原生 JavaScript（ES2017）、Playwright、单文件 HTML/CSS/JS。

**交付文件：** `证券法律法规知识库-前端快照.html`。可重复生成工具放在 `tools/roadshow/`，不自动创建 Git 提交。

---

### Task 1: 建立会先失败的离线验收脚本

**Files:**
- Create: `tools/roadshow/verify_snapshot.py`
- Test target: `证券法律法规知识库-前端快照.html`

- [ ] **Step 1: 写路由与离线资源验收**

使用本机 Chrome 和 Playwright，离线打开根目录快照。首版覆盖以下断言：

```python
ROUTES = {
    "#/home": "证券法律法规研究",
    "#/laws": "全部法规",
    "#/search?q=减持计划": "减持计划",
    "#/law/216": "上市公司信息披露管理办法",
    "#/graph": "知识图谱",
}
EXTERNAL = ("http://", "https://")
```

脚本使用 `browser.new_context(offline=True)`，逐个执行 `page.evaluate("location.hash = ...")`，等待对应文本可见；记录 `pageerror`、控制台 error 和所有 HTTP/HTTPS request。当前快照缺少原路由脚本，因此该测试必须先失败。

- [ ] **Step 2: 运行并确认失败原因正确**

Run: `.venv/Scripts/python.exe -m tools.roadshow.verify_snapshot`

Expected: FAIL，首页/法规列表/图谱不能进入预期模块，而不是脚本找不到 Chrome。Chrome 路径使用 `C:/Program Files/Google/Chrome/Application/chrome.exe`。

- [ ] **Step 3: 增加交互验收清单**

在同一脚本加入以下 Playwright 操作：品牌回首页、侧栏进入列表和图谱、搜索“减持计划”、点击首个法条、阅读器条号定位、篇内搜索、打开关联、打开聊天、打开设置、浏览器前进后退。每一步等待具体目标文本，不用固定休眠代替状态断言。

### Task 2: 提取统一精选法规 fixture

**Files:**
- Create: `tools/roadshow/extract_fixture.py`
- Create: `tools/roadshow/test_fixture.py`
- Generate: `tools/roadshow/fixture.json`

- [ ] **Step 1: 写 fixture 一致性测试**

测试固定要求：

```python
LAW_IDS = {20, 215, 216, 293, 296, 302, 406, 820}
assert set(data["laws"]) == LAW_IDS
assert len(data["articles"][20]) >= 50
assert len(data["laws"]) >= 8
assert any(e["s"] == 293 and e["t"] == 216 for e in data["graph"]["edges"])
for chat in data["chats"].values():
    for citation in chat["citations"]:
        assert citation["law_id"] in LAW_IDS
        assert str(citation["article_id"]) in data["article_by_id"]
```

- [ ] **Step 2: 实现确定性提取**

使用只读 SQLite URI `file:data/knowledge.db?mode=ro`。提取：

```python
LAW_IDS = (20, 215, 216, 293, 296, 302, 406, 820)
# 20：按 id 取前 60 条，保证阅读器两页。
# 215、296：提取 text 含“减持”的真实条文，保留减持问答所需第九条等。
# 216、293、302、406：提取全部条文。
# 820：保留 unsplit 全文条文。
```

`laws` 保存真实元数据；`article_by_id` 以字符串 ID 映射条文；`relations` 通过 `articles` 双 JOIN 获取真实关系；`graph` 至少包含 `(215,20)`、`(216,20)`、`(293,20)`、`(293,216)`、`(302,216)`、`(406,20)`、`(820,20)` 这些已核实关系。Stats 在生成时从本机 `/api/stats` 获取并标记为 `full_library_snapshot`，避免把精选集规模伪装成全库规模。

- [ ] **Step 3: 加入两组预置问答**

减持问答引用真实 `article_id=9349`、`law_id=215`、第九条；信息披露问答引用 `article_id=13717`、`law_id=293`、第四条及 `article_id=13721`、第八条。回答正文采用 `【法规名·第X条】` 格式，并包含“精选离线演示，具体事项需核对官方原文”的说明。

- [ ] **Step 4: 运行提取和测试**

Run: `.venv/Scripts/python.exe -m tools.roadshow.extract_fixture`
Run: `.venv/Scripts/python.exe -m pytest tools/roadshow/test_fixture.py -v`

Expected: 8 部法规、至少 50 条证券法正文、引用 ID 一致、图谱边完整，测试 PASS。

### Task 3: 实现离线 API 适配器

**Files:**
- Create: `tools/roadshow/offline_adapter.js`
- Test: `tools/roadshow/verify_snapshot.py`

- [ ] **Step 1: 实现 Response 与路由入口**

适配器在原 `app.js` 前执行，禁止透传网络：

```javascript
function jsonResponse(data, status) {
  return new Response(JSON.stringify(data), {
    status: status || 200,
    headers: {"Content-Type": "application/json; charset=utf-8"}
  });
}
function ndjsonResponse(events) {
  var text = events.map(function (e) { return JSON.stringify(e) + "\n"; }).join("");
  return new Response(text, {status: 200, headers: {"Content-Type": "application/x-ndjson"}});
}
window.fetch = function (input, init) {
  var url = new URL(typeof input === "string" ? input : input.url, "http://offline.local");
  return offlineRoute(url, init || {});
};
```

`offlineRoute` 精确覆盖 Stats、laws、reader、articles、relations、search、graph、chat、wiki、settings。未覆盖路径返回 `jsonResponse({error: "离线演示未提供该接口"}, 404)`，并记录到 `window.offlineErrors`。

- [ ] **Step 2: 实现列表、筛选与分页**

统一 helper 接收 `items/page/pageSize`，返回原前端要求的 `total/page/page_size/items`；搜索响应返回 `query/count/results/total/page/page_size/has_more/mode`。法规标题和条文正文使用大小写无关的包含匹配，级别、状态、来源、主题按查询参数过滤，空关键词返回真实空结果或列表全集。`mode` 固定为 `offline-demo`，界面明确显示“精选离线检索”。

- [ ] **Step 3: 实现阅读器与证据定位**

Reader helper 根据 `article_no` 找到真实 `article_id`，计算目标页，返回 `law/articles/article_index/article_total/article_page/article_page_size/target_article_id/wiki`。篇内搜索共用同一正文数组。Relations 同时生成 `outgoing/incoming`，字段与原 `app.js` 一致，确保点击关联进入另一个真实 fixture 法规。

- [ ] **Step 4: 实现图谱、设置和离线说明**

Graph 使用统一节点边，节点包含 `id/title/level/status/board/out_deg/in_deg`。设置读取返回 `api_base: "离线演示"`、`api_key_masked: ""`、`has_key: false`、空模型名；保存和测试连接返回 `ok: false` 及“完全离线演示不保存配置、不测试连接”。Wiki 返回预生成 Markdown，强制重新生成返回明确离线错误。

- [ ] **Step 5: 运行路由测试**

Run: `.venv/Scripts/python.exe -m tools.roadshow.verify_snapshot`

Expected: 适配器单元场景通过；完整页面在此任务仍可能因尚未组装而失败，失败必须发生在构建产物阶段而不是 fixture 数据不一致。

### Task 4: 组装完全离线单文件工作台

**Files:**
- Create: `tools/roadshow/build_snapshot.py`
- Generate: `证券法律法规知识库-前端快照.html`

- [ ] **Step 1: 内联完整资源并设置离线顺序**

构建器读取 `web/index.html`，删除两个外部 script 标签和 stylesheet link，按以下顺序写入 `<body>`：

```text
fixture JSON → offline_adapter.js → 原 app.js → 原 graph.js → offline_ui.js
```

CSS 全部放入 `<style>`。把 `const LAW_PAGE_SIZE = 20` 改为 `const LAW_PAGE_SIZE = 6`，仅此一处用于精选目录翻页；其余业务前端逻辑保持原样。HTML 根元素增加 `data-workmate-generated="true"`。

- [ ] **Step 2: 增加离线 UI 标识与降级**

新增 `tools/roadshow/offline_ui.js`：在顶栏下方插入“完全离线演示｜精选 8 部法规｜对话为预置问答”；设置输入框只读，外部官方链接点击时显示“需要联网打开官方原文”，不干扰 hash 内部链接。页尾增加可见 `<footer class="workmate-attribution">由 WORKMATE 生成</footer>`。窄屏目录沿用原折叠按钮，不采用上一版永久隐藏侧栏的规则。

- [ ] **Step 3: 构建并执行静态检查**

Run: `.venv/Scripts/python.exe -m tools.roadshow.build_snapshot`

断言产物包含 8 部法规 fixture、两个预置问答、完整弹层 DOM、唯一 WorkMate 标识，不包含 `/static/`、`fetch("http`、真实 API 地址或 `api_key` 值。Expected: 生成单个 HTML，无其他运行时文件依赖。

- [ ] **Step 4: 运行主要路由验收**

Run: `.venv/Scripts/python.exe -m tools.roadshow.verify_snapshot`

Expected: 首页、列表、搜索、详情、图谱均可直接访问；无 HTTP/HTTPS 资源请求，无未处理脚本错误。

### Task 5: 完成对话、证据和全部页内交互

**Files:**
- Modify: `tools/roadshow/offline_adapter.js`
- Modify: `tools/roadshow/fixture.json`（通过提取脚本重生）
- Test: `tools/roadshow/verify_snapshot.py`

- [ ] **Step 1: 实现预置问答 NDJSON**

减持和信息披露问题分别返回 `stage: thinking`、`stage: searching`、`stage: citing` 和最终 `result`。结果包含 `content/searches/citations`；引用字段与 fixture 的 `law_id/article_id/article_no/title/source_url/ref_title/ref_no` 完全一致。其他问题返回 `type: "error"` 和“离线演示仅支持预置问题”，并给出两个问题示例。

- [ ] **Step 2: 验证证据链**

Playwright 打开聊天，发送减持问题，等待引用出现；点击引用，断言证据抽屉显示第九条真实原文；点击“在阅读器中打开”，断言 hash 为 `#/law/215` 且目标条号第九条。信息披露问答按同样方式验证第四条和第八条。

- [ ] **Step 3: 验证阅读器与图谱交互**

验证目录两页、搜索法规/法条范围切换、筛选、独立分页、证券法正文两页、条号目录定位、篇内搜索、关联 tab、Wiki tab、图谱板块切换和节点进入法规。空关键词、无结果关键词和 `#/law/999999` 均显示正式空状态。

- [ ] **Step 4: 验证设置与本地面板**

设置弹窗可以打开和关闭，所有配置字段只读；保存与测试连接显示离线不可用。对话面板可最小化、拖动和缩放；清空与重试不触发网络。外部官方链接只显示联网提示，不改变当前离线路由。

### Task 6: 最终回归与交付

**Files:**
- Test: `tools/roadshow/verify_snapshot.py`
- Output: `证券法律法规知识库-前端快照.html`

- [ ] **Step 1: 全量运行验收**

Run: `.venv/Scripts/python.exe -m pytest tools/roadshow/test_fixture.py -v`
Run: `.venv/Scripts/python.exe -m tools.roadshow.verify_snapshot`

Expected: fixture 测试全 PASS；所有路由、导航、分页、图谱、聊天、证据、设置、前进后退测试全 PASS；`window.offlineErrors` 为空。

- [ ] **Step 2: 检查桌面、手机和控制台**

Playwright 分别设置 `1440×900`、`375×812`，检查 `document.documentElement.scrollWidth <= window.innerWidth`，测试移动端目录开关、聊天和证据抽屉。保存桌面/手机截图到会话目录并人工查看，控制台 error 和 pageerror 必须为 0。

- [ ] **Step 3: 校验单文件与交付说明**

使用 HTML Writing 校验器检查临时单文件目录，确认 `ok: true`、`errors: []`。内联 CSS 导致的 `missing-responsive-rule` warning 由实际 4 个以上 media query 和双视口测试人工复核。最终回复提供根目录文件链接、支持的模块清单、精选数据边界和验收结果，不声称具备实时 AI 或全库检索。
