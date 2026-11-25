#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TextCNN —— 底座实现（Kim 2014 的原型，纯 torch，不依赖 transformers）。

## 结构（与论文一致的极简版）

```
tokens → embedding(n×d) → 三条并行 Conv1d(kernel=2,3,4) → 各自 max-pool
        → concat(3×filters) → dropout → linear → softmax
```

把词向量序列当成 `1×n` 的"图像"，用宽度为 2/3/4 的卷积核扫出 n-gram 特征 ——
这就是 TextCNN 的全部。没有 RNN、没有 attention、没有预训练。

## 为什么是它，不是 BERT

全哥 2026-11-25 定的：**TextCNN 做底座，规则做降级**。选它的理由不在效果，
在**可训练性**：

| | TextCNN | 微调 BERT |
| --- | --- | --- |
| 训练数据 | 几百到几千条 | 几万条起 |
| 训练时间（CPU） | 分钟级 | 小时级 |
| 显存 | 无（CPU 可跑） | ≥8G |
| 可解释性 | 能看哪个 n-gram 触发 | 难 |

在 allin 的语料规模（126 份文档 → 千级段落）下，BERT 会过拟合，
TextCNN 是**唯一能在 CPU 上真跑完并且能看出好坏**的选项。

## torch 是可选依赖

本模块顶部**不 import torch**（延迟到 `__init__`）。这样：
- 没装 torch 时，`_ml` 包仍可导入，`RuleClassifier` 照常工作；
- `TORCH_AVAILABLE` 用来给 `Router` 判断能否启用底座。

## 自包含的 tokenizer

不引入 jieba（会破坏零依赖）：中文按**字 + 双字**切，英文按词切。
字级粒度对中文文本分类够用，且完全可复现。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from .protocol import SOURCE_TEXTCNN, Prediction

try:  # torch 是**可选**依赖：缺失时本模块仍可导入，只是 available() 为 False
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    TORCH_AVAILABLE = True
except Exception:  # noqa: BLE001  任何导入失败都视为不可用（含 DLL 加载失败）
    TORCH_AVAILABLE = False


# ------------------------------------------------------------------ tokenizer
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]*")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_DIGIT = re.compile(r"\d+")


def tokenize(text: str, with_bigram: bool = True) -> list[str]:
    """字级 + 双字切分（中文）+ 词级（英文）+ 数字归一。

    双字是让卷积核**在字级别也能看到二元组合**（中文的"发版"、"门禁"这类词
    在双字里才成形）。这么做而不是上分词器，是为了零依赖 + 完全可复现。
    """
    tokens: list[str] = []
    for m in _ASCII_WORD.finditer(text):
        tokens.append(m.group(0).lower())
    cjk = [ch for ch in _CJK.findall(text)]
    tokens.extend(cjk)
    tokens.extend(_DIGIT.findall(text).__len__() and [] or [])  # 数字不进词表（防过拟合日期）
    if with_bigram and len(cjk) > 1:
        tokens.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


def build_vocab(texts: Sequence[str], max_size: int = 20000, min_count: int = 2) -> dict[str, int]:
    """按频次建词表（0 保留给 PAD）。"""
    from collections import Counter

    cnt: Counter[str] = Counter()
    for t in texts:
        cnt.update(tokenize(t))
    kept = [(w, c) for w, c in cnt.most_common(max_size) if c >= min_count]
    return {w: i + 1 for i, (w, _) in enumerate(kept)}


def encode(text: str, vocab: dict[str, int], max_len: int) -> list[int]:
    ids = [vocab[w] for w in tokenize(text) if w in vocab][:max_len]
    return ids + [0] * (max_len - len(ids))


# ---------------------------------------------------------------------- model
if TORCH_AVAILABLE:

    class TextCNN(nn.Module):
        """Kim 2014 的三核并行版。参数量在 10 万量级（词表 2 万 × 64 维）。"""

        def __init__(self, vocab_size: int, n_classes: int,
                     embed_dim: int = 64, n_filters: int = 64,
                     kernels: tuple[int, ...] = (2, 3, 4), dropout: float = 0.3):
            super().__init__()
            self.embedding = nn.Embedding(vocab_size, embed_dim, padding_idx=0)
            self.convs = nn.ModuleList(
                nn.Conv1d(embed_dim, n_filters, k) for k in kernels)
            self.dropout = nn.Dropout(dropout)
            self.fc = nn.Linear(n_filters * len(kernels), n_classes)

        def forward(self, x):                      # x: (B, L)
            e = self.embedding(x).transpose(1, 2)  # (B, d, L)
            pooled = [F.relu(c(e)).max(dim=2).values for c in self.convs]
            return self.fc(self.dropout(torch.cat(pooled, dim=1)))

else:  # torch 缺失时保留一个占位类，让 type 检查与文档不断裂
    class TextCNN:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            raise ImportError("torch 未安装：pip install 'allin-governance[ml]'")


# ------------------------------------------------------------------ 分类器封装
class TextCNNClassifier:
    """加载已训练权重并实现 `TextClassifier` 协议。

    权重文件是一个 `torch.save` 的 dict：`{"state_dict", "vocab", "labels", "max_len"}`。
    找不到权重时 `available()` 为 False，`Router` 自动降级到规则。
    """

    name = "textcnn"

    def __init__(self, model_path: str | Path | None = None,
                 min_score: float = 0.55, device: str = "cpu"):
        self.model_path = Path(model_path) if model_path else None
        self.min_score = min_score
        self.device = device
        self._model = None
        self._vocab: dict[str, int] = {}
        self._labels: tuple[str, ...] = ()
        self._max_len = 64

    # -- 生命周期
    def available(self) -> bool:
        if not TORCH_AVAILABLE or self.model_path is None or not self.model_path.is_file():
            return False
        return self._load() is not None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            blob = torch.load(self.model_path, map_location=self.device, weights_only=False)
            self._vocab = blob["vocab"]
            self._labels = tuple(blob["labels"])
            self._max_len = int(blob.get("max_len", 64))
            m = TextCNN(len(self._vocab) + 1, len(self._labels))
            m.load_state_dict(blob["state_dict"])
            m.eval()
            self._model = m
            return m
        except Exception:  # noqa: BLE001  权重损坏等同不可用，交给降级
            return None

    # -- 推理
    def predict(self, texts: Sequence[str]) -> list[Prediction]:
        model = self._load()
        if model is None:
            return [Prediction("未知", 0.0, "unavailable", "textcnn-not-loaded")
                    for _ in texts]
        ids = torch.tensor([encode(t, self._vocab, self._max_len) for t in texts])
        with torch.no_grad():
            probs = F.softmax(model(ids), dim=1)
        out: list[Prediction] = []
        for row in probs:
            score, idx = float(row.max()), int(row.argmax())
            out.append(Prediction(self._labels[idx], score, SOURCE_TEXTCNN,
                                  "below-threshold" if score < self.min_score else ""))
        return out
