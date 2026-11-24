"""T12 · 政策语料采集脚本（广东省教育考试院公开栏目）。

职责（与 clean 脚本解耦：本脚本只负责「拿到并留档原文 + 登记来源」）
--------------------------------------------------------------------
1. 从**公开栏目页**收集政策文章链接（普通高考 / 通知公告 / 省考试院文件 等）；
2. 逐个抓取详情页，解析 `title / url / source / year / doc_type / content / published_at`；
3. 原文按 ``doc_hash``（正文 sha256）落 ``data/raw/policy/<doc_hash>.json``（**幂等**）；
4. 维护 ``data/raw/policy/sources.json``：记录每个来源栏目的 URL、抓取时间、命中数。

红线
----
- **只采公开内容**：仅抓取考试院对外发布的公文/公告/问答，**不采集付费/登录后内容**；
- **保留来源 URL 与年份**：``url`` 必须是详情页原始地址，``year`` 取自发布年份；
- **不编造政策条文**：正文一律来自详情页原始 HTML，**绝不人工补写**；解析失败如实报错不静默；
- **礼貌抓取**：请求间 sleep（默认 0.8s），带 UA，单次请求超时与重试有上限。

易混淆
------
- 政策语料（RAG 文档，非结构化） ≠ 投档数据（结构化表）—— 分属不同存储（本脚本只产 raw，不写 MySQL）；
- 本脚本 ≠ 清洗脚本：raw 里保留原始正文（含少量换行），清洗与去噪交给 ``clean_policy.py``。

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/fetch_policy.py            # 采集（增量，已存在的跳过）
    cd backend && ./.venv/Scripts/python.exe scripts/fetch_policy.py --list     # 仅列出发现的链接，不抓取
    cd backend && ./.venv/Scripts/python.exe scripts/fetch_policy.py --max-pages 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "policy"
SOURCES_JSON = RAW_DIR / "sources.json"

PROVINCE = "广东"
SITE = "广东省教育考试院"
BASE = "https://eea.gd.gov.cn"

# 采集的公开栏目（path → 栏目名）。均为考试院对外公开栏目，无登录门槛。
CATEGORIES: dict[str, str] = {
    "ptgk": "普通高考",
    "tzgg": "通知公告",
    "bmbk_sksywj": "省考试院文件",
    "zwgk_zkgs": "招考公示",
}

# 请求节流与超时（礼貌抓取）
REQUEST_SLEEP = 0.8
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ============================================================
# HTTP
# ============================================================


def _fetch(url: str, *, timeout: int = REQUEST_TIMEOUT, retries: int = MAX_RETRIES) -> str | None:
    """带重试与超时的 GET，返回解码后的 HTML；失败返回 None（不抛，由调用方汇总）。"""
    last_err: str = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            # 政府站点多为 utf-8；容错 GBK
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("gbk", errors="ignore")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(0.8 * attempt)
    print(f"    ✗ 抓取失败：{url}（{last_err}）", file=sys.stderr)
    return None


# ============================================================
# 列表页解析
# ============================================================


def _norm_url(href: str) -> str | None:
    if not href:
        return None
    url = urllib.parse.urljoin(BASE + "/", href)
    if "/content/post_" not in url or not url.endswith(".html"):
        return None
    return url.split("#")[0]


def discover_links(category_path: str, *, max_pages: int = 1, sleep: float = REQUEST_SLEEP) -> list[dict]:
    """从一个栏目页发现文章链接。

    考试院列表页把文章链接直接渲染在 HTML 中（``/xxx/content/post_<id>.html``），
    分页形如 ``index.html`` / ``index_2.html`` …
    """
    found: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        path = f"/{category_path}/" if page == 1 else f"/{category_path}/index_{page}.html"
        html = _fetch(BASE + path)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        page_hits = 0
        for a in soup.find_all("a", href=True):
            url = _norm_url(a["href"])
            if not url or url in seen:
                continue
            title = a.get_text(strip=True)
            if not title or len(title) < 6:  # 过滤导航/杂项短链接
                continue
            seen.add(url)
            found.append({"title": title, "url": url, "column": CATEGORIES.get(category_path, category_path)})
            page_hits += 1
        if page_hits == 0:
            break  # 该栏目没有更多分页
        time.sleep(sleep)
    return found


# ============================================================
# 详情页解析
# ============================================================


def _meta(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": name})
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    return None


def _extract_body(soup: BeautifulSoup) -> str:
    """抽取正文纯文本。优先常见正文容器，退化为 body。

    政府站的正文容器类名不统一，这里按「候选容器 + 取文本最长者」策略择一，
    避免依赖单个易变的 class 名。
    """
    candidates = []
    for sel in (
        "div.zwxl-article",
        "div.article-content",
        "div.content",
        "div#zoom",
        "div.TRS_Editor",
        "div.news_content",
        "div.detail",
    ):
        for node in soup.select(sel):
            txt = node.get_text("\n", strip=True)
            if txt:
                candidates.append(txt)
    if candidates:
        # 取最长者（正文通常最长）
        return max(candidates, key=len)
    # 退化：整页可见文本（去掉脚本/样式）
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _parse_year(*texts: str | None) -> int | None:
    """从发布日期/标题里抽四位年份（优先发布日期）。"""
    for t in texts:
        if not t:
            continue
        m = re.search(r"(20\d{2})", t)
        if m:
            return int(m.group(1))
    return None


_DOC_TYPE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("问答", "答考生问", "热点问题"), "问答"),
    (("通知", "通告", "公告"), "通知"),
    (("规定", "办法", "实施细则", "工作意见", "方案"), "办法"),
    (("公示", "名单"), "公示"),
    (("安排", "提示", "温馨提示"), "提示"),
]


def _doc_type(title: str) -> str:
    for keys, label in _DOC_TYPE_RULES:
        if any(k in title for k in keys):
            return label
    return "其他"


def parse_article(url: str, *, fallback_title: str = "", column: str = "") -> dict | None:
    """抓取并解析一篇详情页。返回结构化 dict；正文过短或解析失败返回 None。"""
    html = _fetch(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    title = _meta(soup, "ArticleTitle") or fallback_title
    if not title:
        m = soup.find("title")
        title = m.get_text(strip=True).split("_")[0] if m else ""
    title = title.replace("&nbsp;", " ").strip()

    body = _extract_body(soup)
    if len(body) < 50:  # 正文过短 → 视为解析失败，不静默接受
        print(f"    ✗ 正文过短（{len(body)} 字），跳过：{url}", file=sys.stderr)
        return None

    pub = _meta(soup, "PubDate") or ""
    source = _meta(soup, "ContentSource") or SITE
    year = _parse_year(pub, title)

    return {
        "title": title,
        "url": url,
        "source": source,
        "province": PROVINCE,
        "year": year,
        "doc_type": _doc_type(title),
        "column": column or _meta(soup, "ColumnName") or "",
        "published_at": pub,
        "content": body,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================
# 落档
# ============================================================


def doc_hash_of(record: dict) -> str:
    """去重指纹：正文 sha256（内容相同即视为同一篇，标题微调不影响）。"""
    return hashlib.sha256(record["content"].encode("utf-8")).hexdigest()


def save_record(record: dict) -> tuple[str, bool]:
    """落档一条原文。返回 (doc_hash, 是否新增)。已存在（同 hash）则跳过。"""
    dh = doc_hash_of(record)
    path = RAW_DIR / f"{dh}.json"
    if path.exists():
        return dh, False
    payload = {**record, "doc_hash": dh}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dh, True


def load_sources() -> dict:
    if not SOURCES_JSON.exists():
        return {"province": PROVINCE, "site": SITE, "categories": {}, "fetched": {}}
    return json.loads(SOURCES_JSON.read_text(encoding="utf-8"))


def save_sources(data: dict) -> None:
    SOURCES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ============================================================
# 主流程
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="政策语料采集（广东省教育考试院公开栏目）")
    parser.add_argument("--list", action="store_true", help="仅发现并列出链接，不抓取详情")
    parser.add_argument("--max-pages", type=int, default=1, help="每个栏目最多翻页数（默认 1）")
    parser.add_argument("--sleep", type=float, default=REQUEST_SLEEP, help="请求间隔秒数（礼貌抓取）")
    args = parser.parse_args(argv)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()

    print("=" * 66)
    print(f"政策语料采集 · {SITE}（{'只列链接' if args.list else '抓取详情'}）")
    print("=" * 66)

    all_records: list[dict] = []
    for path, column in CATEGORIES.items():
        links = discover_links(path, max_pages=args.max_pages, sleep=args.sleep)
        print(f"\n[{column}] {BASE}/{path}/ → 发现 {len(links)} 条链接")
        sources.setdefault("categories", {})[path] = {
            "column": column, "url": f"{BASE}/{path}/", "found": len(links),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if args.list:
            for lk in links[:8]:
                print(f"    - {lk['title'][:44]} | {lk['url']}")
            if len(links) > 8:
                print(f"    … 其余 {len(links) - 8} 条")
            all_records.extend(links)
            continue

        for lk in links:
            time.sleep(args.sleep)
            rec = parse_article(lk["url"], fallback_title=lk["title"], column=column)
            if not rec:
                continue
            dh, is_new = save_record(rec)
            flag = "+ 新增" if is_new else "· 已存在"
            print(f"    {flag} [{rec['doc_type']}] {rec['title'][:36]}… ({len(rec['content'])} 字)")
            all_records.append(rec)

    sources["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not args.list:
        save_sources(sources)

    # ---- 汇总 ----
    if args.list:
        print(f"\n共发现 {len(all_records)} 条候选链接（未抓取详情）。")
        return 0

    files = list(RAW_DIR.glob("*.json"))
    files = [f for f in files if f.name != "sources.json"]
    print("\n" + "-" * 66)
    print(f"本次抓取入库：{len(all_records)} 条；raw 目录现有原文 {len(files)} 篇")
    print(f"留档目录：{RAW_DIR}")
    print("提示：原文为 raw 层（未清洗）；清洗/结构化由 clean_policy.py 完成。")
    return 0 if all_records else 1


if __name__ == "__main__":
    sys.exit(main())
