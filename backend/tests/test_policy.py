"""T12 测试 —— 政策语料采集 / 清洗 / 校验（J1–J3 + 清洗回归守卫）。

对应任务卡「测试场景（必做）」：
    J1 每篇有 source_url + source_year；
    J2 无重复标题；
    J3 内容非空且长度合理。

补足（T12 开发中踩过的真实缺陷，必须锁死）：
- **换行还原守卫**：数字被逐位换行（``202\\n6``）、句中被换行切断须还原；
  编号标题（``二、报考基本条件``）须独占一行；裸标记（``（一）``/``1.``）须与下一行相接。
- **噪声清除守卫**：页头噪声行（``时间 :``/``来源 :``/``分享到：``/``文号 ：``）不得残留。
- **去重方向守卫**：同标题保留正文更长者，且被丢弃项可追溯。
- **清洗不改写**：清洗后正文不得凭空多出/少掉政策表述（用长度下限 + 关键句存在性守卫）。

设计：清洗/解析逻辑为**纯函数**，测试全部**离线**（不联网、不依赖 MySQL/Redis）。
真数据断言用 ``data/cleaned/policy_docs.csv``（若不存在则 skip，避免 CI 无数据时假红）。
"""

from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.clean_policy import (  # noqa: E402
    DEFAULT_MIN_LEN,
    _should_join,
    _strip_noise,
    clean_one,
    dedupe,
    write_csv,
)
from scripts.fetch_policy import _doc_type, _parse_year  # noqa: E402

PROJECT_ROOT = BACKEND_DIR.parent
POLICY_CSV = PROJECT_ROOT / "data" / "cleaned" / "policy_docs.csv"
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "policy"

POLICY_MIN_CONTENT_LEN = 80
POLICY_MIN_EXPECTED = 50


# ============================================================
#  清洗纯函数单测（守卫）
# ============================================================


def test_strip_noise_removes_header_lines():
    """页头噪声行必须被清除。"""
    raw = (
        "关于做好2026年普通高校招生工作的通知\n"
        "时间  :  2026-06-30 14:37:07\n"
        "来源  :  广东省教育考试院\n"
        "文号 ：\n"
        "分享到：\n"
        "各地级以上市招生委员会：\n"
        "现将有关事项通知如下。"
    )
    out = _strip_noise(raw, "关于做好2026年普通高校招生工作的通知")
    for noise in ("时间  :", "来源  :", "文号 ：", "分享到："):
        assert noise not in out
    # 标题行（首行重复）也应去掉
    assert not out.startswith("关于做好2026年普通高校招生工作的通知")
    # 正文实质内容保留
    assert "各地级以上市招生委员会" in out
    assert "现将有关事项通知如下" in out


def test_strip_noise_rejoins_digit_split_lines():
    """数字被逐位换行（202\\n6）必须还原为 2026。"""
    raw = "根据《关于做好\n202\n6\n年普通高校招生工作的通知》精神，现将\n有关事项通知如下。"
    out = _strip_noise(raw, "x")
    assert "2026年" in out
    assert "根据《关于做好2026年" in out
    assert "》，现将有关事项通知如下" in out or "》精神，现将有关事项通知如下" in out


def test_strip_noise_keeps_numbered_heading_on_own_line():
    """编号标题（如「二、报考基本条件」）必须独占一行，不得与下一行黏连。"""
    raw = "一、招生院校及专业\n（一）招生计划见附件。\n二、报考基本条件\n所有报考考生须符合规定。"
    out = _strip_noise(raw, "x")
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert "二、报考基本条件" in lines
    # 下一行必须是独立行，不得以「二、报考基本条件」开头
    assert not any(ln.startswith("二、报考基本条件所有") for ln in lines)


def test_strip_noise_joins_bare_marker():
    """裸标记（只有编号无文字，如「（一）」/「1.」）应与下一行正文相接。"""
    raw = "（一）\n2026年我省高职自主招生含4种招生类型。\n1.\n普通高中考生参加学考。"
    out = _strip_noise(raw, "x")
    assert "（一）2026年我省高职自主招生" in out
    assert "1.普通高中考生参加学考" in out


