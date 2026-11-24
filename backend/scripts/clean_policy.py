"""T12 · 政策语料清洗脚本（raw → cleaned，结构化）。

职责（与 fetch 脚本解耦：本脚本**只读 raw、只产 cleaned**，不联网）
--------------------------------------------------------------------
1. 读取 ``data/raw/policy/*.json``（fetch_policy.py 留档的原文）；
2. 清洗正文：**只去噪、不改写**——
   - 去掉网页头噪声：标题行 / ``时间 : …`` / ``来源 : …`` / ``文号 ：`` / ``分享到：`` / ``【字体：…】``；
   - 收敛空白：行内多余空格、连续空行、零宽字符；
   - 还原被 HTML 断行切碎的「中文被换行分隔」情形（如 ``根据《关于做好\\n2026年…``）。
   - **绝不删除正文实质语句**；不补写、不概括、不翻译任何条文。
3. 计算 ``doc_hash``（清洗后正文 sha256）用于去重；
4. 输出 ``data/cleaned/policy_docs.csv``（UTF-8-SIG）：
   ``title,url,source,province,year,doc_type,column,published_at,content,content_len,doc_hash``。

红线
----
- **不编造政策条文**：清洗仅做机械去噪，**任何一句政策表述都来自原文**；
- **保留来源 URL 与年份**：``url``/``year`` 原样透传，缺失则显式留空（不猜）；
- **同标题去重**：同一 ``title`` 只保留「正文更长」的一篇（正文更长的信息更完整），
  并在报告中列出被丢弃项（可追溯）；
- **不静默失败**：解析失败/正文过短的行计入 ``dropped`` 并在报告中如实列出。

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/clean_policy.py
    cd backend && ./.venv/Scripts/python.exe scripts/clean_policy.py --min-len 120
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "policy"
OUT_CSV = PROJECT_ROOT / "data" / "cleaned" / "policy_docs.csv"

# 正文最短长度（低于此值视为噪声/解析残缺，丢弃并记录）
DEFAULT_MIN_LEN = 80

# ---- 头部噪声行（整行匹配则删） ----
# 例：时间  :  2026-07-10 17:30:33 / 来源  :  广东省教育考试院 / 文号 ： / 分享到：
_HEADER_NOISE = re.compile(
    r"^\s*(时间|来源|文号|分享到|打印|字体|【字体|发布日期|浏览次数|责任编辑|扫描二维码)\s*[:：]?.*$"
)
_FONT_BAR = re.compile(r"^.*【\s*(大|中|小)\s*】.*$")

# 零宽字符与特殊空白
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


def _strip_noise(text: str, title: str) -> str:
    """对正文做机械去噪（只删噪声、不改写）。"""
    text = _ZERO_WIDTH.sub("", text)
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        s = line.replace("\u3000", " ").rstrip()
        stripped = s.strip()
        # 1) 空行
        if not stripped:
            # 折叠连续空行（用哨兵 None 标记，最后统一处理）
            if out and out[-1] is not None:
                out.append(None)
            continue
        # 2) 头部噪声行
        if _HEADER_NOISE.match(stripped) or _FONT_BAR.match(stripped):
            continue
        # 3) 正文首行重复标题
        if not out and stripped == title.strip():
            continue
        # 4) 行内多空格收敛（保留单个空格）
        s = re.sub(r"[ \t]{2,}", " ", s)
        out.append(s)

    # 去掉首尾空行哨兵
    while out and out[0] is None:
        out.pop(0)
    while out and out[-1] is None:
        out.pop()

    # 拼接：还原被网页换行切断的语句（数字逐位换行、"根\n据"等）
    buf: list[str] = []
    for item in out:
        if item is None:
            buf.append("\n\n")
            continue
        if buf and buf[-1] and not buf[-1].endswith("\n\n"):
            prev = buf[-1]
            if _should_join(prev, item):
                buf[-1] = prev + item
                continue
            buf[-1] = prev + "\n" + item
            continue
        buf.append(item)
    text = "".join(buf)

    # 折叠 3+ 连续换行为 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# 行首「新段落标记」：编号/条目起头（这些行必须另起一段，不能与上一行黏连）
_MARKER_CORE = (
    r"("
    r"[一二三四五六七八九十]+[、．.]"          # 一、 二、
    r"|（[一二三四五六七八九十\d]+）"           # （一） （1）
    r"|\([一二三四五六七八九十\d]+\)"           # (一) (1)
    r"|\d+[、．.]\d*"                          # 1. 2、 3.1
    r"|附件\s*\d*"                             # 附件 / 附件1
    r"|第[一二三四五六七八九十\d]+[条章节款]"   # 第三条
    r")"
)
_LINE_START_MARKER = re.compile(r"^\s*" + _MARKER_CORE)
# 仅标记、其后无文字（如单独一个「（一）」「1.」）→ 说明其正文在下一行，应相接
_MARKER_ONLY = re.compile(r"^\s*" + _MARKER_CORE + r"\s*$")
# 行尾「句终标点」：出现则说明上一行已完整，不该与下一行黏连
_LINE_END_PUNCT = "。！？；：!?;:"


def _should_join(prev_line: str, next_line: str) -> bool:
    """判断两行是否属于「同一句被网页换行切断」，应直接相接。

    判据（保守优先，宁可保换行也不误黏）：
    1. 下一行是编号/条目起头 → **不接**（另起段）；
    2. 上一行是「有文字的编号标题」（如「二、报考基本条件」）→ **不接**（独占一行）；
       但若上一行**只有编号无文字**（如「（一）」「1.」，正文在下一行）→ **接**；
    3. 上一行以句终标点结尾 → **不接**（已完整）；
    4. 其余情况视为被切断（数字被逐位换行、"根\\n据"等）→ **接**。
    """
    nxt = next_line.lstrip()
    if _LINE_START_MARKER.match(nxt):
        return False
    # 只看 buf 的**最后一个物理行**判断（prev 可能是多行累积串）
    prev = prev_line.rsplit("\n", 1)[-1].rstrip()
    if _LINE_START_MARKER.match(prev):
        # 只有编号、无文字的裸标记 → 正文在下一行，相接
        if _MARKER_ONLY.match(prev):
            return True
        # 去掉编号标记后的剩余文字（标题正文）
        rest = _LINE_START_MARKER.sub("", prev, count=1).strip()
        # 剩余是纯数字 → 说明是「编号+被换行切碎的数字」（如「（一）202」），应相接
        if rest.isdigit():
            return True
        # 有文字的编号标题 → 独占一行
        if rest and not prev.endswith(tuple(_LINE_END_PUNCT)) and len(prev) <= 24:
            return False
    if prev and prev[-1] in _LINE_END_PUNCT:
        return False
    return True


def _load_raw() -> list[dict]:
    if not RAW_DIR.exists():
        return []
    recs: list[dict] = []
    for f in sorted(RAW_DIR.glob("*.json")):
        if f.name == "sources.json":
            continue
        try:
            recs.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  ✗ 读取失败 {f.name}：{exc}", file=sys.stderr)
    return recs


def clean_one(rec: dict, *, min_len: int) -> tuple[dict | None, str, int]:
    """清洗单条。返回 (清洗后记录 | None, 丢弃原因, 清洗后正文字数)。"""
    title = (rec.get("title") or "").strip()
    raw_content = rec.get("content") or ""
    if not title:
        return None, "缺标题", 0
    if not raw_content.strip():
        return None, "正文为空", 0

    content = _strip_noise(raw_content, title)
    if len(content) < min_len:
        return None, f"正文过短({len(content)}<{min_len})", len(content)

    out = {
        "title": title,
        "url": (rec.get("url") or "").strip(),
        "source": (rec.get("source") or "").strip(),
        "province": (rec.get("province") or "").strip(),
        "year": rec.get("year"),
        "doc_type": (rec.get("doc_type") or "").strip(),
        "column": (rec.get("column") or "").strip(),
        "published_at": (rec.get("published_at") or "").strip(),
        "content": content,
        "content_len": len(content),
        "doc_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    return out, "", len(content)


def dedupe(rows: list[dict], *, keep: str = "longer") -> tuple[list[dict], list[tuple[dict, dict]]]:
    """按 ``title`` 去重：同标题保留正文更长者。返回 (保留行, [(被丢弃, 保留的)…])。"""
    best: dict[str, dict] = {}
    dropped: list[tuple[dict, dict]] = []
    for r in rows:
        key = r["title"]
        if key not in best:
            best[key] = r
            continue
        cur = best[key]
        cur_len, new_len = cur["content_len"], r["content_len"]
        if new_len > cur_len:
            dropped.append((cur, r))
            best[key] = r
        else:
            dropped.append((r, cur))
    ordered = sorted(best.values(), key=lambda x: (x["year"] or 0, x["published_at"]), reverse=True)
    return ordered, dropped


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "title", "url", "source", "province", "year", "doc_type",
        "column", "published_at", "content", "content_len", "doc_hash",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def render_report(rows: list[dict], dropped: list[tuple[dict, dict]], raw_count: int, min_len: int) -> str:
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append("政策语料清洗报告 · 广东省教育考试院")
    lines.append("=" * 66)
    lines.append(f"raw 原文篇数        : {raw_count}")
    lines.append(f"正文最短阈值        : {min_len} 字")
    lines.append(f"清洗后保留          : {len(rows)} 篇")
    lines.append(f"去重/丢弃           : {len(dropped)} 篇")
    lines.append("")

    if rows:
        lens = sorted(r["content_len"] for r in rows)
        n = len(lens)
        lines.append("正文长度分布")
        lines.append(
            f"  最小 {lens[0]} / p25 {lens[n // 4]} / 中位 {lens[n // 2]} / "
            f"p75 {lens[(3 * n) // 4]} / 最大 {lens[-1]}"
        )
        lines.append("")

        lines.append("年份分布")
        for y, c in sorted(Counter(r["year"] for r in rows).items(), key=lambda x: (x[0] is None, x[0]), reverse=True):
            lines.append(f"  {y if y is not None else '(缺失)':>6} : {c}")
        lines.append("")

        lines.append("文档类型分布")
        for t, c in Counter(r["doc_type"] for r in rows).most_common():
            lines.append(f"  {t or '(空)':<6} : {c}")
        lines.append("")

        lines.append("栏目分布")
        for t, c in Counter(r["column"] for r in rows).most_common():
            lines.append(f"  {t or '(空)':<10} : {c}")
        lines.append("")

    if dropped:
        lines.append("被丢弃明细（可追溯）")
        for d, kept in dropped:
            lines.append(f"  - [{d['content_len']}字] {d['title'][:44]}")
            if kept["content_len"] > 0:
                lines.append(f"      → 保留更完整版本 [{kept['content_len']}字] {kept['title'][:44]}")
            else:
                lines.append(f"      → 原因：{kept['title']}")
        lines.append("")

    # 红线自证
    no_url = [r for r in rows if not r["url"]]
    no_year = [r for r in rows if r["year"] is None]
    lines.append("红线自检")
    lines.append(f"  缺 url  的行 : {len(no_url)}（须为 0）")
    lines.append(f"  缺 year 的行 : {len(no_year)}（缺失应显式留空，非猜测）")
    dup_titles = [t for t, c in Counter(r["title"] for r in rows).items() if c > 1]
    lines.append(f"  重复标题     : {len(dup_titles)}（须为 0）")
    lines.append("")
    lines.append("口径说明：清洗仅做机械去噪（页头噪声/空白/换行还原），不增删任何政策表述；")
    lines.append("         正文全部来自详情页原文，未做任何改写或概括。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="政策语料清洗（raw → cleaned）")
    parser.add_argument("--min-len", type=int, default=DEFAULT_MIN_LEN, help="正文最短字数（默认 80）")
    parser.add_argument("--out", type=Path, default=OUT_CSV, help="输出 CSV 路径")
    args = parser.parse_args(argv)

    raw = _load_raw()
    print(f"读取 raw 原文 {len(raw)} 篇 <- {RAW_DIR}")
    if not raw:
        print("✗ 没有 raw 原文，请先运行 scripts/fetch_policy.py 采集。", file=sys.stderr)
        return 1

    cleaned: list[dict] = []
    dropped: list[tuple[dict, dict]] = []
    for rec in raw:
        row, reason, clen = clean_one(rec, min_len=args.min_len)
        if row is None:
            # 丢弃项：用真实标题与清洗后长度留痕，reason 作为「保留原因」占位供报告展示
            dropped.append(
                (
                    {"title": (rec.get("title") or "").strip(), "content_len": clen},
                    {"title": reason, "content_len": 0},
                )
            )
            continue
        cleaned.append(row)

    rows, dup_dropped = dedupe(cleaned)
    dropped.extend(dup_dropped)

    write_csv(rows, args.out)
    report = render_report(rows, dropped, len(raw), args.min_len)
    print(report)
    print(f"\n已写出：{args.out}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
