"""T04 · 全量数据模型（MySQL 8 + SQLAlchemy 2.0）。

设计要点
--------
1. **year 维度贯穿**：`score_segments` / `admission_lines` 以 `year` 为一级维度，联合唯一；
2. **禁用 FLOAT**：分数 / 位次 / 计划数一律 `Integer`，权重用 `Numeric`（金额/分数不得用浮点，口径红线）；
3. **敏感列占位**：`user_profiles` 的分数 / 位次只落 `score_hash`（加盐哈希）+ `score_bucket`（区间桶），
   明文仅内存态使用（PIPL，详见 data_caliber §八）；
4. **选科要求是「专业组」字段**：`admission_lines.subject_requirement`（JSON），非院校字段；
5. **唯一索引**（防重复导入 + 幂等）：
   - `score_segments(province, year, subject_type, score)` 唯一；
   - `admission_lines(year, subject_type, university_code, group_code)` 唯一；
   - `universities.code` 唯一；
6. **迁移即真相**：表结构以 `migrations/versions/` 为准（本文件为模型声明，与迁移保持一致）。

表清单（21 张）
--------------
基础数据：`provinces` `score_segments` `universities` `admission_lines`
政策语料：`policy_docs` `policy_chunks`
会话：`sessions` `messages`
用户档案：`user_profiles`
推荐与志愿表：`recommendations` `volunteer_plans` `volunteer_items`
运营：`feedback` `audit_log` `data_version` `admin_user`
社区：`forum_posts` `forum_comments` `forum_post_likes` `forum_post_favorites`
配置：`quick_links`
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有模型的基类（SQLAlchemy 2.0 Declarative）。"""

    # 默认表选项：utf8mb4 已在 DB 级设定，此处仅声明引擎
    __table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


# ===========================================================================
#  一、基础数据（T02/T03 导入）
# ===========================================================================


class Province(Base):
    """省份与高考模式（3+1+2 / 3+3 / 传统文理）。

    对应 data_caliber §5.1：`code, name, mode, primary_subjects[], secondary_pool[], volunteer_capacity`。
    """

    __tablename__ = "provinces"

    code: Mapped[str] = mapped_column(String(16), primary_key=True, comment="省份代码（拼音，如 guangdong）")
    name: Mapped[str] = mapped_column(String(32), nullable=False, comment="省份名（如 广东）")
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="高考模式：3+1+2 / 3+3 / 传统文理"
    )
    primary_subjects: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="首选科目（逗号分隔，如 物理,历史）"
    )
    secondary_pool: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="再选池（逗号分隔，如 化学,生物,政治,地理）"
    )
    volunteer_capacity: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=45, comment="志愿容量（可填数量上限）"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ScoreSegment(Base):
    """一分一段表（T02 产出，3503 行）。

    **口径**：`rank` = 官方「本科（含本科加分）累计人数」= 该分数及以上考生总数
    = 拿到该分数考生的**位次上限**（同分并列取累计值，data_caliber §二）。
    """

    __tablename__ = "score_segments"
    __table_args__ = (
        UniqueConstraint("province", "year", "subject_type", "score", name="uq_score_seg"),
        # 注意：唯一约束已覆盖 (province,year,subject_type,score) 的前缀查询，无需重复建同列普通索引
        Index("ix_score_seg_rank", "province", "year", "subject_type", "rank"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    province: Mapped[str] = mapped_column(String(32), nullable=False, comment="省份（如 广东）")
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="年份（一级维度）")
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="科类：物理 / 历史")
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="分数（100..top）")
    rank: Mapped[int] = mapped_column(Integer, nullable=False, comment="累计人数（= 位次上限）")
    source: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="来源标注（官方公告 URL）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __mapper_args__ = {"eager_defaults": True}


