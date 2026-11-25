#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文本分类骨架的**协议层** —— 只依赖标准库，不 import torch。

## 为什么先有协议层

2026-11-25 全哥定：**TextCNN 做底座，现有实现做降级**。
但「底座」要成立，必须先有一条**与实现无关的接缝**，否则换模型 = 改调用方。

所以这里只定义两样东西：

1. `Prediction` —— 一次分类的结果（label + score + 来源），**来源必须可见**：
   `source="textcnn"` 还是 `"fallback"`。治理系统的输出不许说不清是谁给的。
2. `TextClassifier` —— 协议。任何实现只要满足 `predict(texts) -> list[Prediction]` 就能插进来。

## 一条硬边界（写在最前面，防止骨架搭错层）

**硬判据不许概率化。** 路径存在性、序号连续性、断链、版本集合相等 —— 这些是**事实判定**，
一旦变成「模型说有 92% 概率通过」，治理就失去了依据。
本包只服务**软判断**：文档该归哪一类、失败像哪一类问题、这段和那段是不是在说同一件事。

`Router` 会强制检查这一点以外的场景由调用方自己把关 —— 骨架不做越权判断。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

#: 实现来源标识 —— 输出里必须带，用于审计「这条结论是谁给的」
SOURCE_TEXTCNN = "textcnn"
SOURCE_FALLBACK = "fallback"
SOURCE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Prediction:
    """一次分类结果。

    Attributes:
        label: 类别名（如 `规范` / `记录`）。
        score: 置信度 0–1。**fallback 实现给的是「规则命中强度」，不是概率** ——
            两者不可比，`source` 字段就是用来区分这个的。
        source: `textcnn` / `fallback` / `unavailable`。
        detail: 可选补充（fallback 会写命中的规则名，便于解释）。
    """

    label: str
    score: float
    source: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"label": self.label, "score": round(self.score, 4),
                "source": self.source, "detail": self.detail}


@runtime_checkable
class TextClassifier(Protocol):
    """分类器协议。实现它就能被 `Router` 用。"""

    name: str

    def available(self) -> bool:
        """本实现当前能否用（模型权重在不在、torch 装没装）。"""
        ...

    def predict(self, texts: Sequence[str]) -> list[Prediction]:
        """批量分类。返回长度必须与 `texts` 一致。"""
        ...


@dataclass
class RouterStats:
    """路由统计：一轮里多少次走了模型、多少次降级。"""

    total: int = 0
    by_source: dict[str, int] = field(default_factory=dict)

    def record(self, preds: Sequence[Prediction]) -> None:
        self.total += len(preds)
        for p in preds:
            self.by_source[p.source] = self.by_source.get(p.source, 0) + 1

    def summary(self) -> str:
        if not self.total:
            return "未分类任何文本"
        parts = [f"{k} {v}" for k, v in sorted(self.by_source.items())]
        return f"{self.total} 条：{' / '.join(parts)}"


def env_flag(name: str, default: bool = False) -> bool:
    """读布尔环境变量（`1/true/yes/on` 为真）。用于骨架的开关与调试。"""
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}
