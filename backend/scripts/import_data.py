"""T04 · 清洗数据 → MySQL 幂等导入（T12 扩展：政策语料）。

职责
----
把 T02/T03 产出的三张 CSV 导入 MySQL，**事务内 DELETE + INSERT，可重复执行**：

| CSV | 目标表 | 期望行数 |
|-----|--------|----------|
| `data/cleaned/score_segments.csv` | `score_segments` | 3503 |
| `data/cleaned/universities.csv` | `universities` | 1095 |
| `data/cleaned/admission_lines.csv` | `admission_lines` | 15868 |

**T12 扩展**：另导入政策语料

| CSV | 目标表 | 期望行数 |
|-----|--------|----------|
| `data/cleaned/policy_docs.csv` | `policy_docs` | ≥50（不写死，按 CSV 实际行数） |

另外写入一行 `provinces`（广东 3+1+2）与一行 `data_version`（当前有效版本），保证下游可用。

幂等设计
--------
1. 每个表：`DELETE FROM <table>` → 批量 `INSERT`，同一事务提交；
2. 导入前后行数可核对（`--check`）；
3. 重复执行结果一致（表内数据相同）；
4. `policy_docs` 按 `doc_hash` 幂等（**不 DELETE 全表**：若表中已有其他来源的政策，
   只增量 upsert 本次 CSV 的行，避免误删历史语料）；
5. **不触碰** `sessions` / `user_profiles` 等业务运行时表。

红线
----
- 分数/位次/计划数一律整数（禁 FLOAT）；
- 选科要求按 CSV 原样导入（JSON 文本），**不臆造**；
- 政策正文来自清洗产物，**不改写、不补造**；`url`/`year` 缺失显式留空（NULL）；
- 账密走 `.env`，脚本不硬编码。

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/import_data.py            # 全量导入
    cd backend && ./.venv/Scripts/python.exe scripts/import_data.py --check    # 只核对行数，不改库
    cd backend && ./.venv/Scripts/python.exe scripts/import_data.py --table policy_docs
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.db.models import (  # noqa: E402
    AdmissionLine,
    DataVersion,
    PolicyDoc,
    Province,
    ScoreSegment,
    University,
)
from app.db.session import SessionLocal  # noqa: E402

PROJECT_ROOT = BACKEND_DIR.parent
CLEANED = PROJECT_ROOT / "data" / "cleaned"

SOURCES_JSON = PROJECT_ROOT / "data" / "raw" / "sources.json"

# 三张表的期望行数（与 T02/T03 口径一致，用于 --check 核对）
EXPECTED = {
    "score_segments": 3503,
    "universities": 1095,
    "admission_lines": 15868,
}

# 政策语料期望下限（任务卡 DoD：50+ 篇）；实际行数以 CSV 为准
POLICY_MIN_EXPECTED = 50

# 广东 3+1+2 省份行（T04 首次写入；后续可在管理后台维护）
PROVINCE_SEED = {
    "code": "guangdong",
    "name": "广东",
    "mode": "3+1+2",
    "primary_subjects": "物理,历史",
    "secondary_pool": "化学,生物,政治,地理",
    "volunteer_capacity": 45,
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"缺少清洗产物：{path}（请先运行 T02/T03 清洗脚本）")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _to_int(v: str | None) -> int | None:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v))  # 容忍 "20.0" 之类
    except ValueError:
        return None


# ---------------------------------------------------------------------------
#  各表导入（返回写入行数）
# ---------------------------------------------------------------------------


def import_score_segments(db: Session) -> int:
    rows = _read_csv(CLEANED / "score_segments.csv")
    payload = [
        {
            "province": r["province"],
            "year": int(r["year"]),
            "subject_type": r["subject_type"],
            "score": int(r["score"]),
            "rank": int(r["rank"]),
            "source": None,
        }
        for r in rows
    ]
    db.execute(delete(ScoreSegment))
    _bulk_insert(db, ScoreSegment, payload)
    return len(payload)


def import_universities(db: Session) -> int:
    rows = _read_csv(CLEANED / "universities.csv")
    payload = [
        {
            "code": str(r["university_code"]).strip(),
            "name": r["university_name"],
            "base_name": r.get("base_name", ""),
            "level": r.get("level") or "本科",
            "province": (r.get("province") or "").strip() or None,
            "school_type": (r.get("school_type") or "").strip() or None,
            "tags": r.get("tags", "") or "",
            "name_variants": r.get("name_variants", "") or "",
            "meta_status": r.get("meta_status") or "待补",
        }
        for r in rows
    ]
    db.execute(delete(University))
    _bulk_insert(db, University, payload)
    return len(payload)


def import_admission_lines(db: Session) -> int:
    rows = _read_csv(CLEANED / "admission_lines.csv")
    payload = []
    for r in rows:
        # 选科要求原样保留；若空白则补「待补」结构（不臆造）
        req = (r.get("subject_requirement") or "").strip()
        if not req:
            req = json.dumps(
                {
                    "mode": "3+1+2",
                    "first": [r["subject_type"]],
                    "second": [],
                    "pattern": "none",
                    "status": "待补",
                    "raw": "",
                },
                ensure_ascii=False,
            )
        payload.append(
            {
                "province": r["province"],
                "year": int(r["year"]),
                "subject_type": r["subject_type"],
                "university_code": str(r["university_code"]).strip(),
                "university_name": r["university_name"],
                "group_code": int(r["group_code"]),
                "plan_count": _to_int(r.get("plan_count")),
                "admitted_count": _to_int(r.get("admitted_count")),
                "min_score": _to_int(r.get("min_score")),
                "min_rank": _to_int(r.get("min_rank")),
                "subject_requirement": req,
            }
        )
    db.execute(delete(AdmissionLine))
    _bulk_insert(db, AdmissionLine, payload)
    return len(payload)


def _parse_published_at(v: str | None) -> datetime | None:
    """解析 ``YYYY-MM-DD HH:MM(:SS)`` / ``YYYY-MM-DD``；无法解析则返回 None（不猜测）。"""
    s = (v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def import_policy_docs(db: Session) -> int:
    """导入政策语料（按 ``doc_hash`` 幂等 upsert，不整表 DELETE）。

    之所以不 DELETE 全表：政策语料可能来自多个来源（考试院/阳光高考…），
    本次 CSV 只是其中一个子集；整表清空会误删历史语料。
    采用「先删同 doc_hash 再插」实现幂等增量。
    """
    rows = _read_csv(CLEANED / "policy_docs.csv")
    payload: list[dict] = []
    skipped: list[str] = []
    for r in rows:
        doc_hash = (r.get("doc_hash") or "").strip()
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        if not (doc_hash and title and content):
            skipped.append(f"{title[:30]}（缺 hash/title/content）")
            continue
        payload.append(
            {
                "title": title[:255],
                "url": (r.get("url") or "").strip() or None,
                "source": (r.get("source") or "").strip() or None,
                "province": (r.get("province") or "").strip() or None,
                "year": _to_int(r.get("year")),
                "doc_type": (r.get("doc_type") or "").strip() or None,
                "content": content,
                "doc_hash": doc_hash[:64],
                "published_at": _parse_published_at(r.get("published_at")),
            }
        )
    if skipped:
        print(f"  ⚠️ 跳过 {len(skipped)} 行（关键字段缺失）：{skipped[:3]}")

    # 幂等：删除本次将写入的 doc_hash（分批，避免 IN 过长）
    hashes = [p["doc_hash"] for p in payload]
    for i in range(0, len(hashes), 500):
        chunk = hashes[i : i + 500]
        db.execute(delete(PolicyDoc).where(PolicyDoc.doc_hash.in_(chunk)))

    _bulk_insert(db, PolicyDoc, payload)
    return len(payload)


def import_province_and_version(db: Session) -> tuple[int, int]:
    """写入省份种子 + 当前数据版本（幂等：先删同键再插）。"""
    # 省份：按 code 幂等 upsert
    db.execute(delete(Province).where(Province.code == PROVINCE_SEED["code"]))
    db.execute(insert(Province).values(**PROVINCE_SEED))

    # 数据版本：只保留一行有效
    db.execute(delete(DataVersion))
    db.execute(
        insert(DataVersion).values(
            current_year=settings.current_year,
            ref_years=settings.ref_years,
            year_weights=settings.year_weights,
            is_active=True,
            note="T04 初始导入（配置来自 .env）",
        )
    )
    return 1, 1


def _bulk_insert(db: Session, model: type, payload: list[dict]) -> None:
    """分批插入（避免单条 SQL 过大 / MySQL max_allowed_packet 限制）。"""
    if not payload:
        return
    batch = 1000
    for i in range(0, len(payload), batch):
        db.execute(insert(model), payload[i : i + batch])


# ---------------------------------------------------------------------------
#  核对
# ---------------------------------------------------------------------------


def check_counts(db: Session) -> dict[str, tuple[int, int]]:
    """返回 {表名: (实际, 期望)}。"""
    out: dict[str, tuple[int, int]] = {}
    for model, name in (
        (ScoreSegment, "score_segments"),
        (University, "universities"),
        (AdmissionLine, "admission_lines"),
    ):
        actual = db.execute(select(func.count()).select_from(model)).scalar_one()
        out[name] = (actual, EXPECTED[name])
    # policy_docs：期望值取 CSV 实际行数（政策语料篇数不写死）
    csv_path = CLEANED / "policy_docs.csv"
    expected_policy = len(_read_csv(csv_path)) if csv_path.exists() else 0
    actual_policy = db.execute(select(func.count()).select_from(PolicyDoc)).scalar_one()
    out["policy_docs"] = (actual_policy, expected_policy)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清洗数据 → MySQL 幂等导入（T04）")
    parser.add_argument("--check", action="store_true", help="只核对行数，不写库")
    parser.add_argument(
        "--table",
        choices=["score_segments", "universities", "admission_lines", "policy_docs", "all"],
        default="all",
        help="只导入指定表",
    )
    args = parser.parse_args(argv)

    db: Session = SessionLocal()
    try:
        if args.check:
            print("=" * 66)
            print("数据行数核对（--check）")
            print("=" * 66)
            counts = check_counts(db)
            ok = True
            for name, (actual, expected) in counts.items():
                mark = "✅" if actual == expected else "❌"
                if actual != expected:
                    ok = False
                print(f"{mark} {name:<18} 实际 {actual:<8} 期望 {expected}")
            print("-" * 66)
            print("通过" if ok else "存在差异，请重新导入")
            return 0 if ok else 1

        print("=" * 66)
        print(f"导入清洗数据 → {settings.database_url.split('@')[-1]}")
        print("=" * 66)

        targets = [args.table] if args.table != "all" else [
            "score_segments", "universities", "admission_lines", "policy_docs"
        ]
        results: dict[str, int] = {}
        for t in targets:
            if t == "score_segments":
                results[t] = import_score_segments(db)
            elif t == "universities":
                results[t] = import_universities(db)
            elif t == "admission_lines":
                results[t] = import_admission_lines(db)
            elif t == "policy_docs":
                results[t] = import_policy_docs(db)

        if args.table == "all":
            p, v = import_province_and_version(db)
            print(f"  · provinces 种子 {p} 行、data_version {v} 行")

        db.commit()

        # 政策文档删除重插会级联清空 policy_chunks；导入后立即补齐 RAG 切片，
        # 避免数据导入悄悄破坏政策问答链路。
        if "policy_docs" in targets:
            try:
                from app.rag.index import build_index, chunk_stats

                stats = build_index(db, only_missing=True)
                after = chunk_stats(db)
                print(f"  · RAG 切片补齐 {stats['chunks']} 块（库内合计 {after['chunks']} 块）")
            except ImportError as exc:  # rag 可选依赖未安装时不阻断基础数据导入
                print(f"  ⚠️ RAG 索引未构建（缺少可选依赖）：{exc}")

        for name, n in results.items():
            if name == "policy_docs":
                mark = "✅" if n >= POLICY_MIN_EXPECTED else "⚠️"
                print(f"{mark} {name:<18} 导入 {n} 行（DoD ≥{POLICY_MIN_EXPECTED}）")
            else:
                mark = "✅" if n == EXPECTED.get(name, n) else "⚠️"
                print(f"{mark} {name:<18} 导入 {n} 行（期望 {EXPECTED.get(name, '-')}）")

        # 提交后核对
        counts = check_counts(db)
        all_ok = all(a == e for a, e in counts.values())
        print("-" * 66)
        print("✅ 导入完成，行数全部一致" if all_ok else "⚠️ 提交后行数存在差异")
        return 0 if all_ok else 1
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"❌ 导入失败并已回滚：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    # Windows 本地终端默认可能是 GBK；直接运行脚本时固定 UTF-8，
    # 避免带 emoji / 中文状态行因编码失败而中断导入。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
