"""LLM 客户端（基础设施层）—— 多模型可切换 + 自动 failover。

设计目标
--------
1. **业务与模型解耦**：任何 service / agent **禁止**硬编码模型名，一律注入本 `LLMClient`。
2. **防单模型停服**：`LLM_MODELS` 是一条降级链，主模型超时/5xx/停服/欠费时自动切下一个。
3. **统一协议**：所有模型走 OpenAI 兼容协议（`/chat/completions`），换模型 ≠ 换 SDK。
4. **可离线**：`MOCK_MODE=true` 时返回确定性 mock（同输入同输出），无 key 也能跑通全链路。
5. **失败可控**：全链失败抛 `LLMError`，由上层切换到「专家规则模式」，而不是崩溃。

响应统一带 `model` 字段，标明**实际**使用的是哪个模型（failover 后可追溯）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 触发 failover 的 HTTP 状态码（服务端错误 / 限流 / 欠费）
_FAILOVER_STATUS = {401, 402, 403, 408, 429, 500, 502, 503, 504}


@dataclass
class LLMResponse:
    """LLM 调用结果。"""

    content: str
    model: str
    mocked: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    attempts: list[dict[str, str]] = field(default_factory=list)  # failover 轨迹

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "model": self.model,
            "mocked": self.mocked,
            "usage": self.usage,
            "attempts": self.attempts,
        }


class LLMClient:
    """OpenAI 兼容的 LLM 客户端，内置多模型 failover。"""

    def __init__(
        self,
        *,
        mock_mode: bool | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.mock_mode = settings.mock_mode if mock_mode is None else mock_mode
        self.timeout = settings.llm_timeout if timeout is None else timeout
        self.max_retries = settings.llm_max_retries if max_retries is None else max_retries
        self.disable_thinking = settings.llm_disable_thinking
        self._chain = settings.llm_chain_configs  # [{model, base_url, api_key}, ...]

    # ==================== 对外主入口 ====================

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> LLMResponse:
        """一次性对话。

        :param model: 指定模型时只用该模型（不走 failover），用于测试/管理；
                      默认 None 走降级链。
        """
        if self.mock_mode:
            return self._mock_response(messages, model=model)

        chain = self._resolve_chain(model)
        attempts: list[dict[str, str]] = []

        for cfg in chain:
            name = cfg["model"]
            if self._requires_api_key(cfg) and not cfg.get("api_key"):
                attempts.append({"model": name, "status": "skipped", "reason": "api_key 未配置"})
                logger.info("模型 %s 未配置 API Key，跳过", name)
                continue
            try:
                data = await self._post_chat(cfg, messages, temperature, max_tokens)
                content = self._extract_content(data)
                return LLMResponse(
                    content=content,
                    model=name,
                    mocked=False,
                    usage=data.get("usage", {}),
                    attempts=attempts,
                )
            except Exception as exc:  # noqa: BLE001 - 需要捕获各类网络/协议异常以决定是否 failover
                reason = f"{type(exc).__name__}: {exc}"
                attempts.append({"model": name, "status": "failed", "reason": reason})
                logger.warning("模型 %s 调用失败，尝试降级：%s", name, reason)
                continue

        # 全链失败
        raise LLMError(
            "所有模型均不可用，已切换至专家规则模式",
            detail={"attempts": attempts},
        )

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """流式对话（SSE 打字机用，T19 接入）。

        mock 模式下按字符切片模拟流式；真实模式走 OpenAI 兼容 stream=true。
        """
        if self.mock_mode:
            resp = self._mock_response(messages, model=model)
            for i in range(0, len(resp.content), 4):
                yield resp.content[i : i + 4]
            return

        chain = self._resolve_chain(model)
        last_err: Exception | None = None
        for cfg in chain:
            if self._requires_api_key(cfg) and not cfg.get("api_key"):
                logger.info("模型 %s 未配置 API Key，跳过", cfg["model"])
                continue
            try:
                async for token in self._stream_from(cfg, messages, temperature, max_tokens):
                    yield token
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("模型 %s 流式失败，降级：%r", cfg["model"], exc)
                continue
        raise LLMError("所有模型流式调用均失败", detail={"last": repr(last_err)})

    async def health(self) -> dict[str, Any]:
        """报告模型链与各模型可用状态（/api/health/llm 用）。"""
        if self.mock_mode:
            return {
                "mock_mode": True,
                "primary": self._chain[0]["model"] if self._chain else None,
                "models": [
                    {"model": c["model"], "status": "mock", "base_url": self._mask_url(c["base_url"])}
                    for c in self._chain
                ],
            }

        models: list[dict[str, Any]] = []
        for cfg in self._chain:
            if not cfg["api_key"]:
                models.append({"model": cfg["model"], "status": "no_key", "base_url": self._mask_url(cfg["base_url"])})
                continue
            try:
                await self._post_chat(cfg, [{"role": "user", "content": "ping"}], 0.0, 1)
                models.append({"model": cfg["model"], "status": "ok", "base_url": self._mask_url(cfg["base_url"])})
            except Exception as exc:  # noqa: BLE001
                models.append(
                    {
                        "model": cfg["model"],
                        "status": "down",
                        "base_url": self._mask_url(cfg["base_url"]),
                        "reason": type(exc).__name__,
                    }
                )
        return {
            "mock_mode": False,
            "primary": self._chain[0]["model"] if self._chain else None,
            "models": models,
        }

    # ==================== 内部实现 ====================

    def _resolve_chain(self, model: str | None) -> list[dict[str, str]]:
        """确定实际要尝试的模型列表。指定 model 时只试它。"""
        if model:
            for cfg in self._chain:
                if cfg["model"] == model:
                    return [cfg]
            return [{"model": model, "base_url": "", "api_key": ""}]
        return self._chain

    async def _post_chat(
        self,
        cfg: dict[str, str],
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """发一次非流式请求。失败抛异常（由 chat 决定是否 failover）。"""
        if not cfg["base_url"]:
            raise RuntimeError("base_url 未配置")
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        self._apply_provider_options(payload, cfg)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code in _FAILOVER_STATUS:
                raise RuntimeError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()

    async def _stream_from(
        self,
        cfg: dict[str, str],
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """单模型的流式请求。"""
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        self._apply_provider_options(payload, cfg)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code in _FAILOVER_STATUS:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"响应结构异常: {exc}") from exc

    def _apply_provider_options(self, payload: dict[str, Any], cfg: dict[str, str]) -> None:
        if self.disable_thinking and "api.deepseek.com" in cfg.get("base_url", ""):
            payload["thinking"] = {"type": "disabled"}

    @staticmethod
    def _requires_api_key(cfg: dict[str, str]) -> bool:
        base_url = cfg.get("base_url", "").lower()
        return bool(base_url) and "127.0.0.1" not in base_url and "localhost" not in base_url

    @staticmethod
    def _mask_url(url: str) -> str:
        """只保留 host 部分，避免把完整内部地址暴露给前端。"""
        if not url:
            return ""
        try:
            return url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            return url

    def _mock_response(self, messages: list[dict[str, str]], model: str | None = None) -> LLMResponse:
        """确定性 mock：同输入 → 同输出（用输入哈希生成）。

        特殊处理：当 System Prompt 含「政策依据」段（RAG 场景）时，返回**带引用的
        依据型回答** —— 让 T14/T19 等下游在 MOCK_MODE 下也能验证「引用编号 → 文档」
        的完整通路，而不是只回一句占位话术。
        """
        primary = model or (self._chain[0]["model"] if self._chain else settings.llm_model)
        prompt = "\n".join(m.get("content", "") for m in messages)
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]

        context, question = self._mock_split_context(messages)
        if context is not None:
            cite_ns = sorted({int(n) for n in re.findall(r"\[(\d+)\]", context)})
            cites = "".join(f"[{n}]" for n in cite_ns[:3]) or "[1]"
            head = context.strip().splitlines()[0][:40] if context.strip() else "相关政策"
            content = (
                f"[MOCK:{primary}] 根据已检索到的政策依据，关于「{question[:20]}」："
                f"相关规定以官方发布为准（依据要点：{head}…）{cites}。"
                f"（离线模拟回复，指纹 {digest}）"
            )
        else:
            content = (
                f"[MOCK:{primary}] 这是离线模拟回复（指纹 {digest}）。"
                "真实环境将基于检索到的政策语料作答，且必带引用与免责声明。"
            )
        return LLMResponse(
            content=content,
            model=primary,
            mocked=True,
            usage={"prompt_tokens": len(prompt), "completion_tokens": len(content), "total_tokens": 0},
        )

    @staticmethod
    def _mock_split_context(messages: list[dict[str, str]]) -> tuple[str | None, str]:
        """从 RAG 消息里拆出 (context, question)；非 RAG 场景返回 (None, "")。"""
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
        marker = "【政策依据】"
        if marker in system:
            ctx = system.split(marker, 1)[1].strip()
            return ctx, user
        return None, user


# 进程级单例（业务层用 get_llm_client() 获取）
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取全局 LLM 客户端（懒加载单例）。"""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