def test_should_join_direction_guard():
    """_should_join 方向守卫（这些是开发中踩过的真实缺陷）。"""
    # 下一行是编号起头 → 不接
    assert _should_join("上一句没有句号", "二、报考基本条件") is False
    assert _should_join("上一句没有句号", "1.普通高中考生") is False
    # 上一行是「有文字的编号标题」→ 不接
    assert _should_join("二、报考基本条件", "所有报考考生须符合") is False
    # 上一行是「裸标记」→ 接
    assert _should_join("（一）", "2026年我省高职自主招生") is True
    assert _should_join("1.", "普通高中考生参加学考") is True
    # 上一行以句终标点结尾 → 不接
    assert _should_join("现就有关事项通知如下：", "一、招生院校") is False
    assert _should_join("已完成投档。", "广东省2026年提前批") is False
    # 普通被切断 → 接
    assert _should_join("根据《关于做好", "2026年普通高校") is True


def test_clean_one_short_content_dropped():
    """正文过短 → 丢弃并给出原因（不静默接受）。"""
    rec = {"title": "短公告", "content": "正文太短。", "url": "u", "year": 2026}
    row, reason, clen = clean_one(rec, min_len=DEFAULT_MIN_LEN)
    assert row is None
    assert "过短" in reason
    assert clen < DEFAULT_MIN_LEN


def test_clean_one_missing_title_or_content():
    assert clean_one({"title": "", "content": "有正文" * 50}, min_len=10)[1] == "缺标题"
    assert clean_one({"title": "有标题", "content": ""}, min_len=10)[1] == "正文为空"


def test_clean_one_ok_and_hash():
    """正常清洗：产出字段齐全，doc_hash 为正文 sha256。"""
    content = "各地级以上市招生委员会：现将有关事项通知如下。" * 5
    rec = {
        "title": "关于做好2026年普通高校招生工作的通知",
        "content": "时间  :  2026-06-30\n来源  :  广东省教育考试院\n" + content,
        "url": "https://eea.gd.gov.cn/ptgk/content/post_1.html",
        "source": "广东省教育考试院",
        "province": "广东",
        "year": 2026,
        "doc_type": "通知",
        "column": "普通高考",
        "published_at": "2026-06-30 14:37",
    }
    row, reason, _ = clean_one(rec, min_len=10)
    assert row is not None and reason == ""
    assert row["url"] == rec["url"]
    assert row["year"] == 2026
    assert row["doc_hash"] == hashlib.sha256(row["content"].encode("utf-8")).hexdigest()


def test_dedupe_keeps_longer():
    """同标题去重：保留正文更长者，短者被丢弃且可追溯。"""
    rows = [
        {"title": "同一篇通知", "content_len": 100, "year": 2026, "published_at": "2026-01-01"},
        {"title": "同一篇通知", "content_len": 300, "year": 2026, "published_at": "2026-01-01"},
    ]
    kept, dropped = dedupe(rows)
    assert len(kept) == 1
    assert kept[0]["content_len"] == 300
    assert len(dropped) == 1
    assert dropped[0][0]["content_len"] == 100  # 被丢弃的是短的


# ============================================================
#  解析逻辑单测（fetch 的纯函数）
# ============================================================


@pytest.mark.parametrize(
    "title,expected",
    [
        ("关于做好2026年普通高校招生工作的通知", "通知"),
        ("广东省2026年普通高校招生志愿填报问答", "问答"),
        ("关于公布某名单的公示", "公示"),
        ("2026年志愿填报温馨提示", "提示"),
        ("广东省2026年普通高等学校招生工作规定", "办法"),
        ("我省2026年提前批正式投档", "其他"),
    ],
)
def test_doc_type(title, expected):
    assert _doc_type(title) == expected


def test_parse_year():
    assert _parse_year("2026-06-30 14:37", "x") == 2026
    assert _parse_year("", "关于做好2025年工作的通知") == 2025
    assert _parse_year("", "") is None


# ============================================================
#  真数据断言（若 CSV 缺失则 skip）
# ============================================================


def _load_clean_rows() -> list[dict[str, str]]:
    if not POLICY_CSV.exists():
        pytest.skip(f"未找到 {POLICY_CSV}（请先运行 fetch/clean 脚本）")
    with POLICY_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_j1_every_doc_has_url_and_year():
    """J1：每篇有 source_url + source_year。"""
    rows = _load_clean_rows()
    assert rows, "政策语料为空"
    for r in rows:
        assert (r.get("url") or "").strip(), f"缺 url：{r.get('title')}"
        assert (r.get("year") or "").strip(), f"缺 year：{r.get('title')}"
        assert 2000 <= int(r["year"]) <= 2100


