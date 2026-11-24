"""T33 · 图形验证码服务（防批量注册/撞库）。

目标
----
开放注册后，任何人都能脚本批量建号（灌垃圾数据、薅 LLM 额度）。本服务提供**服务端图形验证码**：
注册必须携带正确的 ``captcha_id + captcha_code``，否则拒绝。

设计
----
- **必须出位图（PNG），不能出 SVG**：SVG 里字符是 ``<text>A7K9</text>``，脚本能直接正则取出答案，
  验证码形同虚设。故首选 **Pillow 渲染 PNG**（逐字符随机旋转/字号/配色 + 干扰线 + 噪点），
  仅在 Pillow 不可用时**降级 SVG**（此时防脚本能力弱，仅防最朴素的重复提交）；
- **服务端只存答案**：Redis 存 ``captcha:{id} -> code``（TTL 默认 300 秒），**明文不下发**；
- **一次性**：校验通过即删除，防止同一张码被重放（无论校验成功与否都作废，防暴力枚举）；
- **大小写不敏感**：用户输入体验优先（字符集已剔除 0/O/1/I/l 等易混字符）；
- **可注入存储**：``CaptchaStore`` Protocol —— 生产 Redis，测试内存，单测离线可复现；
- **Redis 故障降级内存**：验证码是防护而非核心链路，Redis 异常时退回进程内存，
  保证「Redis 挂了也能注册」，仅记 warning（与限流 fail-open 口径一致）。

红线
----
- 验证码答案**绝不返回给前端**（渲染用 SVG 里是图形化的字符，非结构化文本）；
- 阈值/开关/TTL **不得硬编码** —— 一律从 ``settings`` 读取（``captcha_enabled`` 等）；
- 关闭开关（``captcha_enabled=false``，如本地联调）时**校验直接放行**，不得误伤正常注册。
"""

from __future__ import annotations

import base64
import io
import random
import secrets
import time
from typing import Any, Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 字符集：剔除 0/O/1/I/l 等易混淆字符（人工辨识 + OCR 都更容易出错）
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 4

# SVG 画布
WIDTH = 120
HEIGHT = 44

# 干扰配色（墨蓝 / 稳 / 保 / 朱砂的浅色变体，贴合站点视觉）
_INK_COLORS = ("#1E3A5F", "#2C4A6B", "#B98A2F", "#2E7D5B", "#C0392B", "#4A5D7E")


