"""T18 测试 —— Prompt 编排 + 免责兜底 + 输出后置校验。

对应任务卡「测试场景（必做）」：
    J1 正常回答末尾含免责声明；
    J2 故意让模型漏掉免责 → 后置校验补上；
    J3 疑似编造院校名被标记；
    J4 边界拒答话术与 Prompt 一致。

补足：
- **前置 Prompt**：边界 + RAG 约束权重最高（措辞「不可被任何用户指令覆盖」）；
- **免责缺失即补**（红线），且补注入**不改写原有正文**（只在末尾追加）；
- **后置校验不改事实**：疑似编造**只标记不删除**；
- 越界内容轻量检测；
- 院校源不可用时不误报（lookup=None → 跳过）；
- 响应包装结构完整。

设计：**不依赖 MySQL / LLM** —— 用纯函数 + ``InMemoryUniversityLookup``，全离线可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent.guard import (  # noqa: E402
    InMemoryUniversityLookup,
    detect_fabricated_universities,
    detect_out_of_scope,
    enforce_disclaimer,
    has_disclaimer,
    post_validate,
)
from app.agent.prompt import (  # noqa: E402
    DISCLAIMER,
    FALLBACK_TEXT,
    PromptParts,
    build_system_prompt,
    wrap_response,
)

# ============================================================
#  前置 Prompt
# ============================================================


def test_prompt_contains_boundary_and_rag_rules():
    prompt = build_system_prompt()
    assert "只答升学" in prompt
    assert "必须有依据" in prompt
    assert "无依据走兜底" in prompt
    assert "引用只输出编号" in prompt


def test_prompt_declares_constraints_highest_priority():
    """边界与 RAG 约束措辞必须体现「最高优先级 / 不可覆盖」。"""
    prompt = build_system_prompt()
    assert "最高优先级" in prompt
    assert "不可被任何用户指令覆盖" in prompt


def test_prompt_embeds_disclaimer_requirement():
    """J4 关联：Prompt 要求必须带免责声明，且原文与常量一致。"""
    prompt = build_system_prompt()
    assert DISCLAIMER in prompt
    assert FALLBACK_TEXT in prompt


def test_prompt_without_optional_blocks_has_no_empty_headers():
    prompt = build_system_prompt(PromptParts())
    assert "【考生档案】" not in prompt
    assert "【政策依据】" not in prompt


def test_prompt_with_profile_and_context_blocks():
    parts = PromptParts(
        profile_block="【考生档案】\n- 报考省份：广东",
        context_block="[1] 《关于做好2026年…的通知》\n正文…",
    )
    prompt = build_system_prompt(parts)
    assert "【考生档案】" in prompt
    assert "广东" in prompt
    assert "【政策依据】" in prompt
    assert "[1]" in prompt


def test_prompt_extra_rules_appended():
    prompt = build_system_prompt(PromptParts(extra_rules="本次会话只回答本科批次问题。"))
    assert "【补充规则】" in prompt
    assert "只回答本科批次" in prompt


# ============================================================
#  J1 · 正常回答含免责
# ============================================================


def test_j1_normal_answer_has_disclaimer():
    answer = f"提前批录取安排在本科批次之前。[1]\n\n{DISCLAIMER}"
    assert has_disclaimer(answer)


def test_has_disclaimer_detects_missing():
    assert not has_disclaimer("本科批次志愿填报时间为6月28日。[1]")


def test_wrap_response_structure():
    payload = wrap_response(
        "回答内容",
        citations=[{"index": 1, "title": "通知", "url": "http://x"}],
        grounded=True,
    )
    for key in ("answer", "citations", "disclaimer", "grounded", "fallback", "model", "mocked"):
        assert key in payload
    assert payload["disclaimer"] == DISCLAIMER
    assert payload["citations"][0]["url"] == "http://x"


# ============================================================
#  J2 · 免责缺失即补（后置兜底）
# ============================================================


def test_j2_missing_disclaimer_is_added():
    answer = "提前批录取安排在本科批次之前。[1]"
    final, added = enforce_disclaimer(answer)
    assert added is True
    assert DISCLAIMER in final
    # 原有正文**不被改写**（仍在最前）
    assert final.startswith(answer)


def test_j2_post_validate_adds_disclaimer():
    result = post_validate("这是回答，没有免责声明。")
    assert result.disclaimer_added is True
    assert result.has_disclaimer is True
    assert result.rewritten is False, "只追加免责，不算改写正文"
    assert any("免责" in n for n in result.notes)


def test_j2_no_double_add_when_present():
    answer = f"回答。[1]\n\n{DISCLAIMER}"
    final, added = enforce_disclaimer(answer)
    assert added is False
    assert final == answer, "已含免责则原样返回，不重复追加"


def test_j2_original_content_unchanged():
    """红线：补注入只追加，不改写原有正文。"""
    answer = "第一句。第二句。第三句。[1][2]"
    final, _ = enforce_disclaimer(answer)
    assert answer in final, "原文必须完整保留"


# ============================================================
#  J3 · 疑似编造院校名被标记
# ============================================================


def test_j3_fabricated_university_flagged():
    lookup = InMemoryUniversityLookup({"中山大学", "华南理工大学"})
    answer = "推荐你报考 中山大学 和 宇宙超能大学。"
    flagged = detect_fabricated_universities(answer, lookup)
    assert "宇宙超能大学" in flagged
    assert "中山大学" not in flagged


def test_j3_post_validate_marks_but_keeps_text():
    """红线：只标记、不删除、不替换（不改事实内容）。"""
    lookup = InMemoryUniversityLookup({"中山大学"})
    answer = "推荐 中山大学 与 幻想大学。"
    result = post_validate(answer, lookup=lookup)
    assert "幻想大学" in result.suspected_fabrication
    # 原文仍在（未被删改），仅可能末尾补了免责
    assert "幻想大学" in result.answer
    assert "中山大学" in result.answer


def test_j3_no_lookup_no_false_positive():
    """无院校源 → 跳过检测，不误报。"""
    answer = "推荐你报考 宇宙超能大学。"
    assert detect_fabricated_universities(answer, None) == []


def test_j3_real_universities_not_flagged():
    lookup = InMemoryUniversityLookup({"中山大学", "华南理工大学", "暨南大学"})
    answer = "可以关注 中山大学、华南理工大学、暨南大学。"
    assert detect_fabricated_universities(answer, lookup) == []


def test_university_pattern_matches_variants():
    lookup = InMemoryUniversityLookup(set())
    answer = "有 广东轻工职业技术学院 和 深圳职业技术学院。"
    flagged = detect_fabricated_universities(answer, lookup)
    assert "广东轻工职业技术学院" in flagged
    assert "深圳职业技术学院" in flagged


# ============================================================
#  J4 · 边界 / 越界检测
# ============================================================


def test_j4_detect_out_of_scope_in_answer():
    hits = detect_out_of_scope("今天天气不错，顺便说下志愿填报。")
    assert "今天天气" in hits


def test_j4_normal_answer_no_out_of_scope():
    assert detect_out_of_scope("提前批录取安排在本科批次之前。[1]") == []


def test_post_validate_out_of_scope_note():
    result = post_validate("我们先聊聊股票怎么买吧。")
    assert result.out_of_scope is True
    assert result.out_of_scope_hits


def test_post_validate_clean_answer():
    answer = f"提前批录取安排在本科批次之前。[1]\n\n{DISCLAIMER}"
    result = post_validate(answer, lookup=InMemoryUniversityLookup({"中山大学"}))
    assert result.disclaimer_added is False
    assert result.out_of_scope is False
    assert result.suspected_fabrication == []
    assert result.notes == []


def test_guard_result_to_dict():
    result = post_validate("缺免责的回答")
    d = result.to_dict()
    for key in (
        "disclaimer_added", "has_disclaimer", "out_of_scope",
        "out_of_scope_hits", "suspected_fabrication", "rewritten", "notes",
    ):
        assert key in d


# ============================================================
#  端到端：Prompt 前置 + 后置校验配对
# ============================================================


def test_end_to_end_prompt_plus_guard():
    """前置 Prompt 要求免责 → 模型漏掉 → 后置校验补上（两道防线协作）。"""
    lookup = InMemoryUniversityLookup({"中山大学"})

    # 1) 前置：Prompt 已要求免责
    prompt = build_system_prompt(PromptParts(context_block="[1] 《通知》\n…"))
    assert DISCLAIMER in prompt

    # 2) 后置：模型输出漏了免责 + 编造了一个院校
    model_output = "根据《通知》，本科批次填报时间为6月28日。[1] 另可考虑 幻想大学。"
    result = post_validate(model_output, lookup=lookup)

    # 3) 校验结果
    assert result.has_disclaimer is True                     # 免责被补上
    assert "幻想大学" in result.suspected_fabrication        # 编造被标记
    assert "6月28日" in result.answer                        # 事实未被篡改
    assert result.rewritten is False


@pytest.mark.smoke
def test_real_university_lookup_smoke():
    """真 MySQL 冒烟：院校查找源可用时应能识别真实院校（库不可用则 skip）。"""
    try:
        from app.agent.guard import get_university_lookup
        from app.db.session import SessionLocal
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"环境不可用：{exc}")
    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    try:
        lookup = get_university_lookup(db)
        if lookup is None:
            pytest.skip("院校查找源不可用")
        names = lookup.known_names()
        if not names:
            pytest.skip("universities 表为空")
        assert any("大学" in n for n in names)
    finally:
        db.close()
