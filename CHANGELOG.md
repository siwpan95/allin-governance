# 变更记录

## 2.0.2 —— 2026-11-25

**包内形态的门禁入口修好；消费仓的台账不再随公共核分发。**

- `_gatesuite/run.ts` 识别包内形态（目录名以 `_gatesuite` 结尾），定位 gate 脚本时剥掉
  registry 里的 `governance/` 前缀 —— 修掉「装包后跑门禁找不到脚本」；判定不依赖路径分隔符。
- `ledger/` 只保留 `*.example.csv`：豁免表与基线棘轮属于消费仓的 B 层实例数据，
  打进公共核等于「别人装包也带着 allin 的豁免表」，发布区扫描口径会互相污染。
  真实台账由消费仓经 `--exemptions` 传入，缺省从 cwd 向上找 `governance/ledger/` 或 `tools/gate/ledger/`。
- 发布序列从 `1.1.2` 直接到 `2.0.2`，**没有 2.0.1**（PyPI `releases` 实测无此键）。
  本版与 `VERSION` / `pyproject.toml` / `src/allin_governance/__init__.py` 三处同为 `2.0.2`。

升级：`pip install -U allin-governance==2.0.2`

## 2.0.0 —— 2026-11-25

**公共核提仓：从 `allin` 仓迁出，成为独立发行仓。**

破坏性变更（2.0.0）：

- 源码位置变了（`packages/allin-governance` + `tools/` 六目录 → 本仓 `src/allin_governance/`）；
- `pyproject.toml` 的跨目录 `package-dir` 映射**全部取消**——包内即真身，不存在第二份；
- 随之 `check_pkg_parity.py` 失去对账对象（已从消费仓移除）。

功能与 1.1.2 等价：8 个子命令、20 道门禁套件、`facts measure` 无仓模式均不变。

## 历史

2.0.0 之前的历史见消费仓 [`allin`](https://gitee.com/siwpan/allin) 的 `CHANGELOG.md` 与
`packages/allin-governance/`（P1 前仍然存在）。
