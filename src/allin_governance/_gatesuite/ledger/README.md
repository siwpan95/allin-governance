# ledger/ —— **不随包**（2.0.0）

这里原本是消费仓（allin）的**豁免台账**：`publish_area_exemptions.csv`（发布区各仓的豁免）、
`repo_hygiene_exemptions.csv`（仓体卫生豁免）、以及三份基线棘轮文件。

它们属于 **B 层实例数据**（"allin 自己的仓清单与裁决"），按 SPEC-206 §4 不该进公共核——
打进包等于「别人装了包也带着 allin 的豁免表」。

## 2.0.0 起的做法

- 本目录只放 `*.example.csv`（字段与格式示例）；
- 真实台账由**消费仓**持有，路径通过 `--exemptions <path>` 传入；
- 缺省回退：从 cwd 向上找 `governance/ledger/` 或 `tools/gate/ledger/`。

## 为什么改

2026-11-25 实测：新仓被发布区的 `repo_hygiene --all` 扫到，H5 把台账 `reason` 字段里的
正则示例 `sk-[0-9a-zA-Z]{20,}` 当成凭据形态报红——**别人的数据放在公共核里，连扫描口径都会互相污染**。
