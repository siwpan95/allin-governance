# `governance/` — 治理内核的可执行门禁（allin 侧）

> **这是什么**：把「工作区治理」的规则从**文字**变成**退出码**，并随本仓一起交付 —— 克隆下来就能跑，不依赖任何本机路径或私有工具。
> **口径（2026-10-26 并入）**：本目录原是 `siwpan/deepseek-harness` 的 `governance/` 内核，于 2026-10-26 整体并入 `allin` 的 `tools/gate/governance/`，原 DSH 远端已删除。
> **规则事实源**：规则本身来自本仓 `docs/规范/` 与 `docs/事实源/`；**本目录是它的可执行落地**，逐条对应关系见 [`RULES.md`](RULES.md)。
> **哲学与事故表**：见 [`docs/记录/专题/治理门禁哲学与事故表_2026-10-26.md`](../../docs/记录/专题/治理门禁哲学与事故表_2026-10-26.md)；粘合剂模式见 [`docs/规范/SPEC-105-治理模式建设指南.md`](../../docs/规范/SPEC-105-治理模式建设指南.md) §9。

## 一、怎么跑（allin 侧命令）

```powershell
npm run check                        # 一条命令：治理门禁 + 文档契约 + .ps1 编码
# 以下命令针对**消费仓**布局（由 `init` 铺出 <仓根>/tools/gate/...），本仓没有那些文件。
# gov-cmd: allow 是给命令判据的行内标注：这条命令不是本仓的失效引用，是另一套布局的示例。
npm run check:governance            # gov-cmd: allow 全部门禁，仓内判据
npm run check:governance:selfcheck  # gov-cmd: allow 证明每个门禁仍能抓到注入的缺陷
node tools/gate/governance/run.ts --list            # gov-cmd: allow 列出 id / 档位 / 覆盖的规则
node tools/gate/governance/run.ts --only g01-paths # gov-cmd: allow 只跑某几条
node tools/gate/governance/run.ts --no-profile     # gov-cmd: allow 只看仓内
node tools/gate/governance/run.ts -- --quiet       # gov-cmd: allow 透传参数
python -m allin_governance._gatesuite.gates.g01_paths --selfcheck       # 单条门禁自检（不经 Node）
python -m allin_governance._gatesuite.gates.g01_paths --no-baseline --list-all  # 看全量存量，忽略基线
python -m allin_governance._gatesuite.gates.g14_preset                  # 单跑：含 profile 探针（读 preset 安装点）
```

**仓内 vs 仓外的边界（重要）**：`check:governance` 是**仓内**判据 —— 它带 `--no-profile`，只转发给
注册表里声明了 `profileProbe` 的门禁（当前只有 `g14-preset`），其他门禁不会收到这个开关。
**要查「仓外部署面」**（preset 安装点的 `link:` bundle 是否悬空、挂载的 patch 是否完整）就**单跑** `g14-preset`，
或显式 `node tools/gate/governance/run.ts --only g14-preset`（不带 `--no-profile`）。两种模式都有 A/B 实证：
带标志时输出里 0 条 profile 解析行，不带时 1 条。

**提交时自动跑**：`pre-commit` 钩子会执行 `npm run check:precommit`（13 条快速子集，约 8 秒）：
排除 `g00-meta`（契约自检要跑完 15 条门禁的 `--selfcheck`，约 20 秒）与 `g04-push-evidence`（要连远端，离线会等超时）。
这两条与其余全量核查仍由 `npm run check` 覆盖；子集清单只在 `package.json` 里定义一处，钩子不复制。

**退出码（全部门禁统一）**：`0` 干净 · `1` 基线之外的新阻断 · `2` 用法错误 · `3` 环境错误。
`run.ts` 按「最坏优先」聚合（`3` > `2` > `1` > `0`）；任何一条非 0 都不会被吞掉。

**依赖**：Python 3.9+、`git`、Node.js ≥ 22.18（`run.ts` 用 Node 原生类型擦除直接运行，**不需要 `npm install`、不需要 tsx**）。
Python 解释器按 `DSH_GOVERNANCE_PYTHON` → `python3` → `python` → `py -3` 顺序探测，**探不到就带名字报错**（`exit 3`），不会静默跳过。
平台专属能力在别的平台上记 `SKIPPED (not a pass)` 并计数，**SKIP 不算通过**。