class University(Base):
    """院校库（T03 产出，1095 所）。

    **同名院校按 `code` 严格区分**（哈工大 10213/18213/19213 等，见 university_name_audit.txt）。
    """

    __tablename__ = "universities"
    __table_args__ = (
        UniqueConstraint("code", name="uq_university_code"),
        Index("ix_university_name", "name"),
        Index("ix_university_base", "base_name"),
        Index("ix_university_province", "province"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, comment="院校代码（唯一）")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="完整规范名（含校区后缀）")
    base_name: Mapped[str] = mapped_column(String(128), nullable=False, default="", comment="去括号基础校名")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="本科", comment="本科 / 专科")
    province: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="院校所在省份（未知留空）")
    school_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="综合/理工/师范/医药/财经/农林/政法/语言/艺术/体育/军事/民族"
    )
    tags: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="985|211|双一流（| 分隔）")
    name_variants: Mapped[str] = mapped_column(Text, nullable=False, comment="名称变体（| 分隔，清洗溯源）")
    meta_status: Mapped[str] = mapped_column(
        String(8), nullable=False, default="待补", comment="已核 / 待补（元数据完整度）"
    )
    admission_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="学校招生官网（管理端维护）"
    )
    admission_url_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="待核", comment="官网核验状态：已核 / 待核 / 不可用"
    )
    admission_url_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="官网最后一次核验时间"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AdmissionLine(Base):
    """投档线（T03 产出，15868 条）。

    红线：**投档线 ≠ 录取线**；官方「-」行保留但 `min_score`/`min_rank` 为空（不补造）。
    `subject_requirement` 属**专业组**级别（非院校），JSON 结构见 data_caliber §5.2。
    """

    __tablename__ = "admission_lines"
    __table_args__ = (
        UniqueConstraint(
            "year", "subject_type", "university_code", "group_code", name="uq_admission_line"
        ),
        # 注意：唯一约束已覆盖 (year,subject_type,university_code) 前缀，无需重复建 ix_adm_lookup
        Index("ix_adm_rank", "year", "subject_type", "min_rank"),
        Index("ix_adm_province", "province", "year", "subject_type"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    province: Mapped[str] = mapped_column(String(32), nullable=False, comment="省份")
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="年份（一级维度）")
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="科类")
    university_code: Mapped[str] = mapped_column(String(16), nullable=False, comment="院校代码")
    university_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="院校名（冗余，便于查询）")
    group_code: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="专业组代码")
    plan_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="计划数（整数，禁 FLOAT）")
    admitted_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="投档人数")
    min_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, comment="投档最低分（官方「-」为空）")
    min_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="投档最低排位")
    subject_requirement: Mapped[str] = mapped_column(
        Text, nullable=False, comment='选科要求 JSON：{mode,first,second,pattern,status,raw}'
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ===========================================================================
#  二、政策语料（RAG，T13 落地内容）
# ===========================================================================


class PolicyDoc(Base):
    """政策原文（RAG 一级：文档）。"""

    __tablename__ = "policy_docs"
    __table_args__ = (
        Index("ix_policy_year", "year"),
        Index("ix_policy_province", "province"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="标题")
    url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="官方 URL（引用用）")
    source: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="来源机构")
    province: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="适用省份（国家级留空）")
    year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, comment="发布年份")
    doc_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="通知/办法/问答/表格")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="正文（纯文本）")
    doc_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="内容 sha256（去重用，唯一）")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    chunks: Mapped[list[PolicyChunk]] = relationship(
        back_populates="doc", cascade="all, delete-orphan"
    )


class PolicyChunk(Base):
    """政策切片（RAG 二级：检索单元）。

    检索方案：MySQL FULLTEXT(ngram) 粗召回 + jieba 分词 + 向量 JSON 余弦重排（不引向量库）。
    `embedding` 以 JSON 文本存储（如 `[0.1,0.2,...]`），排序在应用层计算。
    """

    __tablename__ = "policy_chunks"
    __table_args__ = (
        Index("ix_chunk_doc", "doc_id", "seq"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policy_docs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="段落序号")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="切片正文")
    tokens: Mapped[str | None] = mapped_column(Text, nullable=True, comment="jieba 分词结果（空格分隔）")
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True, comment="向量 JSON（余弦重排用）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    doc: Mapped[PolicyDoc] = relationship(back_populates="chunks")


# ===========================================================================
#  三、会话（匿名优先，session 主键即会话 token）
# ===========================================================================


