# 变更记录

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