## 二、门禁契约（由 `g00-meta` 逐条机械校验）

每个登记在 [`registry.json`](registry.json) 的门禁必须满足：

1. 注册表条目含 `id`/`title`/`script`/`tier`/`rules`/`platforms`/`requires`/`selfcheck`；
2. 脚本存在，且 `--help` 暴露 `--selfcheck`、`--quiet`、`--baseline`、`--no-baseline`、`--write-baseline`、`--list-all`；
3. `--selfcheck` 退出 0，输出 `SELFCHECK {"caught":N,"clean":M,"misreports":K}`，要求 `N≥1`、`M≥1`、`K==0`；
4. 门禁 id 在 [`RULES.md`](RULES.md) 里有记载。

**为什么把自检写进契约**：只能跑阳性、或「打印了提示就返回 0」的门禁，会把假绿卖给所有人。自检必须用**真实破坏用例**（临时仓里真的写一条死路径、真的删一个字节）。

## 三、规则覆盖情况（三类，不许含糊）

| 类别 | 含义 | 去处 |
| --- | --- | --- |
| **已机械化** | 有门禁，改坏就红 | 本目录 `gates/`；逐条对照表见 `RULES.md` §4 |
| **流程纪律** | 对话与判断层（先问再计划、无人值守不推送、冗余三查的语义拆分） | `RULES.md` 标注 + `preset/` 的治理段；**不要硬编成门禁** |
| **平台专属** | PowerShell/Windows 语言与系统坑、盘健康事件日志 | 标注为平台专属；非该平台记 SKIP，不记通过 |

## 四、基线棘轮（让门禁「现在就能绿」）

存量没清完就上线的门禁，通常的结局是被绕过。因此每条门禁都支持基线：存量一次性写进 `baseline/<gate>.txt`，此后**只拦新增**。

```powershell
python -m allin_governance._gatesuite.gates.g01_paths --no-baseline --list-all   # 先看全量
python -m allin_governance._gatesuite.gates.g01_paths --write-baseline           # 再冻结存量
git diff src/allin_governance/_gatesuite/baseline/                                    # 基线变化必须可评审
```

**纪律**：刷新基线前必须先看全量；基线文件随仓提交（收紧与放宽都要在提交信息里写明理由）；`g01-paths` 的基线当前**为空**，即存量已清零。

### 4.1 双向棘轮（`g19-docs-refs` 试点，2.4.x 逐门禁接线）

单向棘轮只回答「这条 finding 是不是存量」，它有两种安静的坏法：**静默增长**（`--write-baseline` 把当下全部 finding 一把冻进去，新问题被藏进默认）与**过期账单**（键已不再命中却没人清，账本只增不减，久了没人敢信）。判据口径见 **ADR-0011 §D1**，两条都由机制兜住：

| 语义 | 行为 | 开关 |
| --- | --- | --- |
| **纯增长拒绝**（D1.3） | `--write-baseline` 时若 `旧键集 ⊂ 新键集`（一条都没修好、只是长胖）⇒ **拒绝写入**（基线文件字节不变）、退出 `1`，列出新增键 | 确需接管存量时显式 `--allow-baseline-growth`（建议同时给 `--reason "..."`），输出明示「已允许增长」+ 理由 |
| **混账允许但要两列账**（D1.3） | 有修好、也有新增 ⇒ 允许写，但**必须先打** `-fixed <n>  +new <n>` 再落盘 | —（正常收账不该被拦：每次收账都喊狼，喊三次就没人看开关了） |
| **过期即红**（D1.2） | 默认运行（`--check`）时基线里有当下不再命中的键 ⇒ 退出 `1`，逐条前缀 `-stale` | `--pass-on-stale` 本地放行（退 0，仍列出 stale 键） |
| **人手开关 CI 禁用**（D1.4） | `--pass-on-stale` / `--allow-baseline-growth` 在 `CI=1` 或 `--ci` 下出现 ⇒ **退出 `2`（用法错误）**，判在参数解析、判在跑判据之前 | — |
| **计数可机读**（D1.2） | 每次查账必打印 `counts: scanned=<n> baseline=<n> stale=<n> new=<n>` | — |
| **来源可见** | 每次运行都打印 `baseline: <相对路径> [仓内\|仓外] (N key(s), …)` | — |
| 因「已修」而缩短 | 永远允许（收缩是棘轮正转） | — |

