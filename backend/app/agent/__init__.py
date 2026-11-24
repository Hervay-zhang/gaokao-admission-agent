"""Agent 层（T16 起）：意图路由 + 边界围栏 + 上下文编排 + 流式对话。

模块地图（随任务卡推进填充）
---------------------------
- ``router``   T16 意图路由 + 边界围栏 Guardrails；
- ``prompt``   T18 Prompt 编排（系统角色 + 边界 + RAG 约束 + 免责 + 档案 + Context）；
- ``guard``    T18 输出后置校验（免责兜底 / 越界检测 / 疑似编造标记）。

设计原则
--------
1. **可离线**：路由/围栏不依赖真实 LLM —— 有 LLM 时用 function calling，
   无 LLM（MOCK_MODE）时用**确定性规则**兜底，两条路径结果结构完全一致；
2. **零强改**：路由只做「第一跳分类」，不擅自执行下游业务（见任务卡「易混淆」）；
3. **红线优先**：越界问题**必须**拒答，参数抽取失败**必须**回问，绝不猜测。
"""

from app.agent.guard import (  # noqa: F401
    GuardResult,
    UniversityLookup,
    post_validate,
)
from app.agent.prompt import (  # noqa: F401
    DISCLAIMER,
    FALLBACK_TEXT,
    PromptParts,
    build_system_prompt,
    wrap_response,
)
from app.agent.router import (  # noqa: F401
    ALLOWED_INTENTS,
    OUT_OF_SCOPE_REPLY,
    Intent,
    Router,
    RouteResult,
    get_router,
)

__all__ = [
    "ALLOWED_INTENTS",
    "DISCLAIMER",
    "FALLBACK_TEXT",
    "OUT_OF_SCOPE_REPLY",
    "GuardResult",
    "Intent",
    "PromptParts",
    "RouteResult",
    "Router",
    "UniversityLookup",
    "build_system_prompt",
    "get_router",
    "post_validate",
    "wrap_response",
]
