"""POC-03 spike：laya 本地 System One 可得性验证（Day6 §4.3 步骤 A）。

独立于主项目运行（自带 .venv），验证三件事：
1. pip 可得性（已过：uv pip install laya==0.4.30 成功）
2. multilingual checkpoint 能否加载（HF 下载 pinned revision）
3. CPU 单句推理延迟（目标 <200ms，NFR-01）

用法：
    .venv/Scripts/python.exe spike_laya.py load    # 步骤 2：加载 + 单句延迟
    .venv/Scripts/python.exe spike_laya.py bench   # 步骤 3：50 条样例试跑
"""

from __future__ import annotations

import json
import statistics
import sys
import time

MODEL_ID = "convaiinnovations/laya-multilingual"  # 独立多语种仓库（PINNED_REVISIONS 对应项）
MULTILINGUAL_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"  # laya 0.4.30 pinned

# System One 意图标签（Day6 §4.3 契约枚举）
INTENTS = ["chat", "medication", "opera", "movie", "weather", "health", "other"]


def load_model():
    """加载 multilingual checkpoint，返回 (agent, load_ms)。"""
    import laya

    t0 = time.perf_counter()
    agent = laya.load(
        MODEL_ID,
        device="cpu",
        revision=MULTILINGUAL_REVISION,
        hooks=None,
        hooks_raise=False,
    )
    load_ms = (time.perf_counter() - t0) * 1000
    return agent, load_ms


def make_questions() -> dict:
    """最小意图分类 question schema（laya 原生 choice/noul 类型）。"""
    return {
        "intent": {
            "type": "choice",
            "instructions": "用户这句话的主要意图分类",
            "criteria": {
                "chat": "闲聊问候、陪伴交谈",
                "medication": "用药、吃药、服药相关",
                "opera": "想听戏曲（越剧/京剧等）",
                "movie": "想看电影、视频",
                "weather": "询问天气",
                "health": "询问健康状况、血压等健康数据",
                "other": "以上都不是",
            },
        },
        "is_sensitive": {
            "type": "noul",
            "instructions": "这句话是否涉及急症、跌倒、走失、情绪危机等需要工作人员立即介入的敏感情况",
        },
    }


def run_load() -> None:
    agent, load_ms = load_model()
    print(f"[load] multilingual checkpoint loaded in {load_ms:.0f} ms")
    print(f"[load] model_id: {agent.model_id}")

    questions = make_questions()
    text = "今天天气怎么样"
    # 冷启动首次推理
    t0 = time.perf_counter()
    out = agent.predict(text, questions, lang="zh")
    cold_ms = (time.perf_counter() - t0) * 1000
    print(f"[cold] {cold_ms:.1f} ms -> {json.dumps(out, ensure_ascii=False)[:200]}")

    # 预热后连测 20 句取 P50/P95
    lat: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        agent.predict(text, questions, lang="zh")
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    p50 = statistics.median(lat)
    p95 = lat[int(len(lat) * 0.95) - 1]
    print(f"[warm] n=20 P50={p50:.1f} ms  P95={p95:.1f} ms  min={lat[0]:.1f}  max={lat[-1]:.1f}")
    verdict = "PASS (<200ms)" if p95 < 200 else "FAIL (>=200ms)"
    print(f"[verdict] NFR-01 latency: {verdict}")


def run_bench() -> None:
    """50 条样例试跑：意图准确率（不含敏感句）+ 敏感召回率（spike 粗测口径）。"""
    samples = _load_samples()
    agent, load_ms = load_model()
    print(f"[load] {load_ms:.0f} ms")

    questions = make_questions()
    correct = 0
    intent_total = 0
    sens_hit = 0
    sens_total = 0
    lat: list[float] = []
    misses: list[dict] = []
    for s in samples:
        t0 = time.perf_counter()
        out = agent.predict(s["text"], questions, lang="zh")
        el = (time.perf_counter() - t0) * 1000
        lat.append(el)
        ans = out.get("answers", {})
        pred = str(ans.get("intent", {}).get("choice", "")).lower()
        # noul 答案键为 'noul'（为真概率），阈值 0.5
        pred_sens = float(ans.get("is_sensitive", {}).get("noul", 0.0)) > 0.5
        ok = pred == s["expected_intent"]
        if s["expected_intent"] is not None:
            intent_total += 1
            correct += ok
            if not ok:
                misses.append({"text": s["text"], "expected": s["expected_intent"], "predicted": pred})
        if s["expected_is_sensitive"]:
            sens_total += 1
            sens_hit += pred_sens
        flag = "OK  " if ok else "MISS"
        sens_mark = "  [SENS]" if s["expected_is_sensitive"] else ""
        print(f"{flag} [{pred:>10}] sens={pred_sens} {s['text'][:28]}  ({el:.0f}ms){sens_mark}")

    acc = correct / intent_total * 100 if intent_total else 0.0
    recall = (sens_hit / sens_total * 100) if sens_total else 0.0
    lat.sort()
    print("\n==== spike bench summary ====")
    print(
        f"intent accuracy : {correct}/{intent_total} = {acc:.1f}%  (target >=90%)"
    )
    print(f"sensitive recall: {sens_hit}/{sens_total} = {recall:.1f}%  (target >=95%)")
    print(
        f"latency P50={statistics.median(lat):.1f}ms  "
        f"P95={lat[int(len(lat)*0.95)-1]:.1f}ms  max={lat[-1]:.1f}ms"
    )
    if misses:
        print("intent misses:")
        for m in misses:
            print(f"  {m['text']}  expected={m['expected']} predicted={m['predicted']}")


def _load_samples() -> list[dict]:
    import pathlib

    p = pathlib.Path(__file__).with_name("samples_50.json")
    return json.loads(p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "load"
    if mode == "load":
        run_load()
    elif mode == "bench":
        run_bench()
    else:
        print("usage: spike_laya.py [load|bench]")
        sys.exit(2)
