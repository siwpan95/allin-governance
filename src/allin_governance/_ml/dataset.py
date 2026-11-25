#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语料构建：把**记忆月账与各机月账**的条目变成分类样本。

## 为什么语料源是它俩，而不是 `docs/规范/`

先说清选型（2026-11-25 全哥点出来的）：

| 语料源 | 标签来源 | 标签质量 | **但价值** |
| --- | --- | --- | --- |
| `docs/规范/事实源/记录` | 路径 | 高 | ❌ 低 —— 按路径归类已有确定性判据，模型是多余的 |
| **`docs/记录/记忆/*.md`** | 条目里的 `[tag]` | **人工标的，高** | ✅ 高 —— agent 自己写的、会膨胀、会重复 |
| **`docs/事实源/机器/*/月账/*.md`** | 无 tag | —— | ✅ 高（但只有 28 条，暂不可训） |

真正的痛点是：**记忆是 agent 自己产出的，它只会越写越多**（已 359 条），
其中「真教训 / 流水账 / 和上月重复」混在一起没有任何判据能分。
这才是需要模型的场景。

## 标签怎么来的（免费但质量高）

条目形如 `- **2026-11-25 00:15** [docs·判据递进] **标题**：正文`。
`[tag]` 是写的时候人工标的 —— **天然监督信号**。
359 条有 167 种 tag（平均每类 2 条，没法训），所以经 `normalize_tag` 归一到 5 类：
governance 120 / agent 90 / tooling 56 / 其他 47 / fact 24。

## 已知局限（必须写在这里，不写就是骗人）

1. **359 条太少**。训出来的模型准确率不会高（5 类基线 20%），
   它的价值是**验证 pipeline 通不通**，以及**暴露"数据不够"这个事实**。
2. **月账只有 28 条**，暂时不进训练集，但要一起抽 —— 它是"未来语料"的入口。
3. 记忆条目里有**大量重复**（同一件事在不同月份反复记），
   这会让训练集泄漏（同一条的变体同时进 train 和 val）。见 `split()` 的去重说明。
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

from .fallback import normalize_tag

#: 条目行：`- **<日期 时间>** [tag] 正文…`（tag 可能在或不在）
_ENTRY = re.compile(r"^- \*\*(20\d\d-\d\d-\d\d[^*]*)\*\*\s*(?:\[([^\]]+)\])?\s*(.+)$")

#: 太短的条目没有判别信息（"已完成"、"见上"），剔掉
MIN_CHARS = 40


@dataclass
class Sample:
    text: str
    label: str
    src: str          # 来源文件（相对路径，便于人工回查）
    date: str         # 条目日期，用于按时间切分


def _strip_md(s: str) -> str:
    """去掉 markdown 装饰：标题星号、行内代码、链接地址。保留文字本身。"""
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)   # 链接 → 文字
    s = re.sub(r"[*`_]{1,3}", "", s)                  # 强调/代码标记
    s = re.sub(r"<[^>]+>", "", s)                     # 内联 HTML
    return re.sub(r"\s+", " ", s).strip()


def load_memory(docs_root: str | pathlib.Path, with_monthly: bool = True) -> list[Sample]:
    """抽记忆条目 + 各机月账条目。

    Args:
        docs_root: 文档枢纽根（含 `记录/`、`事实源/`）。
        with_monthly: 是否连月账一起抽（月账无 tag → 标 `其他`，默认只抽不用）。

    Returns:
        `Sample` 列表。无 tag 的月账条目 `label="其他"`。
    """
    root = pathlib.Path(docs_root)
    # 两种传法都接受：文档枢纽根（含 `记录/`）或仓根（含 `docs/记录/`）。
    # 传错一层就会静默抽到 0 条 —— 这种"安静的空结果"最坑，所以显式兜一层。
    if not (root / "记录").is_dir() and (root / "docs" / "记录").is_dir():
        root = root / "docs"
    out: list[Sample] = []

    mem = root / "记录" / "记忆"
    if mem.is_dir():
        for f in sorted(mem.glob("*.md")):
            for ln in f.read_text(encoding="utf-8", errors="replace").split("\n"):
                m = _ENTRY.match(ln)
                if not m:
                    continue
                date, tag, body = m.group(1), m.group(2), m.group(3)
                text = _strip_md(body)
                if len(text) < MIN_CHARS:
                    continue
                out.append(Sample(text, normalize_tag(tag) if tag else "其他",
                                  f"记录/记忆/{f.name}", date))

    if with_monthly:
        for f in sorted((root / "事实源" / "机器").glob("*/月账/*.md")):
            host = f.parent.parent.name
            for ln in f.read_text(encoding="utf-8", errors="replace").split("\n"):
                if not ln.startswith(("- ", "* ")):
                    continue
                text = _strip_md(ln[2:])
                if len(text) < MIN_CHARS:
                    continue
                # 月账条目没有 tag —— 用 `其他` 占位，**不参与训练**（见 split 的说明）
                out.append(Sample(text, "其他", f"事实源/机器/{host}/月账/{f.name}", ""))

    return out


def split(samples: list[Sample], val_ratio: float = 0.2,
          seed: int = 42) -> tuple[list[Sample], list[Sample]]:
    """按标签分层切分 train/val。

    ⚠ **去重**：记忆里同一件事常被记两次（不同月份），若不去重，
    同一条的变体会同时落进 train 和 val，val 准确率会虚高。
    这里按「归一化文本前 60 字」去重，保留最早那条。
    """
    import random

    seen: dict[str, Sample] = {}
    for s in samples:
        key = s.text[:60]
        if key not in seen or (s.date and s.date < seen[key].date):
            seen[key] = s
    uniq = sorted(seen.values(), key=lambda x: (x.date, x.text))

    by_label: dict[str, list[Sample]] = {}
    for s in uniq:
        by_label.setdefault(s.label, []).append(s)

    rnd = random.Random(seed)
    train: list[Sample] = []
    val: list[Sample] = []
    for label, items in by_label.items():
        rnd.shuffle(items)
        k = max(1, int(len(items) * val_ratio)) if len(items) > 4 else 0
        val.extend(items[:k])
        train.extend(items[k:])
    return train, val


def stats(samples: list[Sample]) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(s.label for s in samples).most_common())
