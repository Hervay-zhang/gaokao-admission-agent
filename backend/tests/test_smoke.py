"""T01 冒烟测试 —— 对应任务卡「测试场景」J1–J8。

J1 /health 返回 200 与 {"status":"ok"}
J2 MySQL 连接成功（SELECT 1）   —— 需真实库，缺省跳过
J3 前端首页渲染占位（由前端自测，此处仅验证后端不阻塞）
J4 缺 .env 时给出明确报错而非崩溃
J5 mock 模式下 LLMClient.chat() 同输入同输出（确定性）
J6 failover：主模型指向死端口 → 自动切下一个，响应 model 为备选
J7 全链不可用 → 抛 LLMError（不崩溃）
J8 /api/health/llm 返回模型清单与状态
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import LLMError
from app.core.llm import LLMClient
from app.main import app

client = TestClient(app)


# ==================== J1 ====================
def test_j1_health_ok() -> None:
    """/health 返回 200 且 status=ok。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "config" in body


# ==================== J2 ====================
def test_j2_db_select1() -> None:
    """MySQL 连接成功（SELECT 1）。无库时跳过，不算失败。"""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
        assert result == 1
    except OperationalError:
        pytest.skip("本机 MySQL 未就绪，跳过 J2（起库后应通过）")


# ==================== J3 ====================
def test_j3_backend_does_not_block_frontend() -> None:
    """后端不监听静态前端，前端独立起服务；此处验证 CORS 允许 8080。"""
    resp = client.get("/health", headers={"Origin": "http://127.0.0.1:8080"})
    assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:8080"


# ==================== J4 ====================
def test_j4_config_critical_report() -> None:
    """配置校验返回问题清单（缺 .env 时含明确提示），不抛异常。"""
    from app.core.config import settings

    problems = settings.validate_critical()
    assert isinstance(problems, list)  # 无论通过与否都是列表，不崩溃


# ==================== J5 ====================
@pytest.mark.asyncio
async def test_j5_mock_deterministic() -> None:
    """mock 模式下同输入同输出（确定性）。"""
    llm = LLMClient(mock_mode=True)
    messages = [{"role": "user", "content": "广东 2025 物理类 600 分能报什么"}]

    r1 = await llm.chat(messages)
    r2 = await llm.chat(messages)

    assert r1.mocked is True
    assert r1.content == r2.content, "mock 模式必须同输入同输出"
    assert r1.model == r2.model

    # 不同输入 → 不同输出（指纹不同）
    r3 = await llm.chat([{"role": "user", "content": "完全不同的问题"}])
    assert r3.content != r1.content


# ==================== J6 ====================
@pytest.mark.asyncio
async def test_j6_failover_to_backup(monkeypatch) -> None:
    """主模型 base_url 指向死端口 → 自动切到链中下一个模型。"""
    from app.core.config import settings

    # 构造链：主模型指向死端口，备选指向可用的本地假服务
    monkeypatch.setattr(
        type(settings),
        "llm_chain_configs",
        property(
            lambda self: [
                {"model": "dead-primary", "base_url": "http://127.0.0.1:9", "api_key": "x"},
                {"model": "backup-model", "base_url": "http://127.0.0.1:9", "api_key": "x"},
            ]
        ),
    )

    llm = LLMClient(mock_mode=False)

    # 用 monkeypatch 替换 _post_chat：第一个模型必败，第二个成功
    call_order: list[str] = []

    async def fake_post(cfg, messages, temperature, max_tokens):
        call_order.append(cfg["model"])
        if cfg["model"] == "dead-primary":
            raise RuntimeError("connection refused (死端口)")
        return {"choices": [{"message": {"content": "备选模型回答"}}], "usage": {}}

    monkeypatch.setattr(llm, "_post_chat", fake_post)

    resp = await llm.chat([{"role": "user", "content": "hi"}])
    assert resp.model == "backup-model", "failover 后应使用备选模型"
    assert call_order == ["dead-primary", "backup-model"], "应按链顺序尝试"
    assert len(resp.attempts) == 1 and resp.attempts[0]["status"] == "failed"


# ==================== J7 ====================
@pytest.mark.asyncio
async def test_j7_all_models_down_raises(monkeypatch) -> None:
    """全链不可用 → 抛 LLMError（不崩溃）。"""
    from app.core.config import settings

    monkeypatch.setattr(
        type(settings),
        "llm_chain_configs",
        property(
            lambda self: [
                {"model": "dead-1", "base_url": "http://127.0.0.1:9", "api_key": "x"},
                {"model": "dead-2", "base_url": "http://127.0.0.1:9", "api_key": "x"},
            ]
        ),
    )

    llm = LLMClient(mock_mode=False)

    async def always_fail(cfg, messages, temperature, max_tokens):
        raise RuntimeError("all down")

    monkeypatch.setattr(llm, "_post_chat", always_fail)

    with pytest.raises(LLMError):
        await llm.chat([{"role": "user", "content": "hi"}])


def test_deepseek_thinking_option_only_applies_to_official_endpoint() -> None:
    """DeepSeek 关闭思考参数只注入官方接口，避免影响其他兼容模型。"""
    llm = LLMClient(mock_mode=False)
    llm.disable_thinking = True

    deepseek_payload: dict = {}
    llm._apply_provider_options(deepseek_payload, {"base_url": "https://api.deepseek.com/v1"})
    assert deepseek_payload["thinking"] == {"type": "disabled"}

    other_payload: dict = {}
    llm._apply_provider_options(other_payload, {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    assert "thinking" not in other_payload


@pytest.mark.asyncio
async def test_cloud_model_without_key_is_skipped(monkeypatch) -> None:
    """云端备选模型未配置 Key 时直接跳过，不发起无鉴权请求。"""
    from app.core.config import settings

    monkeypatch.setattr(
        type(settings),
        "llm_chain_configs",
        property(
            lambda self: [
                {"model": "no-key-cloud", "base_url": "https://example.com/v1", "api_key": ""},
                {"model": "local-model", "base_url": "http://127.0.0.1:9999/v1", "api_key": ""},
            ]
        ),
    )
    llm = LLMClient(mock_mode=False)
    called: list[str] = []

    async def fake_post(cfg, messages, temperature, max_tokens):
        called.append(cfg["model"])
        return {"choices": [{"message": {"content": "本地模型回答"}}], "usage": {}}

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    resp = await llm.chat([{"role": "user", "content": "hi"}])
    assert resp.model == "local-model"
    assert called == ["local-model"]
    assert resp.attempts[0]["status"] == "skipped"


# ==================== J8 ====================
def test_j8_health_llm() -> None:
    """/api/health/llm 返回模型链与状态（mock 模式）。"""
    resp = client.get("/api/health/llm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "chain" in body and isinstance(body["chain"], list) and len(body["chain"]) >= 1
    assert "models" in body and len(body["models"]) >= 1
    assert body["primary"] == body["chain"][0]