class Session(Base):
    """会话（匿名优先：不强制登录，`sid` 为前端生成的匿名 ID）。"""

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("sid", name="uq_session_sid"),
        Index("ix_session_updated", "updated_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sid: Mapped[str] = mapped_column(String(64), nullable=False, comment="会话 ID（匿名 token）")
    owner_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="登录账号归属（匿名会话为空）"
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user_profiles.id", ondelete="SET NULL"), nullable=True, comment="绑定用户（可空）"
    )
    title: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="会话标题（首问摘要）")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    messages: Mapped[list[Message]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    """会话消息（user / assistant / system）。

    红线：**日志禁明文考分/身份**；消息正文按需脱敏后再落库（T17 落地写入逻辑）。
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_message_session", "session_id", "seq"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="会话内顺序")
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="user / assistant / system")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息正文")
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="意图（政策/推荐/寒暄/超范围）")
    citations: Mapped[str | None] = mapped_column(Text, nullable=True, comment="引用 JSON：[{title,url}]")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="生成该回复的模型")
    mocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否 mock 生成")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped[Session] = relationship(back_populates="messages")


# ===========================================================================
#  四、用户档案（敏感列脱敏落库）
# ===========================================================================


class UserProfile(Base):
    """用户档案（PIPL 脱敏）。

    **红线**：分数 / 位次以 `score_hash`（加盐 sha256）+ `score_bucket`（如 "610-619"）落库，
    **不存明文**；明文仅内存态用于推荐计算（data_caliber §八）。
    """

    __tablename__ = "user_profiles"
    __table_args__ = (
        Index("ix_profile_province_year", "province", "current_year"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    anon_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="匿名 ID（未登录时）")
    owner_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="登录账号归属（匿名档案为空）"
    )
    province: Mapped[str] = mapped_column(String(32), nullable=False, comment="省份")
    current_year: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="报考年份")
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="首选科目（物理/历史）")
    elective_subjects: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="再选科目（逗号分隔）"
    )
    # ---- 敏感列：脱敏落库（绝不存明文）----
    score_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分数加盐哈希（脱敏）")
    score_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="分数区间桶（如 610-619）")
    rank_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="位次加盐哈希（脱敏）")
    rank_bucket: Mapped[str | None] = mapped_column(String(24), nullable=True, comment="位次区间桶（如 35000-35999）")
    # ---- 偏好（非敏感）----
    preferred_cities: Mapped[str | None] = mapped_column(Text, nullable=True, comment="意向城市 JSON 数组")
    preferred_majors: Mapped[str | None] = mapped_column(Text, nullable=True, comment="意向专业 JSON 数组")
    preferred_types: Mapped[str | None] = mapped_column(Text, nullable=True, comment="意向院校类型 JSON 数组")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# ===========================================================================
#  五、推荐与志愿表
# ===========================================================================


class Recommendation(Base):
    """推荐结果（冲稳保三档，T08 产出）。

    红线：**禁止录取概率数字**；只输出档位（冲/稳/保）+ 依据位次 + 口径说明。
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_rec_profile", "profile_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="基于哪一年的数据")
    tier: Mapped[str] = mapped_column(String(8), nullable=False, comment="冲 / 稳 / 保")
    university_code: Mapped[str] = mapped_column(String(16), nullable=False)
    university_name: Mapped[str] = mapped_column(String(128), nullable=False)
    group_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    base_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="测算基线位次（三年加权）")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="推荐依据说明（可读文案）")
    caliber: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="口径说明（如「基于 2024–2026 三年投档数据 0.2/0.3/0.5 加权」）"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReportSnapshot(Base):
    """报告历史快照（用于“我的”页回看已生成报告，不含明文分数/位次）。"""

    __tablename__ = "report_snapshots"
    __table_args__ = (
        Index("ix_report_owner_created", "owner_username", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user_profiles.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="冲稳保路径报告")
    province: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    score_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rank_bucket: Mapped[str | None] = mapped_column(String(24), nullable=True)
    current_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    tier_counts: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="冲稳保条数 JSON")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class VolunteerPlan(Base):
    """志愿表（用户收藏/编排的方案）。"""

    __tablename__ = "volunteer_plans"
    __table_args__ = (
        Index("ix_plan_profile", "profile_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    owner_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="登录账号归属（旧数据可空）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="我的志愿表")
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="乐观锁版本（每次变更 +1）"
    )
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分享 token（唯一）")
    share_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="分享链接过期时间（只读分享，过期失效）"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    items: Mapped[list[VolunteerItem]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class VolunteerItem(Base):
    """志愿表条目（一个院校专业组一行，含顺序）。"""

    __tablename__ = "volunteer_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "seq", name="uq_volunteer_item_seq"),
        Index("ix_item_plan", "plan_id", "seq"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("volunteer_plans.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="顺序（志愿 1..N）")
    university_code: Mapped[str] = mapped_column(String(16), nullable=False)
    university_name: Mapped[str] = mapped_column(String(128), nullable=False)
    group_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    tier: Mapped[str | None] = mapped_column(String(8), nullable=True, comment="当前档位（默认同 system_tier，用户可改）")
    system_tier: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="系统推荐档位（写入后只读，用户调整不得覆盖）"
    )
    user_adjusted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", comment="用户是否调整过档位"
    )
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    plan: Mapped[VolunteerPlan] = relationship(back_populates="items")


# ===========================================================================
#  六、运营（反馈闭环 / 审计 / 数据版本 / 管理）
# ===========================================================================


class Feedback(Base):
    """反馈闭环（点赞/点踩/纠错落库，作为优化资产）。

    红线（T22）：
    - **一条输出仅一次反馈**：`message_id` 唯一约束兜底（应用层先查 + DB 约束双保险）；
    - **快照必须去身份**：`prompt_snapshot` / `response_snapshot` 落库前经 T17 `sanitize_text` 脱敏
      （分数/位次/手机号等一律掩码），且不携带任何用户身份字段；
    - 反馈是**独立资产**，不反向污染推荐结果（下游只做统计分析）。
    """

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_feedback_message"),
        Index("ix_feedback_message", "message_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, comment="被反馈的消息 id（唯一）"
    )
    session_sid: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="匿名会话 ID（不含身份）")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="up / down / correction")
    dimension: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="反馈维度：answer（问答）/ recommend（推荐）"
    )
    rating: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True, comment="评分：up=1 / down=-1（correction 不评）"
    )
    reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="点踩原因：推荐不准 / 数据错误 / 回答跑题 / 其他"
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="纠错内容（correction 时）")
    # ---- 快照（去身份，落库前经 T17 sanitize_text 脱敏）----
    prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True, comment="提问快照（脱敏）")
    response_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True, comment="回复快照（脱敏）")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="生成该回复的模型")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    """审计日志（数据版本切换/管理操作留痕，可回溯）。"""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_action", "action", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, comment="动作（switch_year/import/...）")
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="操作者（admin/script）")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更详情 JSON（脱敏）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DataVersion(Base):
    """数据版本（唯一有效行）：当前 `current_year` / `ref_years` / `year_weights`。

    切换新一年数据后，推荐窗口自动滚动；切换写 `audit_log`；可回滚（data_caliber §1.3）。
    """

    __tablename__ = "data_version"
    __table_args__ = (
        Index("ix_dv_active", "is_active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    current_year: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="当前报考年份")
    ref_years: Mapped[str] = mapped_column(String(64), nullable=False, comment="参考年份（逗号分隔）")
    year_weights: Mapped[str] = mapped_column(String(64), nullable=False, comment="年份权重（逗号分隔，和=1）")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="唯一有效标记")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdminUser(Base):
    """管理后台账号（切换数据版本、查看审计）。

    红线：账密一律 `.env` 注入 + 加盐哈希入库，**禁明文**。
    """

    __tablename__ = "admin_user"
    __table_args__ = (
        UniqueConstraint("username", name="uq_admin_username"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, comment="登录名（唯一）")
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False, comment="密码加盐哈希（禁明文）")
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ===========================================================================
# ===========================================================================
#  七、社区/论坛（匿名社区，session_sid 为用户标识）
# ===========================================================================


class ForumPost(Base):
    """社区帖子（匿名用户发布）。"""

    __tablename__ = "forum_posts"
    __table_args__ = (
        Index("ix_forum_post_category", "category", "created_at"),
        Index("ix_forum_post_author", "author_sid", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, comment="帖子标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="正文")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="general", comment="板块分类")
    author_sid: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="作者匿名会话 ID")
    author_nickname: Mapped[str] = mapped_column(String(32), nullable=False, default="匿名同学", comment="作者昵称（快照）")
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="点赞数（缓存）")
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="评论数（缓存）")
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="浏览数（缓存）")
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否置顶")
    is_removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否删除（软删）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ForumComment(Base):
    """帖子评论（支持一级回复，盖楼式）。"""

    __tablename__ = "forum_comments"
    __table_args__ = (
        Index("ix_forum_comment_post", "post_id", "created_at"),
        Index("ix_forum_comment_author", "author_sid", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("forum_posts.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="评论内容")
    author_sid: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="评论者匿名会话 ID")
    author_nickname: Mapped[str] = mapped_column(String(32), nullable=False, default="匿名同学", comment="评论者昵称")
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="点赞数")
    reply_to_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="回复的评论 ID（一级回复）")
    reply_to_nickname: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="被回复者昵称快照")
    is_removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否删除（软删）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ForumCommentLike(Base):
    """评论点赞记录（登录账号或匿名会话一人一次）。"""

    __tablename__ = "forum_comment_likes"
    __table_args__ = (
        UniqueConstraint("comment_id", "user_sid", name="uq_forum_comment_like_user"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    comment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("forum_comments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_sid: Mapped[str] = mapped_column(String(64), nullable=False, comment="点赞用户标识")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ForumPostLike(Base):
    """帖子点赞记录（一人一次）。"""

    __tablename__ = "forum_post_likes"
    __table_args__ = (
        UniqueConstraint("post_id", "user_sid", name="uq_forum_post_like_user"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("forum_posts.id"), nullable=False, index=True)
    user_sid: Mapped[str] = mapped_column(String(64), nullable=False, comment="点赞用户匿名 ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ForumPostFavorite(Base):
    """帖子收藏记录（一人一次）。"""

    __tablename__ = "forum_post_favorites"
    __table_args__ = (
        UniqueConstraint("post_id", "user_sid", name="uq_forum_post_fav_user"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("forum_posts.id"), nullable=False, index=True)
    user_sid: Mapped[str] = mapped_column(String(64), nullable=False, comment="收藏用户匿名 ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CommunityReport(Base):
    """社区举报与审核记录。"""

    __tablename__ = "community_reports"
    __table_args__ = (
        Index("ix_community_report_status", "status", "created_at"),
        Index("ix_community_report_target", "target_type", "target_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="post / comment")
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reporter_sid: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    resolver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

# ===========================================================================
#  八、运营配置（页面底部快捷链接，管理后台可设置）
# ===========================================================================


class QuickLink(Base):
    """页面底部快捷链接（footer 常驻展示；管理后台/API 可增删改）。

    用途：对齐参考站「底部快捷链接」——默认含阳光高考等官方高考平台入口，
    管理员可在管理后台调整（标签/链接/分组/排序/启停）。
    红线：URL 只接受 http/https，避免 javascript: 注入。
    """

    __tablename__ = "quick_links"
    __table_args__ = (
        Index("ix_quick_link_sort", "is_active", "sort_order"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False, comment="显示名")
    url: Mapped[str] = mapped_column(String(512), nullable=False, comment="链接（http/https）")
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="", comment="图标字（emoji/单字，可空）")
    group: Mapped[str] = mapped_column(String(32), nullable=False, default="gaokao", comment="分组：gaokao 高考平台 / official 官方 / tool 工具")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="排序（小在前）")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ConsultationMessage(Base):
    """首页滚动咨询浮窗消息（管理员维护，学生/家长可见）。"""

    __tablename__ = "consultation_messages"
    __table_args__ = (
        Index("ix_consultation_active_sort", "is_active", "sort_order"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    content: Mapped[str] = mapped_column(String(160), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ExpertAppointment(Base):
    """专家服务预约记录。"""

    __tablename__ = "expert_appointments"
    __table_args__ = (
        Index("ix_expert_appointment_status", "status", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    contact_name: Mapped[str] = mapped_column(String(64), nullable=False)
    contact_info: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class UserAccount(Base):
    """登录账号（考生 / 家长 / 管理员）。

    密码只保存 PBKDF2-SHA256 哈希；登录令牌由服务端 HMAC 签名，不保存明文密码。
    """

    __tablename__ = "user_accounts"
    __table_args__ = (
        UniqueConstraint("username", name="uq_user_account_username"),
        Index("ix_user_account_role", "role", "is_active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="student")
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="用户可编辑昵称")
    grade: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="年级")
    avatar: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    avatar_data: Mapped[str | None] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"),
        nullable=True,
        comment="压缩后的头像 Data URL",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


#  敏感列辅助（脱敏工具在 app/core/security.py，T17 完整落地）
# ===========================================================================
# 注意：如需金额/权重列，一律用 Numeric(12, 2)，**禁止 FLOAT**（口径红线）。

__all__ = [
    "Base",
    "Province",
    "ScoreSegment",
    "University",
    "AdmissionLine",
    "PolicyDoc",
    "PolicyChunk",
    "Session",
    "Message",
    "UserProfile",
    "Recommendation",
    "ReportSnapshot",
    "VolunteerPlan",
    "VolunteerItem",
    "Feedback",
    "AuditLog",
    "DataVersion",
    "AdminUser",
    "ForumPost",
    "ForumComment",
    "ForumCommentLike",
    "ForumPostLike",
    "ForumPostFavorite",
    "CommunityReport",
    "QuickLink",
    "ConsultationMessage",
    "ExpertAppointment",
    "UserAccount",
]
