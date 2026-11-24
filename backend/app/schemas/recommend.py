"""推荐结果的数据契约（T08）。

用 Pydantic 模型定义，好处：
- **前后端契约稳定**：前端按字段名渲染，字段缺失在序列化阶段即暴露；
- **可校验**：红线字段（如 ``risk_explanation`` 不得含概率数字）可在测试中统一断言；
- **可文档化**：自动生成 OpenAPI schema（T21 前端对接）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RecommendItem(BaseModel):
    """单条推荐（院校 × 专业组）。"""

    # ---- 标识 ----
    university_code: str = Field(description="院校代码")
    university_name: str = Field(description="院校名称")
    group_code: int = Field(description="专业组代码")
    subject_type: str = Field(description="科类（物理/历史）")

    # ---- 档位（由引擎在分档后回填）----
    tier: str = Field(default="", description="档位：chong / wen / bao")
    tier_label: str = Field(default="", description="档位中文：冲 / 稳 / 保")

    # ---- 数据依据 ----
    ref_rank: int | None = Field(default=None, description="三年加权参考位次 L")
    ref_score: int | None = Field(default=None, description="最新年投档最低分（展示用）")
    latest_year: int | None = Field(default=None, description="最新有数据的年份")
    latest_rank: int | None = Field(default=None, description="最新年投档最低位次")
    plan_count: int | None = Field(default=None, description="最新年计划数")
    plan_ratio: float | None = Field(default=None, description="计划变化比（今/上年）")
    evidence: list[str] = Field(default_factory=list, description="逐年数据依据（可解释）")

    # ---- 年份完整性标注（缺年不得静默）----
    ref_years_used: list[int] = Field(default_factory=list, description="实际参与加权的年份")
    ref_years_missing: list[int] = Field(default_factory=list, description="缺失年份")
    ref_years_note: str = Field(default="", description="参考年份说明（不足时显式标注）")

    # ---- 院校元数据（排序与展示）----
    level_tags: str = Field(default="", description="985|211|双一流")
    school_province: str | None = Field(default=None, description="院校所在省份")
    school_type: str | None = Field(default=None, description="院校类型")

    # ---- 选科要求标注（待补时不得静默）----
    require_status: str = Field(default="待补", description="选科要求数据状态")
    require_note: str = Field(default="", description="再选科目待补时的提示文案")

    # ---- 计划变化标注 ----
    plan_note: str = Field(default="", description="计划缩减修正说明")


class TierGroup(BaseModel):
    """一个档位（冲/稳/保）的分组结果。"""

    tier: str = Field(description="chong / wen / bao")
    label: str = Field(description="冲 / 稳 / 保")
    interval: str = Field(default="", description="档位区间文案")
    count: int = Field(default=0, description="本档推荐数")
    items: list[RecommendItem] = Field(default_factory=list)


class RecommendResult(BaseModel):
    """完整推荐结果。"""

    province: str
    current_year: int
    subject_type: str
    electives: list[str] = Field(default_factory=list)
    score: int
    rank: int = Field(description="考生位次（由 T07 位次引擎换算）")

    ref_years_used: list[int] = Field(default_factory=list)
    tiers: list[TierGroup] = Field(default_factory=list)

    # ---- 过滤统计（可解释 / 可审计）----
    excluded: dict = Field(default_factory=dict, description="各阶段排除原因计数")
    unmatched_count: int = Field(default=0, description="有位次但不落在任何档位的候选数")

    # ---- 文本解释（规则模板拼接，非 LLM）----
    caliber_text: str = Field(description="口径说明（年份+权重）")
    explanation: str = Field(description="整体说明")
    risk_explanation: str = Field(description="风险解释（禁概率数字）")
    parent_summary: str = Field(description="家长版摘要")

    # ---- 红线：免责声明必须随结果返回 ----
    disclaimer: str = Field(
        default="本建议仅供参考，不作为最终填报承诺",
        description="免责声明（强制）",
    )

    @property
    def total_count(self) -> int:
        return sum(g.count for g in self.tiers)