def _random_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def render_png_b64(code: str) -> str:
    """渲染成 PNG（base64，不含 ``data:`` 前缀）。

    逐字符绘制在透明小图上再旋转粘贴，配合干扰线与噪点，提高机器识别成本。
    Pillow 内置位图字体（``load_default``）已覆盖 ASCII，无需额外字体文件。
    """
    from PIL import Image, ImageDraw, ImageFont  # 局部导入：Pillow 缺失时降级 SVG 而非启动失败

    rng = random.SystemRandom()
    image = Image.new("RGB", (WIDTH, HEIGHT), "#F4F1EC")
    draw = ImageDraw.Draw(image)

    # 干扰线（字符之下）
    for _ in range(4):
        draw.line(
            [(rng.randint(0, WIDTH), rng.randint(2, HEIGHT - 2)), (rng.randint(0, WIDTH), rng.randint(2, HEIGHT - 2))],
            fill=rng.choice(_INK_COLORS),
            width=1,
        )
    # 噪点
    for _ in range(70):
        draw.point((rng.randint(1, WIDTH - 1), rng.randint(1, HEIGHT - 1)), fill=rng.choice(_INK_COLORS))

    slot = WIDTH / (len(code) + 1)
    for i, ch in enumerate(code):
        size = rng.randint(26, 32)
        font = ImageFont.load_default(size=size)
        tile = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        tile_draw = ImageDraw.Draw(tile)
        tile_draw.text((size // 2, size // 2), ch, font=font, fill=rng.choice(_INK_COLORS))
        tile = tile.rotate(rng.randint(-25, 25), resample=Image.BICUBIC)
        bbox = tile.getbbox()
        if bbox:
            tile = tile.crop(bbox)
        x = int(slot * (i + 1) - tile.width / 2)
        y = int(HEIGHT / 2 - tile.height / 2 + rng.randint(-3, 3))
        image.paste(tile, (x, y), tile)  # 第三参数=tile 自身作 alpha mask

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def render_image(code: str) -> tuple[str, str]:
    """返回 ``(data_url, 格式)``；优先 PNG，Pillow 不可用则降级 SVG。"""
    try:
        return f"data:image/png;base64,{render_png_b64(code)}", "png"
    except Exception as exc:  # noqa: BLE001 - 无 Pillow / 渲染失败 → 降级
        logger.warning("验证码 PNG 渲染失败，降级 SVG：%r", exc)
        encoded = render_svg(code).replace("\n", "")
        return f"data:image/svg+xml;charset=utf-8,{_quote(encoded)}", "svg"


def _quote(raw: str) -> str:
    from urllib.parse import quote

    return quote(raw, safe="")


def render_svg(code: str) -> str:
    """把验证码文本渲染成 SVG（含旋转、干扰线、噪点）。

    说明：字符取自固定白名单 ``ALPHABET``，不含 ``<>&`` 等字符，无需额外 HTML 转义。
    """
    rng = random.SystemRandom()
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="图形验证码">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#F4F1EC" rx="4"/>',
    ]

    # 干扰线（在字符之下）
    for _ in range(3):
        x1 = rng.randint(0, WIDTH)
        y1 = rng.randint(4, HEIGHT - 4)
        x2 = rng.randint(0, WIDTH)
        y2 = rng.randint(4, HEIGHT - 4)
        color = rng.choice(_INK_COLORS)
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{color}" stroke-width="1" stroke-opacity="0.35"/>'
        )

    # 噪点
    for _ in range(28):
        cx = rng.randint(2, WIDTH - 2)
        cy = rng.randint(2, HEIGHT - 2)
        color = rng.choice(_INK_COLORS)
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="1" fill="{color}" fill-opacity="0.35"/>')

    # 字符：逐个随机旋转、位移、字号与颜色
    slot = WIDTH / (len(code) + 1)
    for i, ch in enumerate(code):
        x = slot * (i + 1)
        y = HEIGHT / 2 + rng.randint(-3, 4)
        angle = rng.randint(-22, 22)
        size = rng.randint(22, 28)
        color = rng.choice(_INK_COLORS)
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-family="Georgia,serif" '
            f'font-weight="700" fill="{color}" text-anchor="middle" '
            f'transform="rotate({angle} {x:.1f} {y:.1f})">{ch}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# ============================================================
#  存储（可注入）
# ============================================================


class CaptchaStore(Protocol):
    """验证码答案存储接口。"""

    async def save(self, captcha_id: str, code: str, ttl: int) -> None: ...
    async def take(self, captcha_id: str) -> str | None:
        """取出并**立即作废**该验证码（一次性），不存在/已过期返回 ``None``。"""
        ...


