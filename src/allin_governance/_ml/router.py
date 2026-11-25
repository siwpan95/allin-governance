#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路由层：**TextCNN 打底，规则降级** —— 全哥 2026-11-25 定的结构。

## 三条路由规则（不是简单 if/else）

| 情形 | 走谁 | 理由 |
| --- | --- | --- |
| 模型可用 且 置信度 ≥ `min_score` | **TextCNN** | 底座的正常输出 |
| 模型可用 但 置信度 < `min_score` | **规则** | 模型自己说没把握，别硬答 |
| 模型可用 但 与规则**不一致** | **两者都返回** | 这是最有价值的信号：说明这块知识模型和规则理解不同，值得人看一眼 |
| 模型不可用（没装 torch / 没训权重） | **规则** | 降级的兜底，永远可用 |

## 为什么「不一致」要暴露而不是二选一

如果静默选一个，就永远发现不了**模型学到了规则的盲区**（或反过来）。
allin 的经验是：**分歧点就是判据该补的地方**。所以 `RouteResult` 里
`disagreement=True` 时把两边的判断都带出来，供 `ml diagnose` 汇总。

## 一条纪律

本层**不碰硬判据**。调用方只在软判断场景（文档归类、失败归因）用它；
硬判据（D1–D19）继续走确定性实现，不经过这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .fallback import RuleClassifier
from .protocol import SOURCE_FALLBACK, SOURCE_TEXTCNN, Prediction, RouterStats
from .textcnn import TORCH_AVAILABLE, TextCNNClassifier


@dataclass
class RouteResult:
    """一次路由的完整结果 —— 两边的判断都留着，便于审计。"""

    final: Prediction
    model: Prediction | None = None
    rule: Prediction | None = None
    disagreement: bool = False

    def as_dict(self) -> dict:
        return {
            "final": self.final.as_dict(),
            "textcnn": self.model.as_dict() if self.model else None,
            "rule": self.rule.as_dict() if self.rule else None,
            "disagreement": self.disagreement,
        }


@dataclass
class Router:
    """把「底座 + 降级」的组合暴露成一个 `predict()`。"""

    model_path: str | None = None
    min_score: float = 0.55
    prefer_model: bool = True
    stats: RouterStats = field(default_factory=RouterStats)

    def __post_init__(self):
        self._rule = RuleClassifier()
        self._model = TextCNNClassifier(self.model_path, min_score=self.min_score) \
            if (self.prefer_model and TORCH_AVAILABLE) else None

    # -- 状态
    def model_available(self) -> bool:
        return bool(self._model and self._model.available())

    def describe(self) -> str:
        if not TORCH_AVAILABLE:
            return "底座不可用（torch 未安装）→ 全程规则降级"
        if self._model is None:
            return "底座被禁用（prefer_model=False）→ 全程规则降级"
        if not self._model.available():
            return f"底座未就绪（权重未找到：{self.model_path or '未指定'}）→ 全程规则降级"
        return f"底座就绪：TextCNN @ {self.model_path}（阈值 {self.min_score}）"

    # -- 推理
    def predict(self, texts: Sequence[str],
                paths: Sequence[str] | None = None) -> list[RouteResult]:
        rules = self._rule.predict(texts, paths)
        models: list[Prediction | None] = [None] * len(texts)
        if self.model_available():
            models = list(self._model.predict(texts))  # type: ignore[union-attr]

        results: list[RouteResult] = []
        for i, rule in enumerate(rules):
            model = models[i]
            if model is None or model.source != SOURCE_TEXTCNN:
                results.append(RouteResult(final=rule, rule=rule))
                continue
            disagree = model.label != rule.label
            if model.score < self.min_score:
                # 模型没把握：信规则，但把模型意见留档
                final = rule
            else:
                final = model
            results.append(RouteResult(final=final, model=model, rule=rule,
                                       disagreement=disagree))

        self.stats.record([r.final for r in results])
        return results
