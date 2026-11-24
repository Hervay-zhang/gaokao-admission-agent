"""pytest 全局夹具。

T01 阶段测试不依赖真实 MySQL/Redis（用 MOCK_MODE + 纯内存即可）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 确保能 import app（backend 根目录加入 sys.path）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 测试必须确定性：部分开发机把 DEBUG 注入为 release 等非布尔值，
# pydantic-settings 会优先读取环境变量并在收集阶段直接报错。
os.environ["DEBUG"] = "false"
# 测试默认隔离外部模型，避免单元测试意外消耗真实 API Key。
if os.getenv("RUN_REAL_LLM_TESTS") != "1":
    os.environ["MOCK_MODE"] = "true"
# 有独立测试库时优先使用，避免破坏性测试或导入测试触碰开发库。
if os.getenv("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
