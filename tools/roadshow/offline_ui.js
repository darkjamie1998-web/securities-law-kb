/* tools/roadshow/offline_ui.js — 单文件快照的离线演示界面约束（ES2017） */
(function (window, document) {
  "use strict";

  var OFFLINE_LINK_MESSAGE = "需要联网打开官方原文";
  var OFFLINE_WIKI_MESSAGE = "完全离线演示不生成或重新生成解读";
  var noticeTimer = null;

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function installStyles() {
    var style = element("style");
    style.id = "offline-ui-style";
    style.textContent = [
      ".offline-demo-banner{flex:none;padding:10px 18px;background:#fff7df;color:#5f4300;border-bottom:1px solid #e5d39b;line-height:1.55;}",
      ".offline-demo-banner strong{display:block;font-size:14px;color:#553b00;}",
      ".offline-demo-banner span{display:block;margin-top:3px;font-size:12px;}",
      ".workmate-attribution{flex:none;margin:0;padding:9px 16px;border-top:1px solid #d4dfe9;background:#fff;color:#42566b;text-align:center;font-size:12px;line-height:1.5;}",
      ".offline-link-notice{position:fixed;left:50%;bottom:52px;z-index:200;max-width:90%;padding:12px 18px;transform:translateX(-50%);border:1px solid #d4a72c;border-radius:7px;background:#fff7df;color:#553b00;box-shadow:0 5px 20px rgba(12,41,73,.2);font-size:14px;line-height:1.5;text-align:center;}",
      ".offline-link-notice[hidden]{display:none;}",
      ".offline-settings-note{margin:0 0 12px;padding:10px 12px;border:1px solid #d4dfe9;border-radius:6px;background:#edf2f7;color:#42566b;font-size:12px;line-height:1.6;}",
      "#settingsModal input[readonly]{background:#f3f6fa;color:#42566b;cursor:default;}",
      "@media (max-width:760px){.offline-demo-banner{padding:9px 12px}.offline-demo-banner strong{font-size:13px}.workmate-attribution{padding:8px 12px}.offline-link-notice{bottom:48px;width:calc(100% - 24px);max-width:none;padding:11px 12px}}"
    ].join("\n");
    document.head.appendChild(style);
  }

  function installBanner() {
    var fixture = window.OFFLINE_FIXTURE || {};
    var stats = fixture.stats || {};
    var laws = fixture.laws || {};
    var articles = fixture.article_by_id || {};
    var chats = fixture.chats || {};
    var banner = element("section", "offline-demo-banner");
    banner.setAttribute("aria-label", "离线演示数据范围");
    banner.appendChild(element(
      "strong", "", "完全离线演示｜精选8部法规｜对话为预置问答"
    ));
    banner.appendChild(element(
      "span", "",
      "全库统计快照：" + String(stats.laws || 0) + " 部法规、" +
      String(stats.articles || 0) + " 条法条；精选交互数据：" +
      String(Object.keys(laws).length) + " 部法规、" +
      String(Object.keys(articles).length) + " 条法条、" +
      String(Object.keys(chats).length) + " 组预置问答。" +
      "顶栏和首页统计卡片属于全库快照，检索、阅读、关联与图谱仅限精选数据。"
    ));
    var topbar = document.querySelector(".topbar");
    topbar.parentNode.insertBefore(banner, topbar.nextSibling);

    var statBadge = document.getElementById("statBadge");
    statBadge.title = "全库统计快照，非精选交互数据量";
    var trustLabel = document.querySelector(".trust-label");
    if (trustLabel) trustLabel.textContent = "全库统计快照";
  }

  function installFooter() {
    var footer = element("footer", "workmate-attribution", "由 WORKMATE 生成");
    document.body.appendChild(footer);
  }

  function installSettingsConstraints() {
    var modal = document.getElementById("settingsModal");
    var inputs = modal.querySelectorAll("input");
    Array.prototype.forEach.call(inputs, function (input) {
      input.readOnly = true;
      input.setAttribute("readonly", "readonly");
      input.setAttribute("aria-readonly", "true");
      input.title = "完全离线演示，配置只读";
    });
    var note = element(
      "p", "offline-settings-note",
      "完全离线演示：所有配置只读；保存和连接测试不可用，不会写入配置或发起网络请求。"
    );
    var actions = modal.querySelector(".modal-actions");
    actions.parentNode.insertBefore(note, actions);

    /* 原保存逻辑只显示“保存失败”；把 adapter 返回的具体离线原因继续交给原错误路径展示。 */
    if (typeof API !== "undefined" && !API.offlineSaveWrapped) {
      var originalPutSettings = API.putSettings;
      API.putSettings = function (body) {
        return originalPutSettings(body).then(function (result) {
          if (result && result.ok === false) {
            throw new Error(result.error || "完全离线演示不保存配置");
          }
          return result;
        });
      };
      API.offlineSaveWrapped = true;
    }
  }

  function showOfflineLinkNotice() {
    var notice = document.getElementById("offlineLinkNotice");
    if (!notice) {
      notice = element("div", "offline-link-notice", OFFLINE_LINK_MESSAGE);
      notice.id = "offlineLinkNotice";
      notice.setAttribute("role", "status");
      notice.setAttribute("aria-live", "polite");
      document.body.appendChild(notice);
    }
    notice.textContent = OFFLINE_LINK_MESSAGE;
    notice.removeAttribute("hidden");
    if (noticeTimer) window.clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(function () {
      notice.setAttribute("hidden", "hidden");
    }, 4500);
  }

  function installCatalogPagination() {
    /* 原目录请求与分页器仍硬编码 20；精选 8 部法规按 6 部两页展示。
     * 只改写离线 API 参数及分页渲染输入，不复制或变更业务加载逻辑。 */
    if (typeof API === "undefined" || API.offlineCatalogWrapped) return;
    var originalLaws = API.laws;
    API.laws = function (params) {
      var adjusted = Object.assign({}, params || {});
      if (Number(adjusted.page_size) === 20) adjusted.page_size = 6;
      return originalLaws(adjusted);
    };
    API.offlineCatalogWrapped = true;
    if (typeof window.renderPager === "function" && !window.renderPager.offlineCatalogWrapped) {
      var originalRenderPager = window.renderPager;
      var offlineRenderPager = function (box, route, page, total, pageSize, pageKey) {
        return originalRenderPager(box, route, page, total,
          pageSize === 20 ? 6 : pageSize, pageKey);
      };
      offlineRenderPager.offlineCatalogWrapped = true;
      window.renderPager = offlineRenderPager;
    }
  }

  function installWikiGenerationGuard() {
    /* 实际 DOM 只有 #btnWiki，按缓存状态显示“生成解读/重新生成”。
     * 捕获阶段截停原 onclick，避免 confirm(token) 和 30-60 秒假进度。 */
    document.addEventListener("click", function (event) {
      var target = event.target;
      var button = target && target.closest ? target.closest("#btnWiki") : null;
      if (!button) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      var box = document.getElementById("wikiBox");
      var notice = document.getElementById("offlineWikiNotice");
      if (!notice) {
        notice = element("div", "offline-wiki-notice empty-compact", OFFLINE_WIKI_MESSAGE);
        notice.id = "offlineWikiNotice";
        notice.setAttribute("role", "status");
        box.insertBefore(notice, box.firstChild);
      }
      notice.textContent = OFFLINE_WIKI_MESSAGE;
      document.getElementById("wikiMeta").textContent = "完全离线演示，不调用大模型";
    }, true);
  }

  function installExternalLinkGuard() {
    /* 捕获阶段只拦截显式 http(s) 锚点；内部 hash、关联、证据跳转完全走原逻辑。 */
    document.addEventListener("click", function (event) {
      var target = event.target;
      var anchor = target && target.closest ? target.closest("a[href]") : null;
      if (!anchor) return;
      var href = anchor.getAttribute("href") || "";
      if (!/^https?:\/\//i.test(href)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      showOfflineLinkNotice();
    }, true);
  }

  function initOfflineUi() {
    if (window.offlineUiInitialized) return;
    window.offlineUiInitialized = true;
    installStyles();
    installBanner();
    installFooter();
    installSettingsConstraints();
    installCatalogPagination();
    installWikiGenerationGuard();
    installExternalLinkGuard();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initOfflineUi, { once: true });
  } else {
    initOfflineUi();
  }
})(window, document);
