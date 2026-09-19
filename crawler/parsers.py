# crawler/parsers.py — 法条切分、HTML/PDF 正文提取、各来源解析分派
import hashlib
import re

from bs4 import BeautifulSoup

# 行首"第X条"（支持中文数字、阿拉伯数字、"之一/之二"后缀）
ARTICLE_RE = re.compile(
    r"^(第[一二三四五六七八九十百千零〇\d]+条(?:之[一二三四五六七八九十\d]+)?)\s*",
    re.MULTILINE,
)


def split_articles(full_text: str) -> list[tuple[str, str]]:
    """把法规全文切成 [(条号, 条文), ...]。无'条'结构时返回 []（调用方决定整篇入库）。"""
    if not full_text:
        return []
    matches = list(ARTICLE_RE.finditer(full_text))
    arts = []
    for i, m in enumerate(matches):
        start = m.start()
        # 条号必须独立成行或前有换行（finditer 已锚定行首 ^ MULTILINE）
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        no = m.group(1)
        text = full_text[start:end].strip()
        # 去掉条号前缀，保留正文
        body = text[len(no):].strip() or text
        arts.append((no, body))
    return arts


def content_hash(full_text: str) -> str:
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()


def extract_text_from_html(html: str) -> str:
    """HTML → 纯文本（去 script/style，压缩空行）。"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_text_from_pdf(pdf_path) -> str:
    """PDF → 纯文本（惰性导入 pdfplumber）。"""
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("需要安装 pdfplumber：.venv/Scripts/python.exe -m pip install pdfplumber") from e
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


# ---- 各来源列表页/详情页解析（返回统一结构） ----
# LawDoc 字段：title / doc_number / issuer / dates / full_text / source_url

def parse_law_generic(html: str, source_url: str) -> dict:
    """通用详情页解析：title 取 <title> 或 h1，正文取 body 最大文本块。"""
    soup = BeautifulSoup(html, "lxml")
    title = ""
    for sel in ("h1", "title"):
        el = soup.find(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            break
    full_text = extract_text_from_html(html)
    return {"title": title, "full_text": full_text, "source_url": source_url}


PARSERS = {
    "generic": parse_law_generic,
    # 各来源专属解析器在真实页面适配时逐个补齐（见 plan M2 注意事项）
    "flk": parse_law_generic,
    "csrc": parse_law_generic,
    "sse": parse_law_generic,
    "szse": parse_law_generic,
    "bse": parse_law_generic,
    "court": parse_law_generic,
}


def parse_law(html: str, source_url: str, method: str = "generic") -> dict:
    parser = PARSERS.get(method, parse_law_generic)
    return parser(html, source_url)


def extract_links(html: str, base_url: str, pattern: str = r".*") -> list[tuple[str, str]]:
    """列表页提取 (链接文本, 绝对 URL)，按正则过滤链接文本或 href。"""
    soup = BeautifulSoup(html, "lxml")
    rx = re.compile(pattern)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or not rx.search(text) and not rx.search(href):
            continue
        from urllib.parse import urljoin
        url = urljoin(base_url, href)
        if url.startswith("http") and url not in seen:
            seen.add(url)
            out.append((text, url))
    return out
