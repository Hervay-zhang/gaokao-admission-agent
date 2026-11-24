"""全局配置模块（唯一配置入口）。

设计要点：
1. **零硬编码**：所有可变参数（数据库、Redis、LLM 模型、年份、限流阈值）一律从 `.env` 读取。
2. **多模型可切换**：LLM 支持一条「主模型 + 备选降级链」，防止单一模型停服导致全线不可用。
3. **缺 .env 明确报错**：关键配置缺失时给出可读的中文提示，而不是抛出难以定位的异常。

所有模块通过 `from app.core.config import settings` 使用，**禁止**在业务代码里二次读取 os.environ。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（backend 的上一级），.env 放在项目根
BASE_DIR = Path(__file__).resolve().parents[3]
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    """应用配置。字段名大小写不敏感，对应 .env 中的大写键。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 允许 .env 中有未声明的键，不报错
    )

    # ==================== 基础 ====================
    app_name: str = "gaokao-volunteer-assistant"
    app_version: str = "2.0.0"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    # 演示省份（广东，新高考 3+1+2）
    province: str = "guangdong"
    # 「当年」——推荐只用 <= CURRENT_YEAR 的数据
    current_year: int = 2026
    # 参考年份（冲稳保加权用），逗号分隔，**从旧到新**（口径：data_caliber §1.1）
    ref_years: str = "2024,2025,2026"
    # 年份权重（与 ref_years **一一对应、同序**，从旧到新），和应为 1；越近权重越高
    year_weights: str = "0.2,0.3,0.5"

    # ==================== 「冲稳保」推荐（T08，经验初值，T10 回测调优）====================
    # 分档系数（以考生位次 R 为基准，比较院校参考位次 L）：
    #   冲 L <= chong_ratio * R ；稳 chong_ratio*R < L <= wen_ratio*R ；保 wen_ratio*R < L <= bao_ratio*R
    # 注意：系数乘的是 R（考生位次），L 越接近 1 越「稳」；越小越「冲」，越大越「保」
    chong_ratio: float = 0.85
    wen_ratio: float = 1.2
    bao_ratio: float = 2.0
    # 极高位次（如全省第 1 名）按原始倍数会把冲档区间压缩为 0；
    # rank < 500 时使用最小位次窗口 500，保证三档仍有可比院校。
    tier_min_rank_basis: int = 500
    # 每档最多推荐数（任务卡：各 ≤10）
    tier_limit: int = 10
    # 计划变化因子阈值：计划明显缩减（ratio < shrink_threshold）时做保守化修正
    plan_shrink_threshold: float = 0.9
    # 计划缩减时的位次上移系数（保守化：把院校参考位次 L 调小 = 更靠前 = 更「冲」，降低乐观预期）
    plan_shrink_factor: float = 0.05

    # ==================== 志愿表容量（T26）====================
    # 志愿表可容纳的「院校专业组」个数上限。各省批次数不同（如广东普通类本科批 45 个），
    # 未在 map 中列出的省份用 default。**只写入已核实的省份**，其余走默认并显式标注待核。
    plan_capacity_default: int = 45
    plan_capacity_map: str = "广东:45"

    # ==================== 案例中心（T27）====================
    # 案例目录（默认项目根 data/cases；留空则用默认推导路径）
    cases_dir: str = ""

    # ==================== 分享（T28）====================
    # 只读分享链接有效期（天）；过期后返回 410。
    # 事实源 = volunteer_plans.share_token / share_expires_at（MySQL），非 Redis —— 见 services/share.py 顶部说明。
    share_ttl_days: int = 7

    # ==================== 缺年保守化（T10 回测定稿）====================
    # 问题：训练窗口缺年（如只有 1 年数据）时，参考位次的**年际不确定性**显著放大。
    # 实测（data/eval/backtest_report.md §数据事实）：相邻年预测位次比 |误差| 中位 11.4%、
    #       p90 45–59%；隔一年 |误差| 中位 18.8%、p90 64%。
    # 做法：按**缺年比例**把参考位次向「更难」（位次数值更小）方向收缩：
    #       L' = L × (1 - missing_ratio × missing_penalty)
    # 收缩后 L 变小 → 判档更偏向「冲」→ 缺年院校**更难被列为稳/保**，
    # 避免仅凭 1 年数据就给出「稳保」的乐观结论（红线：宁可少推不可错推）。
    # ⚠️ 注意：回测显示该系数对**保档命中率**有轻微负向（更审慎），但对**保档有效性**
    #    （不滑档红线）为正向 —— 以有效性为准，命中率让位。
    missing_penalty: float = 0.18
    # 单年数据（缺年比例 = 1）时的额外惩罚：无任何交叉验证，风险最高
    single_year_extra_penalty: float = 0.10

    # ==================== 数据库 ====================
    database_url: str = "mysql+pymysql://root:123456@127.0.0.1:3306/gaokao?charset=utf8mb4"

    # ==================== Redis ====================
    redis_url: str = "redis://127.0.0.1:6380/0"

    # ==================== LLM（OpenAI 兼容，多模型可切换）====================
    # 离线联调开关：true 时 LLM 走确定性 mock，无需任何 API key 即可跑通全链路
    mock_mode: bool = True

    # 默认主模型（业务代码禁止硬编码模型名，一律用 settings.llm_model_chain）
    llm_model: str = "deepseek-v4-flash"
    # 备选降级链（逗号分隔，第一位应是主模型；failover 时依次尝试）
    llm_models: str = "deepseek-v4-flash,qwen-plus,glm-4-flash"
    # 与模型一一对应的 base_url / api_key（逗号分隔，顺序必须与 llm_models 严格一致）
    llm_base_urls: str = "https://api.deepseek.com/v1,https://dashscope.aliyuncs.com/compatible-mode/v1,https://open.bigmodel.cn/api/paas/v4"
    llm_api_keys: str = ",,"

    # 单次请求超时（秒）与最大重试次数
    llm_timeout: float = 30.0
    llm_max_retries: int = 1
    # DeepSeek 思考型模型在低 token 预算下可能把额度全部消耗在 reasoning_content，
    # 导致最终 content 为空。默认关闭思考，仅对 DeepSeek 官方接口注入该参数。
    llm_disable_thinking: bool = True

    # embedding / rerank（RAG 用，T13；可空缺，缺省则退化为关键词匹配）
    llm_embed_model: str = "bge-m3"
    llm_rerank_model: str = "bge-reranker-v2-m3"

    # ==================== 安全 ====================
    # 脱敏哈希盐值（敏感字段落库用；生产必须改）
    score_hash_salt: str = "change-me-in-production"
    admin_token: str = "change-me"
    # 登录令牌签名密钥与有效期（生产必须改）
    auth_secret: str = "change-me-auth-secret"
    auth_token_ttl_hours: int = 24

    # ==================== 限流（T20）====================
    # 频率限流（滑动窗口）——单位「次/分钟」，另设每日窗口防长期刷
    rate_limit_ip_per_min: int = 60
    rate_limit_ip_per_day: int = 1000
    rate_limit_session_per_min: int = 30
    rate_limit_session_per_day: int = 500
    # SSE 长连接：单 IP / 单会话的**并发连接数**上限（与请求频率是两个维度）
    rate_limit_sse_concurrent: int = 3
    # SSE 连接最长存活（秒），超时强制断开，防长连接耗尽
    rate_limit_sse_max_seconds: int = 300
    # 触发限流时的默认重试等待（秒），写入 Retry-After
    rate_limit_retry_after: int = 60
    # 白名单（逗号分隔）——本地开发 IP / 管理 Token，命中即不限流
    rate_limit_whitelist_ips: str = "127.0.0.1,::1"
    rate_limit_whitelist_tokens: str = ""

    # ==================== 注册/登录防滥用（T33）====================
    # 单 IP 注册次数上限（滑动窗口）——防脚本批量建号
    rate_limit_register_per_hour: int = 5
    rate_limit_register_per_day: int = 20
    # 单 IP 登录失败次数上限（小时窗口）——防撞库爆破
    rate_limit_login_fail_per_hour: int = 20
    # 单 IP 验证码签发次数上限（小时窗口）——防无限取码后暴力枚举
    rate_limit_captcha_per_hour: int = 60
    # 图形验证码：开关（本地联调可关）+ 有效期（秒）
    captcha_enabled: bool = True
    captcha_ttl_seconds: int = 300
    cors_origins: str = (
        "http://127.0.0.1:8080,http://localhost:8080,"
        "http://127.0.0.1:5173,http://localhost:5173,"
        "http://127.0.0.1:5174,http://localhost:5174,"
        "http://127.0.0.1:5599,http://localhost:5599"
    )

    # ==================== 派生字段（不入 .env，代码计算）====================
    @property
    def llm_model_chain(self) -> list[str]:
        """模型降级链（去重去空，保持顺序）。第一位即主模型。"""
        raw = self.llm_models or self.llm_model
        seen: list[str] = []
        for name in raw.split(","):
            name = name.strip()
            if name and name not in seen:
                seen.append(name)
        return seen or [self.llm_model]

    @property
    def llm_chain_configs(self) -> list[dict[str, str]]:
        """把模型链与 base_url / api_key 对齐成结构化列表。

        base_urls / api_keys 若数量不足，用最后一个补齐（避免 .env 写不全直接崩）。
        """
        models = self.llm_model_chain
        base_urls = [u.strip() for u in self.llm_base_urls.split(",")]
        api_keys = [k.strip() for k in self.llm_api_keys.split(",")]

        def _pick(seq: list[str], idx: int, fallback: str = "") -> str:
            if not seq:
                return fallback
            if idx < len(seq):
                return seq[idx]
            return seq[-1]

        return [
            {"model": name, "base_url": _pick(base_urls, i), "api_key": _pick(api_keys, i)}
            for i, name in enumerate(models)
        ]

    @property
    def ref_year_list(self) -> list[int]:
        """参考年份列表（int），**从旧到新**（与 year_weight_list 严格同序）。"""
        return [int(y.strip()) for y in self.ref_years.split(",") if y.strip()]

    @property
    def year_weight_list(self) -> list[float]:
        """年份权重列表（float），与 ref_year_list 严格同序。"""
        return [float(w.strip()) for w in self.year_weights.split(",") if w.strip()]

    @property
    def ratelimit_whitelist_ip_list(self) -> list[str]:
        """限流白名单 IP 列表（去空、去重，保持顺序）。"""
        return [x.strip() for x in self.rate_limit_whitelist_ips.split(",") if x.strip()]

    @property
    def ratelimit_whitelist_token_list(self) -> list[str]:
        """限流白名单 Token 列表（去空、去重，保持顺序）。"""
        return [x.strip() for x in self.rate_limit_whitelist_tokens.split(",") if x.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"production", "prod"}

    @property
    def plan_capacity_dict(self) -> dict[str, int]:
        """省份 → 志愿表容量（`省份:容量` 逗号分隔）。解析失败项忽略（不猜测）。"""
        out: dict[str, int] = {}
        for item in (self.plan_capacity_map or "").split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            name, _, cap = item.partition(":")
            try:
                out[name.strip()] = int(cap.strip())
            except ValueError:
                continue
        return out

    def plan_capacity_for(self, province: str) -> int:
        """取某省志愿表容量；未配置省份用默认（并可由调用方显式标注「待核」）。"""
        return self.plan_capacity_dict.get(province, self.plan_capacity_default)

    @field_validator("debug", "mock_mode", "llm_disable_thinking", mode="before")
    @classmethod
    def _normalize_bool_flags(cls, value: object) -> object:
        """兼容部署环境常见的 release/dev/prod 布尔写法。"""
        if isinstance(value, bool) or value is None:
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "debug", "dev", "development"}:
            return True
        if normalized in {"0", "false", "no", "off", "prod", "production", "release"}:
            return False
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    def validate_critical(self) -> list[str]:
        """检查关键配置并返回问题清单（空列表=通过）。

        缺 .env 时由启动流程调用，给出明确中文提示而非直接崩溃。
        """
        problems: list[str] = []
        if not ENV_FILE.exists():
            problems.append(f"未找到配置文件：{ENV_FILE}（请先 `cp .env.example .env`）")
        if not self.database_url:
            problems.append("DATABASE_URL 为空，无法连接数据库")
        if len(self.ref_year_list) != len(self.year_weight_list):
            problems.append(
                f"REF_YEARS({len(self.ref_year_list)}个) 与 YEAR_WEIGHTS({len(self.year_weight_list)}个) 数量不一致"
            )
        if abs(sum(self.year_weight_list) - 1.0) > 1e-6:
            problems.append(f"YEAR_WEIGHTS 之和应为 1，当前为 {sum(self.year_weight_list)}")
        # 顺序约定：REF_YEARS 必须「从旧到新」（与 YEAR_WEIGHTS 同序），否则权重会挂错年份
        yrs = self.ref_year_list
        if yrs != sorted(yrs):
            problems.append(
                f"REF_YEARS 必须按「从旧到新」排列（当前 {self.ref_years}），"
                "否则 YEAR_WEIGHTS 会与年份错配（口径：data_caliber §1.1）"
            )
        # 时效红线：不得引用晚于 CURRENT_YEAR 的年份
        future = [y for y in yrs if y > self.current_year]
        if future:
            problems.append(
                f"REF_YEARS 含晚于 CURRENT_YEAR({self.current_year}) 的年份 {future}，"
                "违反时效红线（禁止使用未来年份数据）"
            )
        if not self.mock_mode and not self.llm_api_keys.strip(", "):
            problems.append("MOCK_MODE=false 但未配置任何 LLM_API_KEYS")
        if self.is_production:
            defaults = {
                "SCORE_HASH_SALT": ("change-me-in-production", self.score_hash_salt),
                "ADMIN_TOKEN": ("change-me", self.admin_token),
                "AUTH_SECRET": ("change-me-auth-secret", self.auth_secret),
            }
            for name, (default, current) in defaults.items():
                if current == default:
                    problems.append(f"生产环境必须修改 {name}")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例配置（lru_cache 保证全局唯一）。"""
    return Settings()


# 全局快捷引用
settings = get_settings()


def env_snapshot() -> dict[str, str]:
    """返回脱敏后的关键配置快照（供 /health 展示，**绝不输出 key 明文**）。"""
    return {
        "app_version": settings.app_version,
        "province": settings.province,
        "current_year": str(settings.current_year),
        "mock_mode": str(settings.mock_mode),
        "llm_model_chain": ",".join(settings.llm_model_chain),
        "python": os.sys.version.split()[0],
    }
