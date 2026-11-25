#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文本分类骨架：**TextCNN 打底，规则降级**（2026-11-25 全哥定）。

## 一句话

给「软判断」一条统一接缝 —— 模型可用就走模型，不可用或没把握就走规则，
**分歧点留下来当判据该补的线索**。

## 三层结构

```
protocol.py   协议层：Prediction / TextClassifier —— 只依赖标准库
fallback.py   降级：关键词规则（永远可用，也是标签口径的唯一准绳）
textcnn.py    底座：TextCNN（torch 可选，CPU 分钟级可训）
router.py     路由：模型 ≥ 阈值走模型，否则降级；不一致时两边都留
dataset.py    语料：记忆月账 + 各机月账的条目（标签来自 [tag]）
train.py      训练与评估
```

## 硬边界（写在最显眼的地方）

**硬判据不许概率化。** 路径存在性、序号连续性、断链、版本集合相等是**事实判定**；
一旦变成「模型说有 92% 概率通过」，治理就失去了依据。
本包只服务软判断：**记忆条目的归类**（`docs/记录/记忆/` 的 `[tag]`）。

## 快速用

```python
from allin_governance._ml import Router

r = Router(model_path="model.pt")
print(r.describe())
for item in r.predict(["门禁 D19 必须由它自己拦住"]):
    print(item.final.label, item.final.source, item.disagreement)
```

## 依赖

torch **不在** `dependencies`，走额外组：

```bash
pip install 'allin-governance[ml]'
```

没装 torch 时本包仍可 import，`Router` 自动全程降级 —— 这是设计，不是残缺。
"""
from __future__ import annotations

from .fallback import LABELS, RuleClassifier, normalize_tag
from .protocol import (
    SOURCE_FALLBACK,
    SOURCE_TEXTCNN,
    Prediction,
    RouterStats,
    TextClassifier,
)
from .router import Router, RouteResult
from .textcnn import TORCH_AVAILABLE, TextCNNClassifier, tokenize

__all__ = [
    "LABELS", "Prediction", "TextClassifier", "RouterStats", "Router", "RouteResult",
    "RuleClassifier", "TextCNNClassifier", "normalize_tag", "tokenize",
    "TORCH_AVAILABLE", "SOURCE_TEXTCNN", "SOURCE_FALLBACK",
]
