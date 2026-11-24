"""T13 · 政策切片入库（RAG 索引侧）。

职责（**入库脚本，不是检索服务** —— 任务卡「易混淆」明确两者分开）
--------------------------------------------------------------------
1. 读 ``policy_docs``（已由 T12 入库）→ 用 ``chunk.chunk_document()`` 切片；
2. 写 ``policy_chunks``（``doc_id/seq/content/tokens/embedding``）；**幂等**：按 ``doc_id`` 先删后插；
3. 建 **FULLTEXT ngram** 索引（``WITH PARSER ngram``）供 T13 检索粗召回；
   - MySQL 的 ``ngram_token_size=2`` 已确认，中文二元切分可用；
   - 索引不存在才建（幂等），避免重复执行报错。

MOCK_MODE 下的 embedding
------------------------
不引入向量库（红线）。``embedding`` 以 **JSON 文本**存 ``policy_chunks.embedding``：
- MOCK_MODE：**确定性伪向量**（对文本做 sha256 → 展开成固定维度单位向量），同文本同向量；
- 真实模式：调用 embedding 模型（本卡允许空缺，缺省退化为关键词匹配）。
实际排序在应用层（``retrieve.py``）计算余弦相似度。

易混淆
------
- 建索引（本模块，写库）≠ 检索（``retrieve.py``，读库）；
- FULLTEXT ngram ≠ LIKE：前者按 ngram 倒排，能命中「词的部分」；后者是整串包含，
  中文短查询差异明显（T13 测试专门对比）。
"""

from __future__ import annotations

import hashlib
import json
import math
import sys

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import PolicyChunk, PolicyDoc
from app.db.session import SessionLocal
from app.rag.chunk import chunk_document

logger = get_logger(__name__)

# 伪向量维度（MOCK_MODE 用；真实 embedding 模型维度不同时以返回值为准）
MOCK_EMBED_DIM = 64
# FULLTEXT 索引名
FULLTEXT_INDEX = "ft_chunk_content"


# ============================================================
#  embedding（MOCK 确定性伪向量 / 真实模型占位）
# ============================================================


def mock_embedding(text_: str, dim: int = MOCK_EMBED_DIM) -> list[float]:
    """确定性伪向量：sha256(文本) → 展开为 ``dim`` 维**单位向量**。

    为什么可行：同文本恒得同向量 → 余弦相似度在「完全相同」时为 1，不同文本近似随机；
    足以验证「向量重排通路是通的」，且**离线可复现**（无需任何 API key）。
    """
    digest = hashlib.sha256(text_.encode("utf-8")).digest()
    # 用哈希字节循环填充 dim 个 [-1,1] 分量
    raw = [(digest[i % len(digest)] - 127.5) / 127.5 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def embedding_json(text_: str) -> str:
    """把伪向量序列化为 JSON 文本（``policy_chunks.embedding``）。"""
    return json.dumps(mock_embedding(text_), ensure_ascii=False)


# ============================================================
#  索引
# ============================================================


def ensure_fulltext_index(db: Session) -> bool:
    """确保 ``policy_chunks.content`` 上有 FULLTEXT ngram 索引。返回是否**本次新建**。"""
    rows = db.execute(
        text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'policy_chunks' "
            "AND INDEX_NAME = :name"
        ),
        {"name": FULLTEXT_INDEX},
    ).all()
    if rows:
        return False
    db.execute(
        text(
            f"ALTER TABLE policy_chunks ADD FULLTEXT INDEX {FULLTEXT_INDEX} (content) WITH PARSER ngram"
        )
    )
    logger.info("已创建 FULLTEXT ngram 索引：%s", FULLTEXT_INDEX)
    return True


def build_index(db: Session, *, only_missing: bool = False) -> dict[str, int]:
    """为 ``policy_docs`` 建切片索引。返回统计。

    :param only_missing: True 时只为「尚无切片」的文档建（增量）；False 时全量重建。
    """
    doc_ids = [row[0] for row in db.execute(select(PolicyDoc.id).order_by(PolicyDoc.id)).all()]
    if only_missing:
        have = {row[0] for row in db.execute(select(PolicyChunk.doc_id).distinct()).all()}
        doc_ids = [d for d in doc_ids if d not in have]

    total_chunks = 0
    for doc_id in doc_ids:
        doc = db.get(PolicyDoc, doc_id)
        if doc is None:
            continue
        chunks = chunk_document(doc.content)
        db.execute(delete(PolicyChunk).where(PolicyChunk.doc_id == doc_id))
        payload = [
            {
                "doc_id": doc_id,
                "seq": c.seq,
                "content": c.content,
                "tokens": c.tokens,
                "embedding": embedding_json(c.content),
            }
            for c in chunks
        ]
        if payload:
            db.execute(PolicyChunk.__table__.insert(), payload)
            total_chunks += len(payload)

    db.flush()
    created = ensure_fulltext_index(db)
    db.commit()
    return {
        "docs": len(doc_ids),
        "chunks": total_chunks,
        "index_created": int(created),
    }


def chunk_stats(db: Session) -> dict[str, int]:
    """切片统计（供报告/核对）。"""
    n_chunks = db.execute(select(func.count()).select_from(PolicyChunk)).scalar_one()
    n_docs = db.execute(select(func.count()).select_from(PolicyDoc)).scalar_one()
    avg = db.execute(select(func.avg(func.length(PolicyChunk.content)))).scalar_one() or 0
    return {"docs": n_docs, "chunks": n_chunks, "avg_len": int(avg)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="T13 政策切片入库 + FULLTEXT 索引")
    parser.add_argument("--only-missing", action="store_true", help="只为尚无切片的文档建（增量）")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        stats = build_index(db, only_missing=args.only_missing)
        after = chunk_stats(db)
        print("=" * 66)
        print("政策切片索引（T13）")
        print("=" * 66)
        print(f"  本次处理文档 : {stats['docs']} 篇")
        print(f"  生成切片     : {stats['chunks']} 块")
        print(f"  FULLTEXT 新建 : {'是' if stats['index_created'] else '否（已存在）'}")
        print(f"  库内合计     : {after['docs']} 篇 / {after['chunks']} 块 / 平均 {after['avg_len']} 字")
        if after["chunks"] == 0:
            print("  ⚠️ 无切片，请确认 policy_docs 已由 T12 入库。")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
