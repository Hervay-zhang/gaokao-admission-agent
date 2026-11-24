"""T27 · 案例中心服务（读取脱敏案例：真实数据生成，禁编造）。

数据来源
--------
`data/cases/index.json`（索引）+ `data/cases/case_XX.md`（正文），由
`scripts/gen_cases.py` 从**回测样本 + 真实投档数据**生成。

红线
----
- 案例正文**只做机械解析**（按 `##` 切分章节），不改写、不补写；
- 目录可注入（测试用临时目录），生产读 settings.cases_dir 或默认项目路径；
- `slug` 严格校验 `case_<数字>`（**防目录穿越**）；
- 接口**不返回**明文分数 / 位次 / 姓名 / 手机号（案例本身只含分数段与位次段）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

_SLUG_RE = re.compile(r"^case_\d{2,3}$")
# 敏感字段黑名单：索引里出现即视为泄露（生成端不应产生，此处兜底剔除）
_FORBIDDEN_KEYS = {"score", "rank", "name", "phone", "mobile", "id_card", "email", "real_name"}


def default_cases_dir() -> Path:
    """默认案例目录：项目根 /data/cases。"""
    if settings.cases_dir:
        return Path(settings.cases_dir)
    # app/services/cases.py → backend/app/services → parents[3] = 项目根
    return Path(__file__).resolve().parents[3] / "data" / "cases"


class CaseService:
    """案例读取服务（只读）。"""

    def __init__(self, cases_dir: Path | None = None) -> None:
        self.dir = Path(cases_dir) if cases_dir else default_cases_dir()

    # ---- 内部 ----

    def _load_index(self) -> dict[str, Any]:
        path = self.dir / "index.json"
        if not path.exists():
            logger.warning("案例索引不存在：%s（请先执行 scripts/gen_cases.py）", path)
            return {"count": 0, "cases": [], "filters": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:  # noqa: BLE001
            logger.error("案例索引解析失败：%r", exc)
            return {"count": 0, "cases": [], "filters": {}}
        cases = []
        for c in data.get("cases", []):
            cases.append(self._sanitize_meta(c))
        data["cases"] = cases
        data["count"] = len(cases)
        return data

    @staticmethod
    def _sanitize_meta(meta: dict[str, Any]) -> dict[str, Any]:
        """兜底：剔除任何敏感字段（红线的结构性保证）。"""
        return {k: v for k, v in meta.items() if k.lower() not in _FORBIDDEN_KEYS}

    # ---- 对外 ----

    def list_cases(
        self,
        *,
        subject_type: str | None = None,
        score_band: str | None = None,
        path_type: str | None = None,
        narrative_tier: str | None = None,
    ) -> dict[str, Any]:
        """案例列表（支持筛选）。返回 `{total, filters, cases}`。"""
        index = self._load_index()
        cases = index.get("cases", [])

        def keep(c: dict[str, Any]) -> bool:
            if subject_type and c.get("subject_type") != subject_type:
                return False
            if score_band and c.get("score_band") != score_band:
                return False
            if path_type and c.get("path_type") != path_type:
                return False
            if narrative_tier and c.get("narrative_tier") != narrative_tier:
                return False
            return True

        filtered = [c for c in cases if keep(c)]
        return {
            "total": len(filtered),
            "filters": index.get("filters", {}),
            "generated_at": index.get("generated_at", ""),
            "disclaimer": "案例由公开投档数据自动生成并脱敏，仅供方法参考，不构成填报建议。",
            "cases": filtered,
        }

    def get_case(self, slug: str) -> dict[str, Any]:
        """案例详情（元数据 + 按章节解析的正文）。"""
        if not _SLUG_RE.match(slug or ""):
            raise NotFoundError("案例不存在", detail={"code": "CASE_NOT_FOUND"})
        index = self._load_index()
        meta = next((c for c in index.get("cases", []) if c.get("slug") == slug), None)
        if meta is None:
            raise NotFoundError("案例不存在", detail={"code": "CASE_NOT_FOUND"})

        md_path = self.dir / f"{slug}.md"
        if not md_path.exists():
            raise NotFoundError("案例正文缺失", detail={"code": "CASE_NOT_FOUND"})
        return {
            "meta": meta,
            "sections": parse_sections(md_path.read_text(encoding="utf-8")),
            "disclaimer": "案例由公开投档数据自动生成并脱敏，仅供方法参考，不构成填报建议。",
        }


def parse_sections(md: str) -> list[dict[str, Any]]:
    """把案例 Markdown 机械切分为章节（不改写内容）。

    - `# ` 一级标题 → `title`；
    - `## ` 二级标题 → 章节；章节体为原始行（含表格与引用）。
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in md.splitlines():
        if line.startswith("## "):
            current = {"heading": line[3:].strip(), "lines": []}
            sections.append(current)
        elif line.startswith("# "):
            continue  # 文档标题由 meta.title 提供
        elif current is not None:
            current["lines"].append(line)
    # 收敛尾部空行
    for s in sections:
        while s["lines"] and not s["lines"][-1].strip():
            s["lines"].pop()
    return sections


# ============================================================
#  单例
# ============================================================

_service: CaseService | None = None


def get_case_service() -> CaseService:
    global _service
    if _service is None:
        _service = CaseService()
    return _service


def set_case_service(service: CaseService | None) -> None:
    global _service
    _service = service
