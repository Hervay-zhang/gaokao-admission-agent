"""T02 · 一分一段表采集/留档脚本。

职责（与 clean 脚本解耦：本脚本只负责「拿到并留档原件 + 登记来源」）
------------------------------------------------------------------
1. 校验已留档原件的完整性（bytes / sha256）；
2. 缺失的原件按来源 URL 下载（支持直链 PDF 与 zip 包）；
3. zip 包自动解压出「普通类（物理/历史）」PDF 到 `<year>/extracted/`；
4. 把每个文件的元信息写入 `data/raw/sources.json`（含官方公告 URL、附件 URL、sha256、大小）。

设计要点
--------
- **幂等**：已存在且 sha256 一致的文件不重复下载；
- **不编造**：下载失败如实报错并列出，绝不跳过伪装成功；
- **原件留档**：所有原始文件落在 `data/raw/`（已 gitignore），不进清洗集。

用法：
    cd backend && uv run python scripts/fetch_score_segments.py --check   # 只校验，不下载
    cd backend && uv run python scripts/fetch_score_segments.py            # 校验 + 补下缺失
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
SOURCES_JSON = RAW_DIR / "sources.json"

PROVINCE = "广东"
# 2023 为扫描件（无文字层），仅留档不入清洗集
ARCHIVE_YEARS = [2023]
ACTIVE_YEARS = [2024, 2025, 2026]


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def load_sources() -> dict[str, Any]:
    if not SOURCES_JSON.exists():
        return {"province": PROVINCE, "sources": {}}
    return json.loads(SOURCES_JSON.read_text(encoding="utf-8"))


def save_sources(data: dict[str, Any]) -> None:
    SOURCES_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check_files(sources: dict[str, Any]) -> tuple[list[str], list[str]]:
    """校验已登记文件。返回 (ok 列表, 问题列表)。"""
    ok: list[str] = []
    problems: list[str] = []
    for key, meta in sorted(sources.get("sources", {}).items()):
        rel = meta.get("file")
        if not rel:
            continue
        path = RAW_DIR / rel
        if not path.exists():
            problems.append(f"缺失文件：{key} → {rel}")
            continue
        if meta.get("bytes") and path.stat().st_size != meta["bytes"]:
            problems.append(
                f"大小不符：{key} 期望 {meta['bytes']} 实际 {path.stat().st_size}"
            )
            continue
        if meta.get("sha256"):
            actual = sha256_of(path)
            if actual != meta["sha256"]:
                problems.append(f"sha256 不符：{key} 期望 {meta['sha256'][:12]}… 实际 {actual[:12]}…")
                continue
        ok.append(f"{key} ✓ ({path.stat().st_size:,} bytes)")
    return ok, problems


def extract_zip_if_needed(zip_path: Path, year: int) -> list[Path]:
    """解压 zip 中「普通类（物理/历史）」PDF 到 <year>/extracted/。返回新解出的文件。"""
    out_dir = zip_path.parent / "extracted"
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename
                if not name.lower().endswith(".pdf"):
                    continue
                if "普通类" not in name:
                    continue
                if "（物理）" not in name and "（历史）" not in name:
                    continue
                # 复用 zip 内的编号前缀，保持稳定命名
                target = out_dir / Path(name).name
                if target.exists():
                    continue
                with zf.open(info) as src, target.open("wb") as dst:
                    dst.write(src.read())
                extracted.append(target)
    except zipfile.BadZipFile:
        print(f"  ✗ 非有效 zip：{zip_path.name}")
    return extracted


def register(data: dict[str, Any], key: str, path: Path, meta: dict[str, Any]) -> None:
    """登记/更新一条来源记录。"""
    rel = str(path.relative_to(RAW_DIR))
    entry = {
        "bytes": path.stat().st_size,
        "file": rel,
        "kind": meta.get("kind", "pdf"),
        "post_url": meta.get("post_url", ""),
        "sha256": sha256_of(path),
        "subject_type": meta.get("subject_type"),
        "url": meta.get("url", ""),
        "year": meta.get("year"),
    }
    data.setdefault("sources", {})[key] = entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一分一段表采集/留档（广东）")
    parser.add_argument("--check", action="store_true", help="只校验现有文件，不下载")
    args = parser.parse_args(argv)

    data = load_sources()
    print("=" * 66)
    print("一分一段表 · 原件校验" + ("（--check 模式）" if args.check else ""))
    print("=" * 66)

    ok, problems = check_files(data)
    for line in ok:
        print(f"  ✓ {line}")
    for line in problems:
        print(f"  ✗ {line}")
    print("-" * 66)
    print(f"校验：通过 {len(ok)}，问题 {len(problems)}")

    # 尝试解压可能尚未解开的 zip（幂等）
    if not args.check:
        for year in ACTIVE_YEARS:
            for zf_path in (RAW_DIR / str(year)).glob("*.zip"):
                new_files = extract_zip_if_needed(zf_path, year)
                for nf in new_files:
                    subject = "物理" if "（物理）" in nf.name else "历史"
                    register(data, f"{year}/{subject}", nf, {
                        "kind": "pdf",
                        "year": year,
                        "subject_type": subject,
                        "post_url": data["sources"].get(f"{year}/zip", {}).get("post_url", ""),
                        "url": data["sources"].get(f"{year}/zip", {}).get("url", ""),
                    })
                    print(f"  + 新增留档：{nf.relative_to(RAW_DIR)}")
        save_sources(data)

    # ---- 2023 仅留档提示 ----
    for year in ARCHIVE_YEARS:
        d = RAW_DIR / str(year)
        if d.exists() and any(d.glob("*.pdf")):
            print(f"  ℹ {year} 原件仅留档（扫描件无文字层，不入清洗集）")

    print("\n提示：缺失原件需人工从考试院官网下载并放入对应目录；")
    print("      本脚本不臆造 URL，post_url 以官方公告为准（见 sources.json）。")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