⚠ **`--pass-on-stale` 在 CI 里禁止使用，且已是机制不是口号**：ADR-0011 D1.4 要求「宁可红在参数解析，不要绿在语义」，所以 `CI=1` 或 `--ci` 下出现它直接退 `2`。它只为人放行（本地重构到一半、账本先不清是常态）；CI 里用它等于把「账本失真」设成常态 —— 一次放行之后，后续所有 stale 都被同一句话盖住，屏幕上永远只有一行绿。

首次建账（基线文件**不存在**）不算增长：没有旧账本可比。文件**存在但零键**（存量已清零的记录）算 —— 从 0 变 1 正是「把新问题藏进默认」。

**本版未做（属 2.4.x 批量，理由见 ADR-0011 §D1.1/D1.5 与 §D4 的「不一次铺满」）**：键格式带 `count:<n>`；基线头部元数据 `# written: <ISO8601> commit:<sha> reason:<原话>` 及「缺元数据判红」。这两条要同时改 17 份基线文件的格式并给消费仓迁移路径，单点试点先落格式会让「先试点」变成「试点即破格式」。

其余 16 道门禁本版仍是单向棘轮：一次铺开会让一批消费仓门禁从绿转红，且会造出 16 个半接线状态。共享判据在 `runtime/govlib.py` 的 `baseline_write()` / `baseline_check()` / `ratchet_human_only()` / `ratchet_selfcheck()`，2.4.x 是**接线**而不是重写。

## 五、新增一条门禁（五步，缺一步就会在 `g00-meta` 变红）

1. 写 `tools/gate/governance/gates/gNN_<name>.py`，`import govlib`（`sys.path` 指向 `../runtime`），核心判定写成 `scan(...) -> Report`；
2. 在文件末尾加 `selfcheck()`：注入真实破坏用例 → `check.detects(...)`；干净样例 → `check.quiet(...)`；末尾 `return check.finish()`；
3. 在 `tools/gate/governance/registry.json` 登记（含 `rules` 与 `baseline` 路径）；
4. 在 `tools/gate/governance/RULES.md` §三/§四 写清它覆盖哪条规则、判据与已知豁免；
5. 跑 `npm run check:governance:selfcheck` 与 `npm run check:governance`，两条都绿才算完成。

## 六、与既有门禁的关系（不要重复造）

本工作区 `tools/` 与 `platform/` 已有一批同源的机械保障，**新门禁只在它们没覆盖的地方造**：
仓根 `tools/repo_hygiene.py`（`H1`–`H8`）、`tools/gate/` 下的 `scan_hardcoded.ps1` / `check_undefined_names.py` /
`docs_index.py` / `check_ps1_bom.py` / `watchdog.py` / `check_gate_drift.py` / `make_doc_aliases.ps1`，
以及 `platform/scripts/check.mjs`。逐条对应关系见 `RULES.md` §4.4 与 SPEC-105 §9。

## 七、文件清单

| 路径 | 作用 |
| --- | --- |
| [`RULES.md`](RULES.md) | **规则 → 门禁映射（规则事实源）**，含引用契约与维护约定 |
| [`registry.json`](registry.json) | 门禁注册表（`g00-meta` 的输入） |
| [`run.ts`](run.ts) | 派发器：解释器发现、聚合退出码、`--list`/`--only`/`--selfcheck-all` |
| [`runtime/govlib.py`](runtime/govlib.py) | 共享运行时：退出码、仓根发现、基线棘轮、报告、自检夹具 |
| [`gates/`](gates) | 门禁本体（Python，每条自带 `--selfcheck`）：`g00` 契约自证、`g01` 路径、`g03` 凭据、`g04` 推送实证、`g05` 仓库卫生、`g06` 编号契约、`g07` 事实源时效、`g08` 删除证据、`g09` Python 编码/转义、`g10` 失败要响亮、`g11` 未定义名、`g12` 对象库完整性、`g13` docs 只放文档、`g14` preset 结构、`g15` 文档契约 |
| [`preset/`](preset) | 治理 persona 的可重装载体（`allin.patch.yml` + 安装/校验/回滚说明）；结构由 `g14-preset` 守住 |
| [`baseline/`](baseline) | 基线棘轮文件（随仓提交，变化可评审） |
