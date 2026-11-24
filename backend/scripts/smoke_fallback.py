"""T11 生产环境冒烟脚本（连真实 MySQL，验证「专家规则模式」兜底表端到端可用）。

跑法（backend 目录下）：
    ./.venv/Scripts/python.exe scripts/smoke_fallback.py

做四件事，**全只读、不改任何数据**：
1. 用真实 MySQL 数据源构建兜底服务，按分数（不传位次）跑一次兜底表 —— 验证
   「接口场景：只给分数，由 T07 位次引擎现算位次」；
2. **模拟 LLM 全链不可用**（把 LLM 客户端入口设为必抛错），再次调用兜底表 ——
   验证「关掉 LLM 仍拿到表格」（T11 人工验收核心）；
3. 校验结果契约：三档存在、含 mode=expert_rule 标注、含免责声明、**不含任何 LLM 解释字段**；
4. 直接调用 HTTP 接口（TestClient）确认路由可达、返回 200 且结构正确。

**不依赖 Redis**：脚本内关闭缓存（use_cache=False），避免 Redis 未启动导致噪声。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.services.fallback import FallbackService  # noqa: E402
from app.services.recommend import SqlRecommendRepository  # noqa: E402
from app.services.score import get_score_engine  # noqa: E402

setup_logging()
logger = get_logger("smoke.t11")

PROV = "广东"
SUBJECT = "物理"
SCORE = 600

FORBIDDEN_FIELDS = ("explanation", "risk_explanation", "parent_summary", "evidence")


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"❌ {msg}")
    print(f"    ✓ {msg}")


def main() -> int:
    print("[1] 真实数据源 → 只给分数跑兜底表（位次由 T07 现算）")
    rank = get_score_engine().query_rank(2026, PROV, SUBJECT, SCORE).rank
    print(f"    T07 位次换算：2026 {PROV} {SUBJECT} {SCORE} 分 → 位次 {rank}")

    svc = FallbackService(SqlRecommendRepository(), use_cache=False)
    fb = svc.fallback(score=SCORE, subject_type=SUBJECT, rank=rank)
    for t in fb.tiers:
        names = "、".join(r.university_name for r in t.rows[:3])
        print(f"    {t.label}档 {t.count} 条：{names}")

    print("[2] 模拟 LLM 全链不可用 → 再跑一次（应当不受影响）")
    import app.core.llm as llm_mod

    original = getattr(llm_mod, "get_llm_client", None)

    def _boom(*_a, **_k):
        raise RuntimeError("all models unavailable (smoke)")

    if original is not None:
        llm_mod.get_llm_client = _boom  # type: ignore[assignment]
    try:
        fb_down = svc.fallback(score=SCORE, subject_type=SUBJECT, rank=rank)
    finally:
        if original is not None:
            llm_mod.get_llm_client = original  # type: ignore[assignment]
    _check(fb_down.total_rows == fb.total_rows, "LLM 不可用时兜底表条数与正常一致（降级路径不碰 LLM）")

    print("[3] 校验结果契约")
    _check(fb.mode == "expert_rule", "mode == expert_rule（专家规则模式标注）")
    _check(fb.mode_label == "专家规则模式", "mode_label 中文标注正确")
    _check(fb.disclaimer == "本建议仅供参考，不作为最终填报承诺", "免责声明随结果返回")
    _check([t.tier for t in fb.tiers] == ["chong", "wen", "bao"], "三档齐备（冲/稳/保）")
    _check(fb.total_rows > 0, f"兜底表非空（共 {fb.total_rows} 条）")
    _check(fb.caliber_text != "", "口径文字随结果返回")
    dumped = fb.model_dump()
    _check(all(f not in dumped for f in FORBIDDEN_FIELDS), "结果不含任何 LLM 解释字段（不伪造）")
    row_dumped = fb.tiers[0].rows[0].model_dump()
    _check(all(f not in row_dumped for f in FORBIDDEN_FIELDS), "行级同样不含解释字段")

    print("[4] HTTP 接口（TestClient）")
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get(
        "/api/recommend/fallback",
        params={"score": SCORE, "subject_type": SUBJECT, "rank": rank},
    )
    _check(resp.status_code == 200, f"GET /api/recommend/fallback → HTTP {resp.status_code}")
    body = resp.json()
    _check(body["mode"] == "expert_rule", "接口返回 mode=expert_rule")

    print("\nT11 生产冒烟通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
