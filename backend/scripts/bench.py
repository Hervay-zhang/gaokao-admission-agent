"""T30 · 压测：推荐/报告接口延迟分位数 + SSE 并发能力。

口径
----
- 功能正确性与性能**分开脚本**（任务卡「易混淆」点名）：本脚本只测性能，不做断言式业务校验；
- 延迟分位数取**端到端**（客户端发起 → 收到完整响应体），含网络与服务处理；
- 阈值（任务卡 J4）：推荐接口 P95 < 2s。

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/bench.py --base http://127.0.0.1:8000
    cd backend && ./.venv/Scripts/python.exe scripts/bench.py --requests 300 --concurrency 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx

REPORT_PATH = "/api/report"
FALLBACK_PATH = "/api/recommend/fallback"
CHAT_PATH = "/api/chat/stream"
PARAMS = {"score": 600, "subject_type": "物理", "province": "广东", "tier_limit": 10}


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank 分位数。"""
    if not sorted_vals:
        return 0.0
    k = max(1, int(len(sorted_vals) * pct / 100))
    return sorted_vals[min(k, len(sorted_vals)) - 1]


async def _hit(client: httpx.AsyncClient, path: str, params: dict) -> tuple[float, int]:
    t0 = time.perf_counter()
    r = await client.get(path, params=params)
    return time.perf_counter() - t0, r.status_code


async def _burst(client: httpx.AsyncClient, path: str, params: dict, n: int, conc: int) -> dict:
    sem = asyncio.Semaphore(conc)
    lat: list[float] = []
    codes: dict[int, int] = {}

    async def one() -> None:
        async with sem:
            el, code = await _hit(client, path, params)
            lat.append(el)
            codes[code] = codes.get(code, 0) + 1

    await asyncio.gather(*[one() for _ in range(n)])
    lat.sort()
    return {
        "n": n,
        "concurrency": conc,
        "codes": codes,
        "p50_ms": round(_percentile(lat, 50) * 1000, 1),
        "p95_ms": round(_percentile(lat, 95) * 1000, 1),
        "p99_ms": round(_percentile(lat, 99) * 1000, 1),
        "max_ms": round(lat[-1] * 1000, 1),
        "mean_ms": round(statistics.fmean(lat) * 1000, 1),
    }


async def _sse_one(client: httpx.AsyncClient, sid: str, q: str) -> tuple[str, float]:
    """单条 SSE 流，返回 (结果, 耗时秒)：done=成功 / limited=被限流(429或RATE_LIMITED) / error=异常。"""
    t0 = time.perf_counter()
    try:
        async with client.stream("GET", CHAT_PATH, params={"q": q, "session_id": sid}) as s:
            if s.status_code == 429:
                return "limited", time.perf_counter() - t0
            async for line in s.aiter_lines():
                if line.startswith("event: done"):
                    return "done", time.perf_counter() - t0
                if line.startswith("event: error"):
                    return "limited", time.perf_counter() - t0
    except httpx.HTTPError:
        return "error", time.perf_counter() - t0
    return "error", time.perf_counter() - t0


async def _sse_concurrency(client: httpx.AsyncClient, n: int) -> dict:
    """n 路并发 SSE：期望 ok + limited = n（超限连接被**并发连接限流**正确拦截，非缺陷）。"""
    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        _sse_one(client, f"bench_sse_{i}", f"平行志愿投档规则是怎样的？（并发{i}）")
        for i in range(n)
    ])
    elapsed = time.perf_counter() - t0
    done = [d for r, d in results if r == "done"]
    limited = sum(1 for r, _ in results if r == "limited")
    errors = sum(1 for r, _ in results if r == "error")
    return {
        "streams": n,
        "ok": len(done),
        "limited_by_design": limited,
        "errors": errors,
        "wall_seconds": round(elapsed, 2),
        "slowest_ok_seconds": round(max(done), 2) if done else None,
    }


async def _main_async(args: argparse.Namespace) -> dict:
    limits = httpx.Limits(max_connections=args.concurrency + args.sse_streams + 10,
                          max_keepalive_connections=args.concurrency + args.sse_streams + 10)
    async with httpx.AsyncClient(base_url=args.base, timeout=60.0, limits=limits) as client:
        report = await _burst(client, REPORT_PATH, PARAMS, args.requests, args.concurrency)
        fallback = await _burst(client, FALLBACK_PATH, PARAMS, args.requests, args.concurrency)
        # 压测后稍等再测 SSE（让限流窗口衰减，避免频率维度误伤）
        await asyncio.sleep(65)
        sse = await _sse_concurrency(client, args.sse_streams)
        return {"report": report, "fallback_recommend": fallback, "sse_concurrency": sse}


def main() -> int:
    parser = argparse.ArgumentParser(description="T30 压测（功能/性能分离）")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=200, help="每接口总请求数")
    parser.add_argument("--concurrency", type=int, default=20, help="并发数")
    parser.add_argument("--sse-streams", type=int, default=5,
                        help="SSE 并发流数（超过服务端并发连接上限的部分应被限流，计入 limited_by_design）")
    parser.add_argument("--json-out", default="", help="结果另存 JSON 路径（供报告引用）")
    args = parser.parse_args()

    result = asyncio.run(_main_async(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))

    p95_ms = result["report"]["p95_ms"]
    verdict = "PASS" if p95_ms < 2000 else "FAIL"
    print(f"\n推荐接口 P95 = {p95_ms} ms（阈值 < 2000 ms）→ {verdict}")

    if args.json_out:
        from pathlib import Path

        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json_out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys_exit = main()
    raise SystemExit(sys_exit)
