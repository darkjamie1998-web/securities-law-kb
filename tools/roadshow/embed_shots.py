"""把 docs/roadshow/shots/ 下的功能截图以 base64 嵌入业务路演 HTML。

锚点：在指定现有截图区块之前插入；带 data-shot 属性，重复执行时替换同名区块。
"""

import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "docs" / "roadshow" / "shots"
TARGET = ROOT / "证券法律法规知识库-业务路演.html"

# (data-shot, 图片文件, 标题, 副标题, 图注, 插入到哪个现有区块标题之前)
NEW_BLOCKS = [
    ("law-list", "law-list.png",
     "全部法规：853 部法规目录与效力状态",
     "按效力级别浏览 · 现行 / 已修订 / 已废止标记",
     "法规目录按效力级别分组浏览，每部法规标注效力级别、库内标记状态、发布信息与官方来源；点击标题进入条文阅读器。",
     "减持规则检索"),
    ("law-detail", "law-detail.png",
     "条文阅读器：条号目录、篇内搜索与 AI 解读同屏",
     "示例：《中华人民共和国证券法》 · 共 227 条",
     "左侧条号目录直达任意条文，正文支持篇内搜索与复制条文；右侧 Wiki 解读由大模型生成并缓存，帮助快速建立法规整体认识，具体适用仍以原文为准。",
     "对话研究"),
    ("law-relations", "law-relations.png",
     "关联法规：从一部法看到整个规则体系",
     "《证券法》关联 678 部 · 引用 / 上位法 / 程序衔接 / 同一事项 / 修订替代",
     "大模型批量抽取法规间关联并分类标注；每条关联附条文级依据说明，点击即可跳到关联法规继续追溯，形成可复核的规则脉络。",
     "对话研究"),
    ("graph-full", "graph-full.png",
     "知识图谱：853 部法规、4,902 条关联一屏总览",
     "16 个业务板块 · 节点半径 ∝ 关联数 · 按效力级别着色",
     "全库法规关联全景：发行上市、交易结算、监管执法等 16 个业务板块可独立查看子图；支持滚轮缩放、拖拽平移，点击节点直达法规详情。",
     "对话研究"),
    ("graph-board", "graph-board.png",
     "板块子图：以《证券法》为中心的关联辐射",
     "证券综合板块 · 悬停高亮关联 · 点击节点进入法规",
     "悬停任意法规即可高亮其关联网络并显示关联数量；示例中《证券法》关联 678 部法规，是理解证券规则体系的入口。",
     "对话研究"),
]

BLOCK_TPL = """        <div class="screenshot-block" data-shot="{key}">
          <div class="screenshot-head">
            <h3>{title}</h3>
            <p>{subtitle}</p>
          </div>
          <figure>
            <div class="screenshot-frame"><img src="data:image/png;base64,{b64}" alt="{title}"></div>
            <figcaption>{caption}</figcaption>
          </figure>
        </div>
"""


def block_start(html, h3_text):
    """找到包含指定 h3 文本的 screenshot-block 起始偏移。"""
    pos = html.index(f"<h3>{h3_text}")
    start = html.rfind('<div class="screenshot-block"', 0, pos)
    if start < 0:
        raise RuntimeError(f"未找到 {h3_text} 所在的 screenshot-block")
    return start


def main():
    html = TARGET.read_text(encoding="utf-8")

    # 幂等：先移除所有已嵌入的 data-shot 区块（含尾部换行）
    html = re.sub(
        r'[ \t]*<div class="screenshot-block" data-shot="[^"]+">.*?</figure>\s*</div>\n',
        "", html, flags=re.S)

    for key, filename, title, subtitle, caption, anchor in NEW_BLOCKS:
        b64 = base64.b64encode((SHOTS / filename).read_bytes()).decode("ascii")
        block = BLOCK_TPL.format(key=key, title=title, subtitle=subtitle,
                                 b64=b64, caption=caption)
        start = block_start(html, anchor)
        # 锚点区块前可能有前导空行，插入并保持一个空行分隔
        html = html[:start] + block + "\n" + html[start:]
        print(f"embedded {key}: {len(b64)//1024} KB, before 「{anchor}」")

    TARGET.write_text(html, encoding="utf-8")
    print(f"done, total {TARGET.stat().st_size/1024/1024:.1f} MB")


if __name__ == "__main__":
    main()
