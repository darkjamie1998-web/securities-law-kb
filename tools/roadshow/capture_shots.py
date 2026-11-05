"""为业务路演 HTML 截取各功能界面的真实运行截图。

前置条件：本机服务已在 http://localhost:8000 运行。
输出：docs/roadshow/shots/*.png（1600x1000 视口，2x 缩放）。
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "roadshow" / "shots"
BASE = "http://localhost:8000"
OUT.mkdir(parents=True, exist_ok=True)


def close_chat_if_open(page):
    """对话面板若默认展开则关闭，避免遮挡功能界面。"""
    for sel in ("#chatPanel:not(.hidden) #btnChatClose",
                "#chatPanel:not(.hidden) .chat-close",
                "#btnChatToggle[aria-expanded='true']"):
        btn = page.query_selector(sel)
        if btn:
            btn.click()
            page.wait_for_timeout(300)
            return


def shoot(page, name):
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path))
    print(f"saved {path.name}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000},
                                device_scale_factor=2)

        # 1. 知识图谱全景：等 canvas 数据渲染稳定
        page.goto(f"{BASE}/#/graph")
        page.wait_for_selector("#viewGraph:not(.hidden)", timeout=20000)
        page.wait_for_selector("#viewGraph canvas", timeout=20000)
        page.wait_for_timeout(3500)
        close_chat_if_open(page)
        page.wait_for_timeout(500)
        shoot(page, "graph-full")

        # 2. 图谱板块视图：证券综合（证券法为中心，关联最密）
        chip = page.query_selector(".gb-chip[data-b='证券综合']")
        if chip:
            chip.click()
            page.wait_for_timeout(1500)
            shoot(page, "graph-board")
        else:
            print("WARN: 未找到证券综合板块 chip")

        # 3. 全部法规列表
        page.goto(f"{BASE}/#/laws")
        page.wait_for_selector("#viewList:not(.hidden)", timeout=20000)
        page.wait_for_timeout(1500)
        shoot(page, "law-list")

        # 4. 证券法详情：正文 + 条号目录 + Wiki 解读面板
        page.goto(f"{BASE}/#/law/20")
        page.wait_for_selector("#viewDetail:not(.hidden)", timeout=20000)
        page.wait_for_timeout(2000)
        wiki_text = (page.text_content("#wikiBox") or "").strip()
        print(f"wiki 面板当前内容长度: {len(wiki_text)}")
        if len(wiki_text) < 50:
            btn = page.query_selector("#btnWiki:not([disabled])")
            if btn:
                print("生成 Wiki 解读中（LLM，最多等 120s）...")
                btn.click()
                try:
                    page.wait_for_function(
                        "(document.querySelector('#wikiBox')||{}).textContent.length > 200",
                        timeout=120000)
                except Exception as e:
                    print(f"WARN: wiki 生成等待超时: {e}")
        page.wait_for_timeout(800)
        shoot(page, "law-detail")

        # 5. 关联法规面板
        page.click("#tabRelations")
        page.wait_for_selector("#relationPanel:not(.hidden)", timeout=10000)
        page.wait_for_timeout(1500)
        shoot(page, "law-relations")

        browser.close()


if __name__ == "__main__":
    main()
