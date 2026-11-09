"""System One laya 本地适配器（Day6 P0-3，方案 A 裁决实装）。

要点（POC-03 spike 实证口径）：
- laya 依赖极重（torch ~1.1GB 模型），进程加载 31–34s 一次性成本：
  懒加载 + 后台线程加载（warmup），未就绪时 classify 抛 SystemOneUnavailable，
  cascade 捕获后跳过 ③ 直进 ④ 主控 Agent（可见降级，不静默）；
- 推理走 asyncio.to_thread + asyncio.wait_for(200ms 超时)；
  超时同上抛 SystemOneUnavailable("timeout")；
- laya import 延迟到 load 内部：未启用 system_one_provider=laya 时
  主项目不 import torch，应用照常启动（AGENTS 实时规则 6 同口径）；
- is_sensitive 输出仅作信号保留（契约字段），级联层不信任（方案 A）。
"""

from __future__ import annotations

import asyncio
import os
import threading

from app.system_one.questions import (
    LAYA_MODEL_ID,
    LAYA_REVISION,
    IntentLabel,
    SystemOneResult,
    build_laya_questions,
)

# noul 输出为「为真概率」，>0.5 判真（spike 实证键名）
SENSITIVE_NOUL_THRESHOLD = 0.5


class SystemOneUnavailable(Exception):
    """System One 不可用：未加载/加载失败/推理超时。cascade 捕获后跳到主控 Agent。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LayaProvider:
    """laya 本地分类器适配器：懒加载 + 超时保护 + 契约映射。"""

    def __init__(
        self,
        timeout_ms: int = 200,
        confidence_threshold: float = 0.85,
        model_id: str = LAYA_MODEL_ID,
        revision: str = LAYA_REVISION,
    ):
        self.timeout_ms = timeout_ms
        self.confidence_threshold = confidence_threshold
        self.model_id = model_id
        self.revision = revision
        self._agent = None
        self._load_error: str | None = None
        self._questions = build_laya_questions()
        self._lock = threading.Lock()

    # ---- 生命周期 ----

    @property
    def is_ready(self) -> bool:
        return self._agent is not None

    @property
    def model_version(self) -> str:
        return f"laya:{self.model_id}@{self.revision[:7]}"

    def warmup(self) -> None:
        """后台线程加载 checkpoint（一次性 31–34s），不阻塞应用启动。"""
        with self._lock:
            if self._agent is not None or self._load_error is not None:
                return

        def _load() -> None:
            try:
                self._agent = self._do_load()
            except Exception as exc:  # noqa: BLE001 — 加载失败必须可见降级
                self._load_error = f"{type(exc).__name__}: {exc}"

        threading.Thread(target=_load, name="laya-warmup", daemon=True).start()

    def _do_load(self):
        """真实加载 laya checkpoint（独立方法便于测试注入失败场景）。"""
        # hf-mirror 镜像兜底（内网/无外网直达 HF 时必需；已缓存时不产生下载）
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        import laya  # 延迟导入：未启用时主项目不背 torch

        return laya.load(
            self.model_id,
            device="cpu",
            revision=self.revision,
            hooks=None,
            hooks_raise=False,
        )

    @property
    def load_error(self) -> str | None:
        return self._load_error

    # ---- 推理 ----

    async def classify(self, text: str) -> SystemOneResult:
        """单句分类：返回契约结果；未就绪/超时抛 SystemOneUnavailable。"""
        if not self.is_ready:
            raise SystemOneUnavailable("model_not_loaded")
        try:
            out = await asyncio.wait_for(
                asyncio.to_thread(self._agent.predict, text, self._questions, lang="zh"),
                timeout=self.timeout_ms / 1000,
            )
        except TimeoutError:
            raise SystemOneUnavailable("timeout") from None
        return self._to_contract(out)

    # ---- laya 输出 → 契约映射 ----

    def _to_contract(self, out: dict) -> SystemOneResult:
        answers = out.get("answers", {})
        intent_ans = answers.get("intent", {})
        choice = str(intent_ans.get("choice", "other")).lower()
        try:
            intent = IntentLabel(choice)
        except ValueError:
            intent = IntentLabel.OTHER
        # confidence：laya 答案自带 confidence；缺失时取最大概率兜底
        confidence = float(intent_ans.get("confidence", 0.0))
        if confidence <= 0.0:
            probs = intent_ans.get("probabilities") or {}
            confidence = max((float(v) for v in probs.values()), default=0.0)
        noul = float(answers.get("is_sensitive", {}).get("noul", 0.0))
        return SystemOneResult(
            intent=intent,
            # 方案 A：仅作信号保留，级联层敏感判定只认 safety.py 词表
            is_sensitive=noul > SENSITIVE_NOUL_THRESHOLD,
            sensitive_types=[],
            confidence=confidence,
            needs_clarification=confidence < self.confidence_threshold,
            privacy_restricted=False,  # D8：契约就绪，行为留 Day7+
            is_out_of_domain=intent == IntentLabel.OTHER,
            secondary_intents=[],
            model_version=self.model_version,
        )
