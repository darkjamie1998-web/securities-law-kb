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


def extract_text_from_docx(docx_path) -> str:
    """Word(.docx) → 纯文本（惰性导入 python-docx，含表格）。"""
    try:
        import docx
    except ImportError as e:
        raise RuntimeError("需要安装 python-docx：.venv/Scripts/python.exe -m pip install python-docx") from e
    d = docx.Document(str(docx_path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_text_from_doc(doc_path) -> str:
    """Word 97-2003 (.doc, OLE2) → 纯文本。

    .doc 二进制格式复杂，这里走实用主义路线：从 WordDocument 流中
    扫描连续 UTF-16LE 中文/ASCII 段落拼回文本（法规文档 95%+ 内容
    为中文+标点+数字，提取质量足够知识库检索使用）。
    """
    try:
        import olefile
    except ImportError as e:
        raise RuntimeError("需要安装 olefile：.venv/Scripts/python.exe -m pip install olefile") from e
    if not olefile.isOleFile(str(doc_path)):
        raise ValueError(f"不是 OLE2 .doc 文件: {doc_path}")
    ole = olefile.OleFileIO(str(doc_path))
    try:
        if not ole.exists("WordDocument"):
            raise ValueError(f".doc 缺少 WordDocument 流: {doc_path}")
        data = ole.openstream("WordDocument").read()
    finally:
        ole.close()

    # 解码为 UTF-16LE 并保留可打印段（中文、ASCII、常用标点、空白）
    try:
        text = data.decode("utf-16-le", errors="ignore")
    except Exception:
        text = ""
    kept, cur = [], []
    for ch in text:
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F      # CJK 统一/标点
                or 0xFF00 <= o <= 0xFFEF                          # 全角
                or ch.isalnum() or ch in "，。、；：？！《》（）“”‘’—…·%.-/ "
                or ch == "\n" or ch == "\r"):
            cur.append(ch)
        else:
            if len(cur) >= 8:                                     # 丢弃短噪声段
                kept.append("".join(cur))
            cur = []
    if len(cur) >= 8:
        kept.append("".join(cur))
    return "\n".join(kept)


def extract_text_from_file(path) -> str:
    """按扩展名自动分派 PDF/DOCX/DOC 文本提取。"""
    p = str(path).lower()
    if p.endswith(".pdf"):
        return extract_text_from_pdf(path)
    if p.endswith(".doc"):
        return extract_text_from_doc(path)
    if p.endswith(".docx"):
        return extract_text_from_docx(path)
    raise ValueError(f"不支持的文件类型: {path}")


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


# ---- 深交所：列表项在 <li><script> 的 curHref/curTitle 变量里，正文多为 PDF ----
SZSE_ITEM_RE = re.compile(
    r"var\s+curHref\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27];"
    r".*?var\s+curTitle\s*=\s*[\"\x27]([^\"\x27]*)[\"\x27];",
    re.S,
)


def extract_szse_list(html: str, base_url: str) -> list[tuple[str, str]]:
    """深交所规则列表：提取 (法规名, 详情/PDF 绝对URL)。"""
    from urllib.parse import urljoin

    out, seen = [], set()
    for m in SZSE_ITEM_RE.finditer(html):
        href, title = m.group(1).strip(), m.group(2).strip()
        if not href or not title or title == "无标题":
            continue
        url = urljoin(base_url, href)
        if url not in seen:
            seen.add(url)
            out.append((title, url))
    return out


# 各来源列表页提取器：method → list 提取函数（缺省用 <a> 标签通用提取）
LIST_EXTRACTORS = {
    "szse": extract_szse_list,
    "szse_render": None,  # 占位，下方实现
}


def extract_szse_render_list(html: str, base_url: str) -> list[tuple[str, str]]:
    """深交所业务规则（JS 渲染页）：Playwright 渲染后取 .newslist 容器内链接。"""
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "lxml")
    box = soup.select_one(".newslist") or soup
    out, seen = [], set()
    for a in box.find_all("a", href=True):
        text = a.get_text(strip=True)
        if len(text) < 6:
            continue
        url = urljoin(str(base_url), a["href"])
        if url.startswith("http") and url not in seen:
            seen.add(url)
            out.append((text, url))
    return out


LIST_EXTRACTORS["szse_render"] = extract_szse_render_list


# 上交所：法规链接特征为 /c/<id>/files/<hash>.docx|pdf 或 c_<date>_<id>.shtm
SSE_URL_RE = re.compile(r"/c/\d+/files/|/c/c_\d+_\d+\.shtm")


def extract_sse_list(html: str, base_url: str) -> list[tuple[str, str]]:
    """上交所法规列表：仅提取 /c/<id>/files/* 与 c_*.shtm 详情链接，排除导航。"""
    out, seen = [], set()
    for text, url in extract_links(html, base_url):
        if SSE_URL_RE.search(url):
            if url not in seen:
                seen.add(url)
                out.append((text, url))
    return out


LIST_EXTRACTORS["sse"] = extract_sse_list


# 北交所：法规在 table 内，附件为 /uploads/**.pdf
BSE_URL_RE = re.compile(r"/uploads/|\.pdf$")


def extract_bse_list(html: str, base_url: str) -> list[tuple[str, str]]:
    """北交所法规列表：取表格内指向 uploads/PDF 的链接。"""
    out, seen = [], set()
    for text, url in extract_links(html, base_url):
        if BSE_URL_RE.search(url):
            if url not in seen:
                seen.add(url)
                out.append((text, url))
    return out


LIST_EXTRACTORS["bse"] = extract_bse_list


def extract_list(html: str, base_url: str, method: str = "generic") -> list[tuple[str, str]]:
    fn = LIST_EXTRACTORS.get(method)
    if fn:
        return fn(html, base_url)
    return extract_links(html, base_url)


def extract_links(html: str, base_url, pattern: str = r".*") -> list[tuple[str, str]]:
    """列表页提取 (链接文本, 绝对 URL)，按正则过滤链接文本或 href。

    base_url 接受 str 或 httpx.URL（自动转 str）。
    """
    soup = BeautifulSoup(html, "lxml")
    rx = re.compile(pattern)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or not rx.search(text) and not rx.search(href):
            continue
        from urllib.parse import urljoin
        url = urljoin(str(base_url), href)
        if url.startswith("http") and url not in seen:
            seen.add(url)
            out.append((text, url))
    return out
