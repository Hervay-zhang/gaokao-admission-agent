"""降级兜底结果的数据契约（T11）。

**与正常推荐报告（``schemas/recommend.py``）的区别**（红线，勿混淆）：

- 正常报告：含 ``explanation`` / ``risk_explanation`` / ``parent_summary`` 等**自然语言解释**，
  由 LLM（或规则模板）生成，用于流式对话场景；
- 兜底表（本模块）：**只有结构化精简字段**（院校 / 层次 / 往年位次 / 档位），
  **没有解释字段** —— 因为降级时 **LLM 不可用**，**绝不伪造解释**。

因此本契约刻意**不复用** ``RecommendResult``，而是独立的 ``FallbackResult``，
避免前端误把兜底表当完整报告渲染，也避免实现者「顺手」把解释字段填成假文本。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 降级模式标识（前端据此展示「AI 线路拥挤，已切换至专家规则模式」提示）
MODE_EXPERT_RULE = "expert_rule"


class FallbackRow(BaseModel):
    """兜底表单行（院校 × 专业组），**仅限于可在无 LLM 情况下直接给出的结构化字段**。"""

    university_code: str = Field(description="院校代码")
    university_name: str = Field(description="院校名称")
    group_code: int = Field(description="专业组代码")
    subject_type: str = Field(description="科类（物理/历史）")

    # ---- 档位 ----
    tier: str = Field(description="档位：chong / wen / bao")
    tier_label: str = Field(description="档位中文：冲 / 稳 / 保")

    # ---- 数据依据（精简：只保留展示必需的字段）----
    ref_rank: int | None = Field(default=None, description="三年加权参考位次（展示用）")
    latest_year: int | None = Field(default=None, description="最新有数据的年份")
    latest_rank: int | None = Field(default=None, description="最新年投档最低位次")
    latest_score: int | None = Field(default=None, description="最新年投档最低分（展示用）")

    # ---- 院校层次（前端列表用标签展示）----
    level_tags: str = Field(default="", description="985|211|双一流")
    school_province: str | None = Field(default=None, description="院校所在省份")

    # ---- 缺数据显式标注（红线：降级不得放宽「无数据标注」）----
    ref_years_note: str = Field(default="", description="参考年份说明（缺年时显式标注）")
    require_note: str = Field(default="", description="再选科目待补时的提示文案")


class FallbackTier(BaseModel):
    """兜底表的一个档位分组。"""

    tier: str = Field(description="chong / wen / bao")
    label: str = Field(description="冲 / 稳 / 保")
    count: int = Field(default=0, description="本档条目数")
    rows: list[FallbackRow] = Field(default_factory=list)


class FallbackResult(BaseModel):
    """降级兜底结果（「专家规则模式」）。

    **无解释字段**：不提供 explanation / risk_explanation / parent_summary —— 降级时无 LLM，
    禁止伪造解释。前端只展示结构化表格 + 模式提示 + 免责声明。
    """

    # ---- 降级模式标识（前端据此展示提示条）----
    mode: str = Field(default=MODE_EXPERT_RULE, description="固定为 expert_rule（专家规则模式）")
    mode_label: str = Field(default="专家规则模式", description="模式中文名")
    mode_note: str = Field(
        default="AI 线路拥挤，已为您切换至专家规则模式，以下为基于历史投档数据的结构化结果。",
        description="降级原因与已做动作的说明（不暴露内部错误细节）",
    )

    # ---- 查询上下文（回显，便于前端确认口径）----
    province: str
    current_year: int
    subject_type: str
    score: int
    rank: int = Field(description="考生位次（由 T07 位次引擎换算）")

    ref_years_used: list[int] = Field(default_factory=list)
    tiers: list[FallbackTier] = Field(default_factory=list)

    # ---- 口径说明（与推荐引擎同源，防口径漂移）----
    caliber_text: str = Field(default="", description="口径说明（年份+权重）")

    # ---- 缓存与降级元信息（可观测）----
    generated_at: str = Field(default="", description="生成时间（ISO 字符串）")
    cache_hit: bool = Field(default=False, description="本次是否命中缓存")

    # ---- 红线：免责声明必须随结果返回 ----
    disclaimer: str = Field(
        default="本建议仅供参考，不作为最终填报承诺",
        description="免责声明（强制）",
    )

    @property
    def total_rows(self) -> int:
        return sum(t.count for t in self.tiers)
