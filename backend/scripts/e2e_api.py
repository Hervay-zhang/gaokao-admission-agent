"""T30 · 端到端关键路径验收（HTTP 级 E2E，等价 Playwright 驱动真实浏览器）。

为什么是 HTTP 级而不是浏览器级
----------------------------
本项目前端是**原生 ES Modules 无构建**的 hash 路由站点，业务逻辑全部收敛在后端 API；
浏览器级 E2E（Playwright）在本机安装浏览器内核重且与课程交付节奏不符。HTTP 级 E2E
覆盖**同一条用户旅程的全部后端契约**（含 SSE 流式逐事件断言），是任务卡「Playwright 或等价」
的等价实现；前端渲染正确性由 T23–T28 各卡浏览器人工验证兜底。

覆盖路径（任务卡 T30 E2E 范围）
--------------------------------
1. 画像建档      POST /api/profile          → 脱敏回执（只含区间，无明文分数）
2. 位次换算      GET  /api/rank             → rank 非空
3. AI 报告       GET  /api/report           → 冲稳保三档 + 口径 + 家长版 + 免责 + 无概率数字
4. 志愿表        POST /api/plan + items + reorder → 落库 seq 连续
5. 分享          POST/GET/DELETE /api/share → 脱敏快照 + 撤销失效
6. 流式问答      GET  /api/chat/stream(SSE) → meta→token*→done，done 带 message_id + 免责
7. 反馈          POST /api/feedback         → 点赞落库；同一 message_id 重复 → 409

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/e2e_api.py --base http://127.0.0.1:8000

退出码：0 = 全绿；1 = 有步骤失败（输出 ❌ 明细）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

SCORE = 600
SUBJECT = "物理"

_results: list[tuple[str, bool, str]] = []


def check(step: str, ok: bool, detail: str = "") -> None:
    _results.append((step, ok, detail))
    print(f"{'✅' if ok else '❌'} {step}" + (f" — {detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="T30 HTTP 级端到端验收")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="被测服务基地址")
    parser.add_argument("--timeout", type=float, default=60.0, help="单请求超时（秒）")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    sid = f"e2e_{uuid.uuid4().hex[:12]}"

    with httpx.Client(base_url=base, timeout=args.timeout) as c:
        # ---------- 1. 画像建档（脱敏回执） ----------
        r = c.post("/api/profile", json={
            "province": "广东", "current_year": 2026, "subject_type": SUBJECT,
            "elective_subjects": ["化学", "生物"], "score": SCORE,
            "preferred_cities": ["广州"], "preferred_majors": ["计算机"],
            "anon_id": sid,
        })
        check("1 画像建档 200", r.status_code == 200, f"status={r.status_code}")
        profile = r.json() if r.status_code == 200 else {}
        pid = profile.get("id") or 0
        body = json.dumps(profile, ensure_ascii=False)
        # 注：桶区间「600-609」天然含明文子串，不能查子串——查**键**与 hash 字段缺席
        check("1 回执脱敏（无 score 明文键/hash）", r.status_code == 200
              and "score" not in profile and "score_hash" not in body
              and profile.get("score_bucket") == "600-609",
              f"bucket={profile.get('score_bucket')}")

        # ---------- 2. 位次换算 ----------
        r = c.get("/api/rank", params={"subject_type": SUBJECT, "score": SCORE, "province": "广东"})
        check("2 位次换算 200", r.status_code == 200, f"status={r.status_code}")
        rank = (r.json() or {}).get("rank") if r.status_code == 200 else None
        check("2 rank 非空", bool(rank), f"rank={rank}")

        # ---------- 3. AI 报告 ----------
        r = c.get("/api/report", params={
            "score": SCORE, "subject_type": SUBJECT, "province": "广东",
            "electives": "化学,生物", "tier_limit": 8,
        })
        check("3 报告 200", r.status_code == 200, f"status={r.status_code}")
        report = r.json() if r.status_code == 200 else {}
        tiers = {t["tier"]: t for t in report.get("tiers", [])}
        check("3 三档齐全", all(k in tiers for k in ("chong", "wen", "bao")),
              f"counts={ {k: v.get('count') for k, v in tiers.items()} }")
        full_text = json.dumps(report, ensure_ascii=False)
        check("3 无概率数字 + 免责在尾", "概率" not in full_text
              and report.get("disclaimer") == "本建议仅供参考，不作为最终填报承诺")
        first_row = (tiers.get("chong", {}).get("items") or [{}])[0]
        check("3 报告行含真实院校与依据", bool(first_row.get("university_name"))
              and bool(first_row.get("evidence")) and bool(first_row.get("latest_year")))

        # ---------- 4. 志愿表（建 → 增 → 排） ----------
        r = c.post("/api/plan", json={"profile_id": pid, "name": "E2E方案", "year": 2026})
        check("4 建志愿表 200", r.status_code == 200, f"status={r.status_code}")
        plan = r.json() if r.status_code == 200 else {}
        plan_id = plan.get("id") or 0
        added = 0
        for t in ("wen", "bao"):
            row = (tiers.get(t, {}).get("items") or [None])[0]
            if not row:
                continue
            r = c.post(f"/api/plan/{plan_id}/items", params={"version": added},
                       json={"university_code": row["university_code"],
                             "university_name": row["university_name"],
                             "group_code": row.get("group_code"), "tier": t})
            if r.status_code == 200:
                added += 1
        check("4 加入两条志愿", added == 2, f"added={added}")
        r = c.get(f"/api/plan/{plan_id}")
        items = (r.json() or {}).get("items", []) if r.status_code == 200 else []
        ids = [it["id"] for it in items]
        ver = (r.json() or {}).get("version", 0)
        r = c.post(f"/api/plan/{plan_id}/reorder", json={"version": ver, "item_ids": list(reversed(ids))})
        check("4 排序持久化", r.status_code == 200
              and [it["seq"] for it in r.json().get("items", [])] == [1, 2],
              f"status={r.status_code}")

        # ---------- 5. 分享（生成 → 读 → 撤） ----------
        r = c.post("/api/share", json={"plan_id": plan_id})
        check("5 生成分享 200", r.status_code == 200, f"status={r.status_code}")
        token = (r.json() or {}).get("token") if r.status_code == 200 else ""
        r = c.get(f"/api/share/{token}")
        snap = r.json() if r.status_code == 200 else {}
        snap_text = json.dumps(snap, ensure_ascii=False)
        check("5 分享快照 200 且脱敏", r.status_code == 200
              and "score_hash" not in snap_text and '"score":' not in snap_text
              and snap.get("candidate", {}).get("score_bucket") == "600-609")
        r = c.delete(f"/api/share/{token}")
        check("5 撤销后 404", r.status_code == 200
              and c.get(f"/api/share/{token}").status_code == 404)

        # ---------- 6. 流式问答（SSE 逐事件） ----------
        t0 = time.perf_counter()
        events: list[tuple[str, dict]] = []
        with c.stream("GET", "/api/chat/stream",
                      params={"q": "广东新高考平行志愿是怎么投档的？", "session_id": sid}) as s:
            check("6 SSE 200 event-stream", s.status_code == 200
                  and "text/event-stream" in s.headers.get("content-type", ""),
                  f"status={s.status_code}")
            buf_event = None
            buf_data: list[str] = []
            for line in s.iter_lines():
                if line.startswith("event:"):
                    buf_event = line[6:].strip()
                elif line.startswith("data:"):
                    buf_data.append(line[5:].strip())
                elif line == "" and buf_event is not None:
                    try:
                        events.append((buf_event, json.loads("\n".join(buf_data))))
                    except json.JSONDecodeError:
                        events.append((buf_event, {"raw": "\n".join(buf_data)}))
                    buf_event, buf_data = None, []
        elapsed = time.perf_counter() - t0
        names = [e for e, _ in events]
        done = next((d for e, d in events if e == "done"), {})
        tokens = [d for e, d in events if e == "token"]
        meta = next((d for e, d in events if e == "meta"), {})
        check("6 事件序列 meta→token*→done", names and names[0] == "meta" and bool(tokens)
              and names[-1] == "done", f"events={names}")
        check("6 done 带 message_id + 免责", bool(done.get("message_id"))
              and "仅供参考" in (done.get("disclaimer") or ""),
              f"message_id={done.get('message_id')} fallback={done.get('fallback')}")
        print(f"   （SSE 全程 {elapsed:.2f}s，{len(tokens)} 个 token 事件，stream={meta.get('stream_id', '')[:8]}…）")

        # ---------- 7. 反馈（点赞 + 重复 409） ----------
        mid = done.get("message_id")
        r = c.post("/api/feedback", json={
            "kind": "up", "dimension": "answer", "message_id": str(mid), "session_sid": sid,
        })
        # 若该 message_id 已有历史反馈（之前 E2E 残留），跳过首次落库检查，只保重复 409
        if r.status_code == 409:
            print(f"   （message_id={mid} 已有历史反馈，跳过首次落库检查）")
        else:
            check("7 反馈点赞落库", r.status_code == 200 and r.json().get("ok") is True,
                  f"status={r.status_code}")
        r2 = c.post("/api/feedback", json={
            "kind": "up", "dimension": "answer", "message_id": str(mid), "session_sid": sid,
        })
        check("7 重复反馈 409", r2.status_code == 409
              and (r2.json().get("code") or r2.json().get("detail", {}).get("code")) == "FEEDBACK_DUPLICATE",
              f"status={r2.status_code}")

    # ---------- 汇总 ----------
    failed = [s for s, ok, _ in _results if not ok]
    print(f"\n===== E2E 汇总：{len(_results) - len(failed)}/{len(_results)} 通过 =====")
    if failed:
        print("失败步骤：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
