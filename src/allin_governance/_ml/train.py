#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""训练 TextCNN（CPU 可跑，分钟级）。

## 用法

```bash
# 需要 torch：pip install 'allin-governance[ml]'
allin-governance ml train --docs <文档枢纽> --out model.pt
```

## 诚实的设计约束

1. **语料只有 ~359 条** → 训练集约 250 条、验证集约 60 条。
   5 类基线 20%，**准确率 45–65% 是正常预期**，70%+ 要怀疑泄漏。
2. 所以本脚本**默认不做早停**（没那么多数据可早停），固定 8 epoch + 报 val 曲线。
3. **每次训练都打印基线**（多数类占比）—— 准确率不高于基线就是没学到东西。
4. 训练完**必须**报混淆矩阵中「最大的一格」，让人看见它到底把什么当成了什么。

## 为什么不 import torch 在模块顶层

同 `textcnn.py`：没装 torch 时本模块仍可被 import（CLI 能给出友好提示），
而不是抛一个看不懂的 ImportError。
"""
from __future__ import annotations

import json
import pathlib
import random
from collections import Counter

from .dataset import load_memory, split, stats
from .fallback import LABELS
from .textcnn import TORCH_AVAILABLE, build_vocab, encode


def train(docs_root: str, out_path: str, epochs: int = 8, max_len: int = 96,
          embed_dim: int = 64, n_filters: int = 64, lr: float = 1e-3,
          batch_size: int = 16, seed: int = 42, quiet: bool = False) -> dict:
    """训练并保存权重。返回指标 dict。"""
    if not TORCH_AVAILABLE:
        return {"ok": False, "error": "torch 未安装：pip install 'allin-governance[ml]'"}

    import torch
    import torch.nn.functional as F

    from .textcnn import TextCNN, tokenize

    def say(*a):
        if not quiet:
            print(*a)

    samples = load_memory(docs_root)
    if not samples:
        return {"ok": False, "error": f"在 {docs_root} 下没抽到记忆条目（检查 记录/记忆/*.md）"}

    # 月账条目没有 tag（label=其他），**不参与训练**，避免把噪声当信号
    trainable = [s for s in samples if s.date]
    train_s, val_s = split(trainable)
    say(f"== 语料：抽到 {len(samples)} 条（可训 {len(trainable)}）→ "
        f"train {len(train_s)} / val {len(val_s)}")
    say(f"   训练集分布 {stats(train_s)}")
    say(f"   验证集分布 {stats(val_s)}")

    if len(train_s) < 50:
        return {"ok": False, "error": f"可训样本只有 {len(train_s)} 条，少于 50 不建议训练"}

    labels = [l for l in LABELS if any(s.label == l for s in train_s)]
    lab2idx = {l: i for i, l in enumerate(labels)}
    say(f"   类别（{len(labels)}）：{labels}")

    base = Counter(s.label for s in val_s).most_common(1)
    baseline = base[0][1] / max(1, len(val_s))
    say(f"   **基线（多数类占比）= {baseline:.3f}** —— 准确率不高于它就是没学到东西")

    vocab = build_vocab([s.text for s in train_s], max_size=20000, min_count=2)
    say(f"   词表 {len(vocab)}（tokenizer: 字+双字，零依赖）")

    def to_tensor(items):
        xs = torch.tensor([encode(s.text, vocab, max_len) for s in items])
        ys = torch.tensor([lab2idx[s.label] for s in items])
        return xs, ys

    xtr, ytr = to_tensor(train_s)
    xva, yva = to_tensor(val_s)

    torch.manual_seed(seed)
    model = TextCNN(len(vocab) + 1, len(labels), embed_dim=embed_dim,
                    n_filters=n_filters, kernels=(2, 3, 4), dropout=0.3)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    n_param = sum(p.numel() for p in model.parameters())
    say(f"   模型参数量 {n_param:,}")

    curve = []
    for ep in range(1, epochs + 1):
        model.train()
        idx = list(range(len(train_s)))
        random.Random(seed + ep).shuffle(idx)
        total = 0.0
        for i in range(0, len(idx), batch_size):
            b = idx[i:i + batch_size]
            opt.zero_grad()
            loss = F.cross_entropy(model(xtr[b]), ytr[b])
            loss.backward()
            opt.step()
            total += float(loss) * len(b)
        tr_loss = total / max(1, len(train_s))

        model.eval()
        with torch.no_grad():
            pred = model(xva).argmax(dim=1)
        acc = float((pred == yva).float().mean()) if len(val_s) else 0.0
        curve.append({"epoch": ep, "train_loss": round(tr_loss, 4), "val_acc": round(acc, 4)})
        say(f"   epoch {ep:2d}  loss={tr_loss:.4f}  val_acc={acc:.3f}")

    # 混淆矩阵（只打印非零项里最大的几格）
    conf: Counter = Counter()
    model.eval()
    with torch.no_grad():
        pred = model(xva).argmax(dim=1).tolist()
    for p, s in zip(pred, val_s):
        conf[(s.label, labels[p])] += 1
    conf_rows = [{"true": k[0], "pred": k[1], "n": v}
                 for k, v in sorted(conf.items(), key=lambda x: -x[1]) if k[0] != k[1]]
    if conf_rows:
        say("   混淆（真 → 判）：" + " / ".join(
            f"{r['true']}→{r['pred']} {r['n']}" for r in conf_rows[:5]))
    else:
        say("   混淆：无（全部判对 —— 样本这么少的情况下要警惕泄漏）")

    blob = {"state_dict": model.state_dict(), "vocab": vocab, "labels": labels,
            "max_len": max_len,
            "meta": {"samples": len(train_s), "val": len(val_s), "baseline": round(baseline, 4),
                     "final_val_acc": curve[-1]["val_acc"], "curve": curve,
                     "corpus": "docs/记录/记忆 + 各机月账（月账无 tag 不入训）"}}
    torch.save(blob, out_path)
    say(f"\n== 已存 {out_path}（val_acc={curve[-1]['val_acc']:.3f}，基线 {baseline:.3f}）")

    return {"ok": True, "path": out_path, "labels": labels,
            "train": len(train_s), "val": len(val_s), "baseline": round(baseline, 4),
            "val_acc": curve[-1]["val_acc"], "curve": curve, "confusions": conf_rows[:8],
            "params": n_param, "vocab": len(vocab)}


def evaluate(model_path: str, docs_root: str, quiet: bool = False) -> dict:
    """用已训权重在**全部可训样本**上跑一遍，报「模型 vs 规则」的一致率。

    一致率不是准确率 —— 它回答的是「底座和降级分歧多大」，
    分歧处正是判据该补的地方。
    """
    if not TORCH_AVAILABLE:
        return {"ok": False, "error": "torch 未安装"}
    import torch

    from .fallback import RuleClassifier
    from .textcnn import TextCNNClassifier

    samples = [s for s in load_memory(docs_root) if s.date]
    clf = TextCNNClassifier(model_path)
    if not clf.available():
        return {"ok": False, "error": f"权重不可用：{model_path}"}
    texts = [s.text for s in samples]
    preds = clf.predict(texts)
    rules = RuleClassifier().predict(texts)
    agree = sum(1 for a, b in zip(preds, rules) if a.label == b.label)
    acc_model = sum(1 for p, s in zip(preds, samples) if p.label == s.label)
    acc_rule = sum(1 for p, s in zip(rules, samples) if p.label == s.label)
    n = max(1, len(samples))
    result = {"ok": True, "n": len(samples),
              "model_acc": round(acc_model / n, 4), "rule_acc": round(acc_rule / n, 4),
              "agreement": round(agree / n, 4)}
    if not quiet:
        print(f"== 评估（{len(samples)} 条）")
        print(f"   TextCNN 准确率 {result['model_acc']:.3f}")
        print(f"   规则降级准确率 {result['rule_acc']:.3f}")
        print(f"   两者一致率   {result['agreement']:.3f}（分歧 {len(samples) - agree} 条）")
        print("   → 分歧处是判据该补的地方，不是模型该背的锅")
    return result
