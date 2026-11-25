#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""降级实现：**关键词规则分类器**（现有确定性逻辑，包成协议形状）。

它是 `TextCNN` 不可用时的兜底，也是标签口径的**唯一准绳** ——
两者类别名必须完全一致，否则「底座 + 降级」会给出互相矛盾的结果。

## 标签口径从哪来

`docs/记录/记忆/*.md` 的条目都带 `[tag]`（359 条 / 167 种）。167 类没法训，
所以按 `·` 前半段**归一成 5 类**（实测分布：governance 120 / agent 90 /
tooling 56 / 其他 47 / fact 24）。这套归一化不是拍的，是**语料逼出来的**。

## 这层存在的意义（不是过渡产物）

即使 TextCNN 训好了，这层也不能删：
- 权重丢了 / torch 没装 → 仍能分类；
- 模型置信度低 → 回落到规则；
- **两者不一致时是最有价值的信号**（说明模型学到的东西和规则理解不同，
  那个分歧点往往就是判据该补的地方）。
"""
from __future__ import annotations

import re
from typing import Sequence

from .protocol import SOURCE_FALLBACK, Prediction, TextClassifier

#: 类别名 —— **唯一口径**，TextCNN 的标签集合必须与它完全一致。
#: 由 `docs/记录/记忆/` 的 167 种 tag 归一化而来（保留样本量 ≥20 的 4 类 + 其他）。
LABELS: tuple[str, ...] = ("governance", "agent", "tooling", "fact", "其他")

#: 关键词规则（降级实现）。每条 (正则, 类别, 权重)。
#: 这些词是从 359 条记忆条目里**读出来**的高频判别词，不是凭空写的。
def normalize_tag(raw: str) -> str:
    """把 `[governance·判据]` 这类细分 tag 归一成本包的 5 类。

    与 `LABELS` 同一口径 —— **训练标签必须经过这个函数**，
    否则模型学的是 167 类、规则给的是 5 类，永远对不上。
    """
    head = raw.split("·")[0].strip().lower()
    return head if head in LABELS else "其他"


_TEXT_RULES: tuple[tuple[re.Pattern, str, float], ...] = (
    (re.compile(r"门禁|判据|交接|治理|台账|黄金集|D\d\d|registry", re.I), "governance", 0.85),
    (re.compile(r"agent|记忆|对话|提示词|模型|token|推理", re.I), "agent", 0.8),
    (re.compile(r"脚本|工具|命令|管道|编码|shell|bash|pytest|构建", re.I), "tooling", 0.8),
    (re.compile(r"盘符|磁盘|F 盘|G 盘|D 盘|hostname|本机|设备|检出", re.I), "fact", 0.75),
)


class RuleClassifier:
    """把仓库现有的分类约定包成 `TextClassifier` 协议。"""

    name = "rule"

    def available(self) -> bool:
        return True  # 纯标准库，永远可用

    def predict(self, texts: Sequence[str],
                paths: Sequence[str] | None = None) -> list[Prediction]:
        out: list[Prediction] = []
        for i, text in enumerate(texts):
            path = paths[i] if paths and i < len(paths) else ""
            out.append(self._one(text, path))
        return out

    def _one(self, text: str, path: str = "") -> Prediction:
        hits: list[tuple[str, float]] = []
        for rx, label, w in _TEXT_RULES:
            if rx.search(text):
                hits.append((label, w))
        if hits:
            label, w = max(hits, key=lambda x: x[1])
            return Prediction(label, w, SOURCE_FALLBACK, f"kw:{label}")
        return Prediction("其他", 0.0, SOURCE_FALLBACK, "no-rule-matched")