def test_j2_no_duplicate_titles():
    """J2：无重复标题。"""
    rows = _load_clean_rows()
    titles = [(r.get("title") or "").strip() for r in rows]
    assert len(titles) == len(set(titles)), "存在重复标题"


def test_j3_content_non_empty_and_reasonable():
    """J3：内容非空且长度合理（≥ 80 字）。"""
    rows = _load_clean_rows()
    for r in rows:
        c = (r.get("content") or "").strip()
        assert c, f"内容为空：{r.get('title')}"
        assert len(c) >= POLICY_MIN_CONTENT_LEN, f"内容过短（{len(c)}）：{r.get('title')}"


def test_doc_hash_unique_and_matches_content():
    """doc_hash 唯一，且确为清洗后正文 sha256。"""
    rows = _load_clean_rows()
    hashes = [r["doc_hash"] for r in rows]
    assert len(hashes) == len(set(hashes)), "doc_hash 重复"
    for r in rows:
        expect = hashlib.sha256(r["content"].encode("utf-8")).hexdigest()
        assert r["doc_hash"] == expect, f"doc_hash 与正文不符：{r['title']}"


def test_no_residual_header_noise():
    """清洗后不得残留页头噪声。"""
    rows = _load_clean_rows()
    for r in rows:
        for noise in ("时间  :", "来源  :", "分享到：", "文号 ：", "【字体"):
            assert noise not in r["content"], f"噪声残留 {noise}：{r['title']}"


def test_no_isolated_digit_lines():
    """正文不得残留「孤立数字行」（换行还原是否彻底）。"""
    rows = _load_clean_rows()
    bad = []
    for r in rows:
        for ln in r["content"].split("\n"):
            s = ln.strip()
            if s.isdigit() and len(s) <= 4:
                bad.append((r["title"][:24], s))
    assert not bad, f"残留孤立数字行 {len(bad)} 处：{bad[:3]}"


def test_dod_at_least_50_docs():
    """DoD：50+ 篇入库。"""
    rows = _load_clean_rows()
    assert len(rows) >= POLICY_MIN_EXPECTED, f"仅 {len(rows)} 篇，未达 DoD"


def test_validate_policy_series_passes():
    """P 系列校验在真数据上全 PASS。"""
    from scripts.validate_data import (
        RESULTS,
        set_data_dir,
    )
    from scripts.validate_data import main as validate_main

    # 用真实 data/cleaned 目录
    set_data_dir(POLICY_CSV.parent)
    rc = validate_main(["--quiet"])
    p_items = [r for r in RESULTS if r[0].startswith("P")]
    assert p_items, "未执行 P 系列校验"
    failed = [r for r in p_items if r[1] == "FAIL"]
    assert not failed, f"P 系列失败：{failed}"
    assert rc == 0


# ============================================================
#  清洗产物可复现（同一 raw → 同一 CSV）
# ============================================================


def test_clean_is_deterministic(tmp_path: Path):
    """清洗确定性：同一 raw 记录两次清洗结果一致。"""
    rec = {
        "title": "关于做好2026年工作的通知",
        "content": "时间  :  2026-07-01\n来源  :  广东省教育考试院\n"
        + "各地级以上市招生委员会：现将有关事项通知如下。" * 6,
        "url": "https://eea.gd.gov.cn/ptzx/content/post_x.html",
        "source": "广东省教育考试院",
        "province": "广东",
        "year": 2026,
        "doc_type": "通知",
        "column": "普通高考",
        "published_at": "2026-07-01 10:00",
    }
    r1, _, _ = clean_one(rec, min_len=10)
    r2, _, _ = clean_one(rec, min_len=10)
    assert r1 == r2


def test_write_csv_roundtrip(tmp_path: Path):
    """write_csv 写出后可原样读回（UTF-8-SIG，字段齐全）。"""
    row = {
        "title": "标题", "url": "https://x", "source": "来源", "province": "广东",
        "year": 2026, "doc_type": "通知", "column": "普通高考", "published_at": "2026-07-01 10:00",
        "content": "正文" * 50, "content_len": 100, "doc_hash": "abc",
    }
    out = tmp_path / "policy_docs.csv"
    write_csv([row], out)
    back = list(csv.DictReader(out.open("r", encoding="utf-8-sig", newline="")))
    assert len(back) == 1
    assert back[0]["title"] == "标题"
    assert back[0]["url"] == "https://x"