class InMemoryCaptchaStore:
    """内存实现（测试 / Redis 降级兜底）。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}

    async def save(self, captcha_id: str, code: str, ttl: int) -> None:
        self._purge()
        self._data[captcha_id] = (code, time.time() + ttl)

    async def take(self, captcha_id: str) -> str | None:
        self._purge()
        item = self._data.pop(captcha_id, None)
        return item[0] if item else None

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, (_, exp) in self._data.items() if exp <= now]:
            self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


class RedisCaptchaStore:
    """Redis 实现（生产，``redis.asyncio`` 客户端）。

    - 键 ``captcha:{id}``，写入即带 TTL；
    - ``take`` 用 GETDEL 语义（先 GET 再 DEL），保证一次性；
    - **异常向上抛出**，由 ``CaptchaService`` 捕获后降级内存。
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client

    def _client(self) -> Any:
        if self._redis is None:
            from app.core.redis import get_redis

            self._redis = get_redis()
        return self._redis

    async def save(self, captcha_id: str, code: str, ttl: int) -> None:
        client = self._client()
        await client.set(f"captcha:{captcha_id}", code, ex=max(30, ttl))

    async def take(self, captcha_id: str) -> str | None:
        client = self._client()
        key = f"captcha:{captcha_id}"
        code = await client.get(key)
        await client.delete(key)  # 无论对错都作废，防重放/枚举
        if not code:
            return None
        return code.decode() if isinstance(code, bytes) else str(code)


# ============================================================
#  服务
# ============================================================


class CaptchaService:
    """验证码签发与校验。

    :param store: 主存储（默认 Redis）
    :param fallback: 降级存储（默认内存）
    :param settings_obj: 配置对象（默认全局 ``settings``，便于测试替换）
    """

    def __init__(
        self,
        store: CaptchaStore | None = None,
        *,
        fallback: CaptchaStore | None = None,
        settings_obj: Any | None = None,
    ) -> None:
        self.store: CaptchaStore = store if store is not None else RedisCaptchaStore()
        self.fallback: CaptchaStore = fallback if fallback is not None else InMemoryCaptchaStore()
        self.s = settings_obj or settings

    @property
    def ttl(self) -> int:
        return max(30, int(self.s.captcha_ttl_seconds))

    @property
    def enabled(self) -> bool:
        return bool(self.s.captcha_enabled)

    async def issue(self, *, length: int = CODE_LENGTH) -> dict[str, Any]:
        """签发一张验证码：返回 ``captcha_id`` 与 SVG（**不含答案**）。"""
        code = _random_code(length)
        captcha_id = secrets.token_urlsafe(16)
        try:
            await self.store.save(captcha_id, code, self.ttl)
        except Exception as exc:  # noqa: BLE001 - Redis 故障 → 降级内存
            logger.warning("验证码主存储不可用，降级内存：%r", exc)
            await self.fallback.save(captcha_id, code, self.ttl)
        data_url, fmt = render_image(code)
        return {
            "ok": True,
            "captcha_id": captcha_id,
            "data_url": data_url,
            "format": fmt,
            "ttl": self.ttl,
        }

    async def verify(self, captcha_id: str, user_input: str) -> bool:
        """校验用户输入；**无论对错该验证码立即作废**（一次性）。

        开关关闭时直接放行（本地联调 / 无障碍场景）。
        """
        if not self.enabled:
            return True
        if not captcha_id or not user_input:
            return False

        expected: str | None = None
        try:
            expected = await self.store.take(captcha_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("验证码主存储读取失败，回退内存：%r", exc)
        if expected is None:
            try:
                expected = await self.fallback.take(captcha_id)
            except Exception:  # noqa: BLE001
                expected = None
        if expected is None:
            return False
        # 大小写不敏感；字符集无空白字符，仅 strip 防粘贴带空格
        return expected.upper() == user_input.strip().upper()


_captcha_service: CaptchaService | None = None


def get_captcha_service() -> CaptchaService:
    """获取全局验证码服务单例（生产 Redis；测试可用 ``set_captcha_service`` 注入内存实现）。"""
    global _captcha_service
    if _captcha_service is None:
        _captcha_service = CaptchaService()
    return _captcha_service


def set_captcha_service(service: CaptchaService | None) -> None:
    """替换全局验证码服务（测试用）。"""
    global _captcha_service
    _captcha_service = service


__all__ = [
    "ALPHABET",
    "CaptchaService",
    "CaptchaStore",
    "InMemoryCaptchaStore",
    "RedisCaptchaStore",
    "get_captcha_service",
    "render_svg",
    "set_captcha_service",
]
