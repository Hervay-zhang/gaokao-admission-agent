"""T13 · 政策切片索引构建（脚本入口，薄包装 app.rag.index）。

跑法（backend 目录下）：
    ./.venv/Scripts/python.exe scripts/build_rag_index.py                # 全量重建
    ./.venv/Scripts/python.exe scripts/build_rag_index.py --only-missing # 增量

做两件事：
1. 读 ``policy_docs`` → 切片 → 写 ``policy_chunks``（幂等：按 doc_id 先删后插）；
2. 建 ``policy_chunks.content`` 的 **FULLTEXT ngram** 索引（不存在才建）。

红线：不引入向量库；切片不跨标题（由 ``app/rag/chunk.py`` 保证）。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.rag.index import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
