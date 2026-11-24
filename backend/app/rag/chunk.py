"""T13 · 政策文档切片（RAG 一级 → 二级）。

职责（纯函数，无 IO、无 DB、无 LLM）
------------------------------------
把 ``policy_docs.content`` 切成**语义完整的检索单元**（chunk）：

1. **不跨标题**：按行扫描，遇到「层级标题行」即开新块 —— 保证一个 chunk 不横跨两个章节，
   避免检索命中半句、引用错章节（任务卡红线）；
2. **长度控制**：目标 ``CHUNK_SIZE``（默认 500 字），块内超过即切分；
3. **重叠**：相邻块 ``CHUNK_OVERLAP``（默认 50 字）重叠，防边界信息丢失；
4. **关键词**：jieba 分词 + 去停用词 + 词频排序，取 top-N 作 ``tokens``（供关键词打分）。

设计取舍
--------
- **纯函数**：``chunk_document()`` 输入文本 → 输出 ``list[Chunk]``，**不碰 DB**，便于单测；
- 写库在 ``index.py``（入库脚本 vs 检索服务分开，任务卡「易混淆」明确）；
- jieba 在**模块级懒加载**（首次调用构建词典约 1–3s），避免 import 即变慢。

易混淆
------
- 切块（chunk）≠ 检索：本模块只负责「怎么切」，不负责「怎么找」；
- 标题行判定 ≠ Markdown：政策文本用「一、」「（一）」「1.」等中文编号，非 ``#``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---- 切片参数（任务卡：500 字 / 重叠 50）----
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
# 关键词数量上限（写入 policy_chunks.tokens）
KEYWORD_TOP_N = 30
# 单块最短字数：低于此值的尾块并入上一块（避免碎块污染检索）
MIN_CHUNK_LEN = 40

# 标题行：一、/（一）/1./附件/第X条 等（与 clean_policy 的编号识别同源思路）
_HEADING_RE = re.compile(
    r"^\s*("
    r"[一二三四五六七八九十]+[、．.]"
    r"|（[一二三四五六七八九十\d]+）"
    r"|\([一二三四五六七八九十\d]+\)"
    r"|\d+[、．.]\d*"
    r"|附件\s*\d*"
    r"|第[一二三四五六七八九十\d]+[条章节款]"
    r")\s*\S"
)
# 「章节标题」与「条目标题」两种判定阈值：
#  - 章节标题（一、/ 第二章）：可略长，但一般不超 30 字；
#  - 条目标题（（一）/ 1.）：通常只是标签，超过 20 字基本是正文段落。
_HEADING_MAX_LEN = 30
_ITEM_HEADING_MAX_LEN = 20
_ITEM_MARKER_RE = re.compile(r"^\s*(（[一二三四五六七八九十\d]+）|\([一二三四五六七八九十\d]+\)|\d+[、．.]\d*)\s*\S")

# 中文/英文停用词（轻量集，够用即可；避免引入重型 NLP 依赖）
_STOPWORDS = frozenset(
    """的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这
    之 与 及 或 等 对 为 以 于 并 而 其 则 若 但 由 从 被 把 让 使 请 可 应 该 须 需 将 已 已 经
    各 该 本 此 及 其中 相关 有关 如下 以下 上述 规定 通知 工作 事项 要求 情况 进行 按照 根据
    关于 做好 广东省 广东 年 月 日 号 第 条 一 二 三 四 五 六 七 八 九 十
    什么 时候 怎么 怎样 如何 哪些 哪个 哪里 多少 多久 是否 可以 能否 吗 呢 吧 啊 请问 帮我 一下
    告诉 介绍 我想 我要 需要 有没有 是不 是什么 有什么 怎么办 是什么 为什么 具体 一般 通常""".split()
)

_jieba_mod = None


def _jieba():
    """懒加载 jieba（首次调用构建词典较慢 ~1-3s）。"""
    global _jieba_mod
    if _jieba_mod is None:
        import warnings

        with warnings.catch_warnings():
            # jieba 内部正则含未转义的点号，Python 3.12 会告警；与业务无关，抑制之
            warnings.simplefilter("ignore", SyntaxWarning)
            import jieba

            jieba.setLogLevel(60)  # 关闭 jieba 的启动日志噪声
        _jieba_mod = jieba
    return _jieba_mod


@dataclass
class Chunk:
    """一个检索单元。``seq`` 从 0 起，块内 ``heading`` 为所属标题（可空）。"""

    seq: int
    content: str
    heading: str
    tokens: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "content": self.content, "heading": self.heading, "tokens": self.tokens}


# ============================================================
#  分词与关键词
# ============================================================


def tokenize(text: str) -> list[str]:
    """jieba 分词 + 去停用词 + 去纯符号/单字（中英混合时保留英文词与数字）。"""
    words: list[str] = []
    for w in _jieba().lcut(text):
        w = w.strip()
        if not w or w in _STOPWORDS:
            continue
        # 丢弃纯标点/空白
        if not re.search(r"[\w\u4e00-\u9fff]", w):
            continue
        # 中文单字丢弃（噪声大；英文/数字单词保留）
        if len(w) == 1 and re.match(r"[\u4e00-\u9fff]", w):
            continue
        words.append(w)
    return words


def extract_keywords(text: str, top_n: int = KEYWORD_TOP_N) -> str:
    """抽取关键词（按词频降序，空格分隔）。用于 ``policy_chunks.tokens``。"""
    from collections import Counter

    freq = Counter(tokenize(text))
    # 频次降序；同频按词长降序（长词信息量更大）
    ranked = sorted(freq.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    return " ".join(w for w, _ in ranked[:top_n])


# ============================================================
#  切片
# ============================================================


def _is_heading(line: str) -> bool:
    """判定一行是否是「层级标题」：编号起头且足够短。

    条目标题（``（一）``/``1.``）阈值更严（≤20 字），因为这类标签经常直接跟正文，
    长句不应被误判成标题（否则 chunk 会被切得过碎）。
    """
    s = line.strip()
    if not s:
        return False
    if _ITEM_MARKER_RE.match(s):
        return len(s) <= _ITEM_HEADING_MAX_LEN
    if len(s) > _HEADING_MAX_LEN:
        return False
    return bool(_HEADING_RE.match(s))


def _split_paragraphs(text: str) -> list[tuple[str, str]]:
    """把正文按行拆分，输出 ``[(heading, 段落)…]``。

    - 连续非标题行累积为同一段落；
    - 遇标题行则开新段落，标题作为该段的 heading（**不跨标题**的落地）。
    """
    out: list[tuple[str, str]] = []
    cur_heading = ""
    buf: list[str] = []

    def flush() -> None:
        if buf:
            seg = "\n".join(buf).strip()
            if seg:
                out.append((cur_heading, seg))
            buf.clear()

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if _is_heading(line):
            flush()
            cur_heading = line.strip()
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return out


def _window(seg: str, size: int, overlap: int) -> list[str]:
    """把一段长文本按 size 切分，相邻块重叠 overlap 字。"""
    if len(seg) <= size:
        return [seg]
    chunks: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(seg):
        piece = seg[start : start + size]
        chunks.append(piece)
        if start + size >= len(seg):
            break
        start += step
    return chunks


def chunk_document(
    content: str,
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    min_len: int = MIN_CHUNK_LEN,
) -> list[Chunk]:
    """把政策正文切成 ``list[Chunk]``（纯函数）。

    :param content: 清洗后的政策正文（``policy_docs.content``）
    :param size: 目标块长（字）
    :param overlap: 重叠字数
    :param min_len: 尾块最短字数，过短则并入上一块
    """
    content = (content or "").strip()
    if not content:
        return []

    pieces: list[tuple[str, str]] = []  # (heading, text)
    for heading, seg in _split_paragraphs(content):
        for piece in _window(seg, size, overlap):
            pieces.append((heading, piece))

    # 合并过短尾块（同 heading 时并入上一块）
    merged: list[tuple[str, str]] = []
    for heading, text in pieces:
        if merged and len(text.strip()) < min_len and merged[-1][0] == heading:
            merged[-1] = (heading, merged[-1][1] + "\n" + text)
        else:
            merged.append((heading, text))

    chunks: list[Chunk] = []
    for i, (heading, text) in enumerate(merged):
        text = text.strip()
        if not text:
            continue
        chunks.append(
            Chunk(
                seq=i,
                content=text,
                heading=heading,
                tokens=extract_keywords(f"{heading} {text}"),
            )
        )
    # 重排 seq 连续
    for i, c in enumerate(chunks):
        c.seq = i
    return chunks
