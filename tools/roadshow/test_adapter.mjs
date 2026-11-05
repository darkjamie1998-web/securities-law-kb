/* 离线适配器黑盒行为测试：Node 24+，无第三方依赖、无网络。
 * 运行：node tools/roadshow/test_adapter.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(fs.readFileSync(path.join(here, "fixture.json"), "utf8"));
const adapterSource = fs.readFileSync(path.join(here, "offline_adapter.js"), "utf8");
const context = { URL, URLSearchParams, TextEncoder, console, OFFLINE_FIXTURE: fixture };
context.window = context;
vm.createContext(context);
vm.runInContext(adapterSource, context, { filename: "offline_adapter.js" });

async function request(pathname, init) {
  const response = await context.fetch(pathname, init);
  return { response, data: await response.json() };
}

function postBody(question, lawId = null) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: [{ role: "user", content: question }], law_id: lawId }),
  };
}

async function ndjson(pathname, init) {
  const response = await context.fetch(pathname, init);
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type"), /application\/x-ndjson/);
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let text = "", done = false;
  while (!done) {
    const chunk = await reader.read();
    done = chunk.done;
    if (chunk.value) text += decoder.decode(chunk.value, { stream: true });
  }
  text += decoder.decode();
  return text.trim().split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
}

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

test("stats 返回 fixture 的全库统计快照", async () => {
  const { response, data } = await request("/api/stats");
  assert.equal(response.status, 200);
  assert.equal(data.laws, fixture.stats.laws);
  assert.equal(data.scope, "full_library_snapshot");
});

test("法规列表共 8 部、分页且支持筛选", async () => {
  const all = await request("/api/laws?page=1&page_size=3");
  assert.equal(all.data.total, 8);
  assert.equal(all.data.items.length, 3);
  assert.equal(all.data.page_size, 3);
  const second = await request("/api/laws?page=3&page_size=3");
  assert.deepEqual(Array.from(second.data.items, law => law.id), [215, 20]);
  const filtered = await request("/api/laws?level=" + encodeURIComponent("法律") +
    "&status=" + encodeURIComponent("现行有效") + "&source=" + encodeURIComponent("深交所-法律"));
  assert.equal(filtered.data.total, 1);
  assert.equal(filtered.data.items[0].id, 20);
  const keyword = await request("/api/laws?q=" + encodeURIComponent("信息披露 暂缓"));
  assert.ok(keyword.data.total >= 1);
  assert.ok(keyword.data.items.some(law => law.id === 293));
  const updated = await request("/api/laws?sort=updated&page_size=100");
  const dates = updated.data.items.map(law => law.updated_at || "");
  assert.deepEqual(dates, dates.slice().sort().reverse());
});

test("搜索减持包含真实 article_id=9349 且字段完整", async () => {
  const { data } = await request("/api/search?q=" + encodeURIComponent("减持") +
    "&page=1&page_size=100");
  assert.equal(data.mode, "offline-demo");
  assert.ok(data.total > 0);
  assert.equal(data.count, data.results.length);
  const hit = data.results.find(result => result.id === 9349);
  assert.ok(hit, "减持搜索必须包含真实条文 9349");
  assert.equal(hit.law_id, 215);
  for (const key of ["law_id", "title", "article_no", "status", "match_reasons",
    "score", "snippet", "text", "highlight_terms"]) {
    assert.ok(Object.prototype.hasOwnProperty.call(hit, key), `缺少字段 ${key}`);
  }
  assert.deepEqual(Array.from(hit.match_reasons), ["keyword"]);
  assert.ok(hit.text.includes("减持计划"));
  const none = await request("/api/search?q=" + encodeURIComponent("绝不存在的关键词"));
  assert.equal(none.data.total, 0);
  assert.deepEqual(Array.from(none.data.results), []);
  const scoped = await request("/api/search?q=" + encodeURIComponent("减持") +
    "&level=" + encodeURIComponent("法律") + "&top_k=5");
  assert.ok(scoped.data.results.every(result => fixture.laws[String(result.law_id)].level === "法律"));
  assert.equal(scoped.data.page_size, 5);
});

test("证券法 reader 每页 50 条，至少两页", async () => {
  const first = await request("/api/laws/20?mode=reader&page=1&page_size=50");
  assert.equal(first.data.law.id, 20);
  assert.equal(first.data.article_total, 60);
  assert.equal(first.data.articles.length, 50);
  assert.equal(first.data.article_index.length, 60);
  const second = await request("/api/laws/20?mode=reader&page=2&page_size=50");
  assert.equal(second.data.articles.length, 10);
  assert.equal(second.data.article_page, 2);
  assert.equal(second.data.target_article_id, null);
});

test("reader article_no 定位目标页，非法条号不猜测", async () => {
  const located = await request("/api/laws/20?mode=reader&page=1&page_size=50&article_no=" +
    encodeURIComponent("第五十一条"));
  assert.equal(located.data.article_page, 2);
  assert.equal(located.data.target_article_id, 3064);
  assert.ok(located.data.articles.some(article => article.id === 3064));
  const arabic = await request("/api/laws/20?mode=reader&page_size=50&article_no=" +
    encodeURIComponent("第51条"));
  assert.equal(arabic.data.target_article_id, 3064);
  const invalid = await request("/api/laws/20?mode=reader&page_size=50&article_no=" +
    encodeURIComponent("第999条"));
  assert.equal(invalid.data.target_article_id, null);
  assert.equal(invalid.data.article_page, 1);
});

test("reader 216 返回真实正文", async () => {
  const { data } = await request("/api/laws/216?mode=reader&page_size=100");
  assert.equal(data.article_total, 67);
  assert.equal(data.articles[0].id, 9469);
  assert.equal(data.articles[0].text, fixture.article_by_id["9469"].text);
  assert.ok(data.articles[0].text.includes("信息披露"));
});

test("篇内搜索返回真实正文数组和空结果", async () => {
  const { data } = await request("/api/laws/293/articles?q=" +
    encodeURIComponent("商业秘密") + "&page=1&page_size=100");
  assert.ok(data.article_total > 0);
  assert.equal(data.articles.length, data.article_total);
  assert.ok(data.articles.every(article => article.text.includes("商业秘密")));
  const none = await request("/api/laws/293/articles?q=" + encodeURIComponent("减持计划"));
  assert.equal(none.data.article_total, 0);
  assert.deepEqual(Array.from(none.data.articles), []);
});

test("relations 293 出向及 216 对应入向字段一致", async () => {
  const outgoing = await request("/api/laws/293/relations");
  assert.equal(outgoing.data.outgoing.length, 2);
  assert.equal(outgoing.data.incoming.length, 0);
  assert.ok(outgoing.data.outgoing.every(item => item.from_law_id === 293));
  const incoming = await request("/api/laws/216/relations");
  const reverse = incoming.data.incoming.find(item => item.from_law_id === 293);
  assert.ok(reverse);
  assert.equal(reverse.from_id, 13714);
  assert.equal(reverse.to_id, 9469);
  assert.equal(reverse.from_article_no, "第一条");
  assert.equal(reverse.to_law_id, 216);
});

test("关系1288在未选入from条文时仍保留215出向和20入向", async () => {
  const outgoingResponse = await request("/api/laws/215/relations");
  const outgoing = outgoingResponse.data.outgoing.find(item => item.from_id === 8271);
  assert.ok(outgoing, "215 出向必须包含关系1288");
  assert.equal(outgoing.from_law_id, 215);
  assert.equal(outgoing.to_law_id, 20);
  assert.equal(outgoing.from_article_no, "第十条");
  assert.equal(outgoing.to_article_no, "第一条");
  assert.equal(outgoing.from_law_title, fixture.laws["215"].title);
  assert.equal(outgoing.to_law_title, fixture.laws["20"].title);

  const incomingResponse = await request("/api/laws/20/relations");
  const incoming = incomingResponse.data.incoming.find(item => item.from_id === 8271);
  assert.ok(incoming, "20 入向必须包含关系1288");
  assert.deepEqual(JSON.parse(JSON.stringify(incoming)), JSON.parse(JSON.stringify(outgoing)));
  assert.ok(!JSON.stringify(incoming).includes("undefined"));
});

test("graph 直接提供 8 个真实节点", async () => {
  const { data } = await request("/api/graph");
  assert.equal(data.nodes.length, 8);
  assert.ok(data.edges.length > 0);
  assert.deepEqual(Array.from(data.nodes, node => node.id).sort((a, b) => a - b),
    [20, 215, 216, 293, 296, 302, 406, 820]);
});

test("settings 不暴露密钥，保存和测试均明确失败", async () => {
  const current = await request("/api/settings");
  assert.deepEqual(JSON.parse(JSON.stringify(current.data)), {
    api_base: "离线演示", api_key_masked: "", has_key: false,
    chat_model: "", embedding_model: "", temperature: 0.1,
  });
  const saved = await request("/api/settings", { method: "PUT", body: "{}" });
  assert.equal(saved.response.status, 200);
  assert.equal(saved.data.ok, false);
  assert.match(saved.data.error, /离线.*不保存/);
  const tested = await request("/api/settings/test", { method: "POST", body: "{}" });
  assert.equal(tested.response.status, 200);
  assert.equal(tested.data.ok, false);
  assert.match(tested.data.error, /离线.*不连接/);
});

test("wiki 使用预生成内容且 force 不伪造生成", async () => {
  const { data } = await request("/api/laws/293/wiki?force=false", { method: "POST" });
  assert.equal(data.markdown, fixture.wikis["293"].markdown);
  const forced = await request("/api/laws/293/wiki?force=true", { method: "POST" });
  assert.equal(forced.data.error, "完全离线演示不重新生成解读");
});

test("chat 仅对规范化完整预置问题返回真实内容", async () => {
  const reductionQuestion = fixture.chats.reduction.question;
  const reduction = await request("/api/chat", postBody("  " + reductionQuestion + "\n"));
  assert.equal(reduction.response.status, 200);
  assert.ok(reduction.data.content.includes("十五个交易日"));
  assert.equal(reduction.data.citations[0].article_id, 9349);
  assert.ok(reduction.data.searches.length > 0);
  const disclosure = await request("/api/chat", postBody(fixture.chats.disclosure.question));
  assert.equal(disclosure.response.status, 200);
  assert.ok(disclosure.data.content.includes("商业秘密"));
  assert.deepEqual(Array.from(disclosure.data.citations, citation => citation.article_id), [13717, 13721]);
  const keywordOnly = await request("/api/chat", postBody("减持个税如何计算？"));
  assert.equal(keywordOnly.response.status, 400);
  assert.equal(keywordOnly.response.ok, false);
  assert.match(keywordOnly.data.error, /离线演示仅支持预置问题/);
  assert.equal(keywordOnly.data.detail, keywordOnly.data.error);
  assert.ok(!keywordOnly.data.content);
});

test("chat law_id 仅允许引用全部属于当前法规的预置问答", async () => {
  const allowed = await request("/api/chat", postBody(fixture.chats.disclosure.question, 293));
  assert.equal(allowed.response.status, 200);
  assert.ok(allowed.data.citations.every(citation => citation.law_id === 293));
  const rejected = await request("/api/chat", postBody(fixture.chats.reduction.question, 293));
  assert.equal(rejected.response.status, 400);
  assert.match(rejected.data.error, /当前法规未预置该问题/);
  assert.equal(rejected.data.detail, rejected.data.error);
  assert.ok(!rejected.data.content);
  const scopedStream = await ndjson("/api/chat/stream", postBody(fixture.chats.reduction.question, 293));
  assert.equal(scopedStream.length, 1);
  assert.equal(scopedStream[0].type, "error");
  assert.match(scopedStream[0].error, /当前法规未预置该问题/);
});

test("chat stream 返回阶段及 result，未预置问题返回 NDJSON error", async () => {
  const events = await ndjson("/api/chat/stream", postBody(fixture.chats.reduction.question));
  assert.deepEqual(events.slice(0, 3).map(event => event.stage), ["thinking", "searching", "citing"]);
  assert.equal(events[3].type, "result");
  assert.equal(events[3].data.citations[0].article_id, 9349);
  const unknown = await ndjson("/api/chat/stream", postBody("减持个税如何计算？"));
  assert.equal(unknown.length, 1);
  assert.equal(unknown[0].type, "error");
  assert.match(unknown[0].error, /离线.*未预置/);
  const plain = await request("/api/chat", postBody("减持个税如何计算？"));
  assert.equal(plain.response.status, 400);
  assert.equal(plain.response.ok, false);
  assert.match(plain.data.error, /离线.*未预置/);
  assert.equal(plain.data.detail, plain.data.error);
});

test("非法法规 404，未知 API 404 并记录 offlineErrors", async () => {
  const missing = await request("/api/laws/not-a-number?mode=reader");
  assert.equal(missing.response.status, 404);
  assert.equal(missing.response.ok, false);
  const before = context.offlineErrors.length;
  const unknown = await request("/api/not-provided?q=1");
  assert.equal(unknown.response.status, 404);
  assert.equal(unknown.data.error, "离线演示未提供该接口");
  assert.equal(context.offlineErrors.length, before + 1);
  assert.equal(context.offlineErrors[context.offlineErrors.length - 1].path, "/api/not-provided?q=1");
});

let passed = 0;
for (const { name, fn } of tests) {
  try {
    await fn();
    passed++;
    console.log(`PASS ${name}`);
  } catch (error) {
    console.error(`FAIL ${name}`);
    console.error(error && error.stack || error);
    process.exitCode = 1;
    break;
  }
}
console.log(`${passed}/${tests.length} adapter behavior tests passed`);
