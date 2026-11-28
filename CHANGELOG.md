# 变更记录

## 2.5.6 —— 2026-11-28

### `upgrade-in-place` + `--base`：把「修」和「重建」之间的那条路补上
调研指出 `repair` 的合法领地只有 pip 层，碰解释器基准只有两条路：
`python -m venv --upgrade`（基准被**就地升级**）或 rebuild（基准搬家/没了）。
上一版只有 repair/rebuild，中间那段只能靠手敲。新增：

```
allin-governance venv upgrade-in-place --root <仓根> [--base <解释器>]
```

`rebuild` 同时新增 `--base`：跨仓重建时**必须显式给基准**，否则会把 A 仓的解释器
装进 B 仓（调研点名的错误用法）。

### 实测：三仓的 venv 历史
- `allin`：已有 .venv（3.13.14，home 指 3.13.12 → drift-name-only，不阻断）；
- `govrule`：**没有 .venv** → 用 rebuild 建出（3.13.14，home=vendor/python，0 问题），
  250 测试在 venv 里通过；
- `allin-governance`: 同样建出 .venv 并 `pip install -e ".[full]"`。

### 一个如实记录的既有缺陷
`_repocheck/test_scanner.py` 两个用例在**任何解释器**上都失败
（`attempted relative import with no known parent package`），与本次改动无关；
不修、不掩盖，留给该模块的负责人。

## 2.5.5 —— 2026-11-28

### 判据分级：drift-name-only 不该阻断
2.5.4 把 home 漂移一律记 issue，导致「3.13.12 指向 vs 3.13.14 解释器」这类**同名小版本差**
被判红。调研口径：路径存在 ≠ 健康，但一致性要分两档——跨 minor 是 ABI 风险（阻断），
同名小版本差只是命名漂移（提示）。

`check()` 现在返回 `issues`（阻断）+ `notes`（提示）；`venv check` 退出码只看 issues。
实测 allin：exit 0，NOTE drift-name-only。6 测试不变。

## 2.5.4 —— 2026-11-28

### 调研指出的三处缺陷，修在 venv 能力里
1. **rebuild 安装失败仍报成功**（最严重）：`pip install` 失败只 break，旧环境已被改名、
   目标是半成品，却返回 `rebuilt: True`。现在失败即删新环境、把旧环境改名还原，返回
   `rebuilt: False, rolled_back: True`；
2. **home 判据只看 `Path.exists()`**：路径存在不等于健康。新增四元组：路径存在、
   major.minor 与解释器一致（`drift-abi` / `drift-name-only`）、`home == base_prefix`、
   `import ssl, sqlite3, ctypes` 冒烟；
3. **基准解释器跨 minor 默认重建**：会换 ABI。现在拒绝并提示显式指定基准。

配套：allin 的深度巡检 `repair_home()` 对跨 minor **拒绝改写 pyvenv.cfg**
（把 ABI 问题伪装成修好比不修更危险），并在注释里指向 `venv rebuild` 的正解。

6 条测试（新增 ABI 漂移分类、cross-minor 拒绝重建）。

### PKM 与 TextCNN（调研结论，未动工）
- 文档体系暂不拆仓：现有门禁（索引双射/链接/机器事实）绑在仓内路径上，拆出去等于
  让判据跨仓失效；建议先在 `docs/pkm/` 做索引分段与派生视图（调研 §5）；
- TextCNN（Kim 2014）已在 `_ml/` 作为骨架，接 PKM 之前先攒标注集；
  接法应是「软判断只用于排序，硬结论仍由判据给」，与现有 router 的分工一致。

## 2.5.3 —— 2026-11-28

### venv check / repair / rebuild：虚拟环境的体检与自愈
每个仓都有 `.venv`，每个仓都会坏。修法不依赖业务语义——典型该下沉的能力。

```
allin-governance venv check   --root <仓根> [--json]
allin-governance venv repair  --root <仓根>     # 只修 pip（ensurepip），不猜解释器
allin-governance venv rebuild --root <仓根> [--dry-run]   # 旧环境改名保留，再重建
```

判据（check）：解释器可执行 / `pyvenv.cfg` 的 home 指向仍存在 / pip 可用。
- `home-missing`（换机/卸载 Python 的典型症状）**原地修不了**，repair 明确拒绝并建议 rebuild；
- rebuild 先把旧 venv 改名为 `.bak-<时间戳>`，失败则回滚，不静默删除；
- 有 `requirements-dev.txt` / `requirements.txt` 时按序安装。

消费仓里那套 964 行深度巡检（7 项）保持原样，作为超集委派源；本命令是不依赖消费仓的最小闭环。

4 条测试（临时目录，假 venv：无环境 / 坏 home / dry-run 不改动）。

## 2.5.2 —— 2026-11-28

### 从「只报告」到「解决需求」：`scan --catalog`
三仓分工定案：allin 是消费方（需求：发布区有哪些仓、怎么启动、远端是谁），
本仓解决需求，govrule 负责审批与交付。

`scan` 此前只报项目数与重复检出，消费方无法据此登记。新增：

```
allin-governance scan --catalog --root G:/repos [--out catalog.json]
```

产出 `allin-governance/discovery-catalog/v1`：每仓的 id/path/checkout/launch（探针）
/publish.repo+branch，以及 schema/generated_at/hostname/discovered/probe_unavailable。
**只含机器可判字段**，人工语义（name/category/note/verify…）绝不出现——
那是消费仓 `registry/projects/` 的职责。

实测：78 仓，探针可用 26、不可用 52（不伪造，交人工）。

契约测试 `test_catalog_contract` 守两条：机器字段齐全、人属字段零混入。
消费方（allin）侧另做 schema/discovered 一致性校验，provider 升级不能静默降级。

## 2.5.1 —— 2026-11-28

### `facts verify` 崩了：`current_machine` 搬家后调用点没跟上
`current_machine()` 已提到 `_gatesuite/runtime/govlib.py`（g19 也要用，一份实现一处维护），
但 `_facts/verify_facts.py` 三处仍调用 `gate.current_machine()` ——
`g20_facts` 模块里已经没有这个名字，于是 `AttributeError` 直接崩。

实测：`allin-governance facts verify --root .` → `AttributeError: module ... has no attribute 'current_machine'`
修复：三处改 `gate.govlib.current_machine()`；修后 `scanned 1 file(s); findings 0`。

### 为什么现在才发现
这条命令不在 20 道门禁里，也不在 pre-commit 里——**只有完整跑一遍全流程才会碰到**。
同类的还有：`scan`、`verify`、`pack`。判据覆盖不到的地方，只能靠「按新人的路线走一遍」。

## 2.5.0 —— 2026-11-26

**两件事：扫盘补「区语义 + 未纳入清单 + 口径指纹」；`pyinstaller-tool` 仓并入为 `pack` 子命令。**

### `scan`：三处不足按业界口径补齐

2026-11-26 整盘实战（`scan --root G:/`）暴露的问题，逐条对照业界做法修：

| 不足 | 修法 | 依据 |
| --- | --- | --- |
| 默认单层报「47 项 / 含 git 0」，而整盘实有 87 个 git 仓 —— 读数缺分母语义 | 读数行显式声明 `扫描范围：仅一级目录，未递归 —— 递归请加 --deep`；JSON 加 `scope.depth/note` | ADR-0011「没有分母的 scanned 不算量过」；业界「Record exclusions and revision IDs when saving metrics」 |
| `archive/` 的有意归档备份被判「同远端仓多份检出」/「内容高度相似」，产生永久假告警 | 新增有序 `ZONE_RULES`（`release` / `archive` / `runtime` / `temp` / `system` / `other`，按路径组件、first-match-wins）；只有 `release`+`other` 参与重复判定 | kylin-x-kernel `code_stats.py` 的 ordered `path_rules`（规则对象参与回归测试） |
| `c` vs `tmp` 79%、`nodejs` vs `nodejs_v22_backup` 50% 等非项目目录入相似度 | 同上：runtime/temp/system 区不参与判定；归档区「只报不警」并单独提示计数 | scc `--no-gen` 跳过非源文件；cloc `--exclude-dir` |
| `found.000`~`found.014` 既不在项目数也不在任何一节，无法判断跳过还是漏掉 | 新增 `skipped` 清单**逐条带 reason**（`not-a-directory` / `hidden-directory` / `skip-dir` / `zone=...` / `shallow` / `no-project-signal`）；文本报告按 reason 汇总 | tokei / kylin `code_stats.py` 的 uncounted + reason 形态 |

实测效果（同机同时段）：整盘深扫的可疑重复 **13 组 → 5 组**，且余下 5 组均为真重复
（`dse-core` 同发布名、`art_video_gen` / `video-generator` / `an-engine` 内容相似、3 处 `MIGRATED.txt` 残留）。

另加 `rules_digest`（规则表前 12 位摘要）：规则一改指纹就变，两次读数是否同口径可机读判断。
回归用例 9 条见 `src/allin_governance/_toolkit/test_scan_repos.py`（含「归档区不报警」与「release 区真重复照报」双向断言）。

### 新增 `pack` 子命令（`pyinstaller-tool` 仓并入）

来源：`siwpan/pyinstaller`（PyPI `pyinstaller-tool` 1.0.0，MIT，2026-10-22）。用户定调
「并入 allin-governance，删除其远端和本地仓，仅保留 PyPI」⇒ 能力重构为 CLI，原仓下线。

- 子命令：`info`（环境探测）/ `build`（单脚本）/ `build-all`（批量）/ `selfcheck`（无依赖自检）。
- GUI 自动检测**只看 import 行**：原 `全能宝.bat` 用 `findstr` 全文匹配，注释里提到 `tkinter` 会误判成 GUI（打出来没控制台、报错看不见）。回归用例正反例各二。
- `pyinstaller` 是**可选依赖**（`pip install 'allin-governance[pack]'`）：不装也能 `info`/`selfcheck`，`build` 时返回退出码 3。
- 不搬 Tkinter GUI（公共核是零三方依赖 CLI）。

---
## 2.4.2 —— 2026-11-26

**把"扫描域"和"抑制额度"变成可判的字段，并修一条把自然写法判红的窗口口径。**

### 新增

- **`registry.json` 的 `scope` 字段**：每道门禁声明自己判哪些文件（glob 数组）。
  20 道里**确证 11 道**、其余 9 道留空 —— **缺省 = 未声明 ⇒ 不可判定**，不给 `**/*` 默认值
  （给了等于"什么都声明了"，字段立刻变噪声）。依据：ESLint flat config 的 `files`/`ignores` 形态。

### 变更：基线格式 v2（`count:<n>` 额度 + 头部元数据）

- 每行 `count:<n> <key>`：同一键最多抑制 n 条，**超量则整组全报**（ESLint bulk suppressions 口径）；
  裸键按 `count:1` 解释，旧基线不会失效。
- 头部落 `# written / # commit / # reason`；**旧基线缺头只 WARN，新写入缺头判红**。
- 14 份基线已迁移。

> **迁移踩到的坑（留档）**：首版把额度一律写成 `count:1`，而 g09 有 3 个键在真实仓里各出现 2/4/3 次
> ⇒ 额度不够 ⇒ **整组被放出来**，表现为"存量违规突然变新增"。修法不是调大全局额度，而是
> **按实际出现次数抬这 3 个键**（`1→2`、`1→4`、`1→3`），且**不吸收任何新键**。
> 教训：**"额度"和"条数"是两个量，迁移时按 1 一刀切，等于把"重复命中"判成了"超量违规"。**

### 修复：D14 的限定词窗口按**可见字符**计数

`交接卷 **2026-09** §五十九` 这种自然写法原被判"裸 §"：窗口量的是原始字符，`**` 与 `**`
把「交接卷」挤出了 12 字符外。改为**从 `§` 向前数 12 个可见字符（`*` `_` 反引号不计）**。
- 放宽的是**计量的方式**，不是判定：裸 `§五十九`（前后无限定词）仍判红；
- `g15 --selfcheck` 仍抓到 26 条注入缺陷（与改前同数），真实仓 `D14-qualified` 196 → 202。

### 说明

- 本次 `scope` 只声明**能确证**的 11 道；未确证的 9 道留空，是刻意的"不可判定"，不是遗漏。
- 基线里仍有 11 个键当前扫描已不再出现（历史冻结项）；棘轮按"只拦新增"处理，不会因此判红。


## 2.4.1 —— 2026-11-25

**自检不许再随环境变。** 修一个只在 CI 才红的缺陷。

### 缺陷

2.4.0 的棘轮自检里，有一条用例验的是「人类本地跑、`--pass-on-stale` 应被放行」，但它**继承了宿主环境变量 `CI`**。GitHub Actions 全局注入 `CI=true` ⇒ 该分支在 CI 里永远走"被拒" ⇒ 用例必红：

```
FAIL missed injected defect: ratchet/human-only-in-ci-rejected
SELFCHECK {"caught":11,"clean":4,"misreports":0}
```

本地永远绿（本地没有 `CI`），CI 100% 红（不是 flaky）。连带把 `check_blind_gates.py --two-halves` 判成 `selfcheck_exit=1`。

### 修法

- `govlib.controlled_env(**values)`：`None` 表示**该变量必须不存在**（走 `os.environ.pop`，不设空串 —— `""` 与"不存在"在 `in os.environ` 上语义不同）；`finally` 无条件还原。
- 那条用例拆成**三条各自钉死环境的断言**，无一处读宿主：`CI=None + --ci` ⇒ 拒；`CI=1 + 无 --ci` ⇒ 拒；`CI=None + 无 --ci` ⇒ 放行。
- 新增第 9 条 `ratchet/env-window-restored`：窗口跑完，`os.environ["CI"]` 必须与进场逐字相同 —— 泄漏的 `CI=1` 会让同进程后续所有用例换行为，比没有窗口更难查。
- 全局审计：只有 `g15`(`ALLIN_GATE_STRICT_SKIP`) 与 `g17`(`ALLIN_REPOS_ROOT`) 在自检里动环境，两处都是显式设值 + `finally` 还原，不继承。

### 实测

| 环境 | g19 自检 |
| --- | --- |
| 无 `CI` | `SELFCHECK {"caught":13,"clean":4,"misreports":0}` exit 0 |
| `CI=true` | 同上，exit 0 |
| `CI=0` | 同上，exit 0（"有人明确关掉"不应被读成 CI）|

`--selfcheck-all` 在 `CI=true` 与无 `CI` 两种环境均 **20/20 clean**；下游 `check_blind_gates --two-halves` 回绿（`selfcheck_exit=0`）。

### 判据未放宽

`g19` 判 `allin`：`scanned 89 / blocking 0 / exempted 350`（89 而非 88，是 `architecture/adr/ADR-0011-…md` 新增后判定面 +1，非回归）。

### 教训

**自检用例必须显式设定它要测的环境变量，不能继承。** 继承的后果是「测了 runner」—— 本地绿、CI 红，且两边都自认有理。

## 2.4.0 —— 2026-11-25

**判据开始计量自己的工作量。** 三件事：基线从"只拦新增"升级为**双侧棘轮**；`g15` 补上 `scanned`；`g15` 自检的负对照从"能打印"变成"能判"。

### 新增

| 机制 | 语义 | 借鉴 |
| --- | --- | --- |
| 基线双侧棘轮（`g19` 试点） | **纯增长**（一条没修好、只是长胖）拒绝写入并退 1；**混账**（既修又新）允许写，但强制打印 `-fixed <n> +new <n>` 两列；基线里存在**当下已不命中**的键（stale）⇒ 退 1 并逐条指名，提示 prune | ESLint bulk suppressions（MIT）：抑制记数量、超量全报、残留逼 prune |
| CI 硬拦 | `CI=1` 或 `--ci` 下出现 `--pass-on-stale` / `--allow-baseline-growth` ⇒ **exit 2**（判在参数解析，不是文档约束） | 同上 |
| 机读计数行 | 每次查账打印 `counts: scanned=<n> baseline=<n> stale=<n> new=<n>` | 同上；"只有退出码的日志里，挂 12 条和一条都没有长得一样" |
| `g15-docs-contract` 补 `scanned` | 判定面 = `docs/` 去历史树与 ASCII 别名目录，实测 **141**；输出加 `scanned N file(s)`，保留旧 `blocking=… new=…` 形态（历史执行记录引用它） | ADR-0011 D3 |

### 修正

- **`g15` 自检的 `MISSED case not caught` 原来只 `print`**，不计载荷、不改退出码 —— 把判红整段删掉，自检**照样 exit 0**。现计入 selfcheck 并影响退出码（`caught 24 → 26`），两条新用例各自做过变异测试：删盲门判红 → `MISSED empty-hub` + exit 1；不再记 SKIP → `MISSED no-index` + exit 1。
- `g19` 自检 `4/3 → 12/4`（新增 8 条棘轮 detects + 1 条 quiet）。

### 判据未放宽（实测）

- `g19` 判 `allin`：`scanned 88 / blocking 0 / exempted 350 / exit 0`，hostname 豁免 3+3 不变。
- `g15` 判 `allin`：`exempted` 36 项与 2.3.1 **逐字相同**，`blocking=0` 一致；唯一差别是多出 `scanned 141`（与 `D1-registered=141` 自洽）。
- `--selfcheck-all`：**20/20 clean**。

### 已知（未落，勿当作已实现）

- 基线键的 `count:<n>` 格式与 `# written: <ISO> commit:<sha> reason:<原话>` 头部元数据**未落** —— 需 17 份基线同改，单点先落会把"试点"变成"试点即破格式"，留 2.4.x 批量。
- `g06-numbering` 在本仓与消费仓均为 **0 对象**（`git ls-files` 无 `*.zh.md` / `vN.*`）。它记的是 `skipped`（理由与判定同屏、可复核），不是 `blind`、也不是 `pass`。但"永远是 SKIP"应当是被 owner 登记过的决定，不是没人注意到的默认。
- 删掉基线文件再 `--write-baseline` 仍可绕过增长拦阻（ESLint 同一缺口）。缓解：首建账输出明写"提交前看 `git diff` 是否被整体重写"，把绕过变成 diff 可见 —— 不是机制，是可见性。

## 2.3.1 —— 2026-11-25

**判据的读取路径跟上提仓**：不再假设"套件在本仓"。

### 修复

| 判据 / 组件 | 病 | 修法 |
| --- | --- | --- |
| `g00-meta` | 43 条阻断 = 20 条 `registry: script not found` + 23 条 untracked-support。它按仓内布局认 `registry.parent.parent/script`，而编排层早已会剥 `governance/` 前缀 | 新增 `resolve_gate_script()`（仓内优先、包内兜底）；`untracked_support()` 剪枝 `.venv/.tox/.nox/venv/site-packages/dist-packages/node_modules/__pycache__` 及任一祖先含 `pyvenv.cfg` 的子树，**跳过数打进 note**（排除不许静默） |
| `g18-exceptions` | 例外台账随套件进包后 `不可读`；baseline / ledger / known-gaps 同理 | `locate()` 仓内优先、包内兜底；标签写包内相对名而非绝对路径（绝对路径换机即变假漂移） |
| `g19-docs-refs` | 它逼本机 agent 去改他机分区文件，唯一绿法是违反铁律 6 | 按 `SPEC-001:135`「门禁同源」：`docs/事实源/机器/<stem>/` 且 `stem != hostname` 时 path-ref 与 command-target 一律豁免并计入 `exempted`（不静默跳过）；baseline 改以仓根为基准 |
| `g15-docs-contract` | `--root` 默认值在 argparse 构造时就上溯，传了也照旧可能 exit 3 | 改惰性解析 |
| `run.ts` / `govlib` | 无 `--root` 透传，仓根与解释器靠 `.git` 上溯与 `cwd` 猜 | 新增 `resolve_root()`（显式 root 绝不被上溯覆盖）；`--root` 逐道下发、子进程 `cwd=root`、解释器优先 `<root>/.venv`；用法错 2 / 环境错 3 分离 |
| `_repocheck/rules_gate` | `has_gate_suite` 只认 `tools/gate/governance`，在内核仓跑 `--only-gates` 时 20 条门禁规则一条都不挂 —— 命令照常返回、健康分照给，**没验还报绿** | 补包内布局识别 |

### 自证

`--selfcheck-all` 20/20；`g00` 12/4 → 13/5、`g18` 4/1 → 7/2。新增成对用例：异机事实**必须放过**、本机漂移**必须判红**（只证前者等于只证豁免存在，没证它没越界）。

### 已知

README 的"当前版本"自 2.1.0 起未更新（写 `2.0.2`），本版一并订正。`release_preflight` 的五处一致性检查一直存在，但 2.1–2.3 三次发版没有跑到它 —— 判据存在不等于判据被执行。

## 2.3.0 —— 2026-11-25

**文本分类骨架：TextCNN 打底，规则降级**（全哥定的结构）。

### 交付

| 模块 | 职责 |
| --- | --- |
| `_ml/protocol.py` | 协议层：`Prediction(label, score, source)` / `TextClassifier` —— 只依赖标准库 |
| `_ml/fallback.py` | 降级：关键词规则（永远可用），**也是标签口径的唯一准绳** |
| `_ml/textcnn.py` | 底座：TextCNN（Kim 2014 三核并行，CPU 分钟级）；torch **延迟导入** |
| `_ml/router.py` | 路由：模型 ≥ 阈值走模型，否则降级；**不一致时两边都留档** |
| `_ml/dataset.py` | 语料：记忆月账 + 各机月账条目（标签来自 `[tag]`） |
| `_ml/train.py` | 训练与评估（每次打印基线，防"准确率幻觉"） |

CLI：`allin-governance ml train|eval|route|corpus`
依赖：`pip install 'allin-governance[ml]'` —— **torch 不进 `dependencies`**，
没装时骨架照常可用（`route`/`corpus` 走规则）。

### 实测结果（诚实的负结果）

语料：`docs/记录/记忆/` 339 条 → 归一化 5 类（governance 120/agent 90/tooling 56/其他 47/fact 24）
→ 去重后 train 266 / val 63。

```
基线（多数类占比）= 0.317
epoch 1..8  val_acc = 0.317 / 0.317 / 0.302 / 0.333 / 0.317 / 0.302 / 0.365 / 0.302
最终 val_acc = 0.302  ← 低于基线
混淆：governance→agent 13 / 其他→agent 8 / agent→governance 5 / tooling→agent 5
```

**结论：pipeline 通了，模型无效。** 266 条 / 5 类训不出可用分类器（元宝那篇说"几千条"
是有道理的）。模型把 governance 大量判成 agent，说明它学到的是**类别大小**而非特征。

配套事实：模型与规则**分歧 73%（44/60）**——分岐本身是有价值的信号，
说明规则抓到的东西模型没抓到（反之亦然）。

### 骨架仍然成立的理由

1. **降级链路实测可用**：无 torch → 全程 fallback（分类正确）；有权重但置信度低 → 回落规则；
2. **底座随时可换**：`TextClassifier` 协议与实现无关，将来换 embedding/小模型不用改调用方；
3. **接口把「谁给的结论」变成一等公民**：`Prediction.source` 强制标 `textcnn`/`fallback`，
   治理系统的输出不许说不清来源。

### 硬边界（写进 `_ml/__init__.py`，防止骨架搭错层）

**硬判据不许概率化。** 路径存在性、序号连续、断链、版本集合相等是事实判定；
一旦变成"模型说 92% 概率通过"，治理就失去依据。本包只服务软判断。

## 2.2.2 —— 2026-11-25

**修 2.2.1 漏掉的第二处迁包缺陷：`gitee_release.main()` 签名不接受 argv。**

2.2.1 修了 `import runtime_defaults`，但 `main()` 仍是 `def main():`（无参、自己 parse
`sys.argv`）—— `allin-governance gitee <cmd>` 分发时传 argv 直接
`TypeError: main() takes 0 positional arguments but 1 was given`。

**两个缺陷都是「从 PyPI 装一次、真跑一遍」才暴露的**，仓内测试跑不出来（仓内 import 走的是
源码目录，不走包）。这条写进教训：**迁包后必须从安装态真跑每条子命令**。

## 2.2.1 —— 2026-11-25

**修 2.2.0 的致命缺陷：`gitee` 子命令在包内形态下直接 ImportError。**

2.2.0 把 `gitee_release.py` 迁进包时，它顶层的 `import runtime_defaults` 没跟着处理 ——
那个模块只在 allin 仓的 `tools/` 下存在。于是从 PyPI 装 2.2.0 后跑
`allin-governance gitee …` 会 `ModuleNotFoundError: No module named 'runtime_defaults'`。

**修法**：三段回退（包内 → 仓内旧布局 → 内置默认值），迁包不因一个可选依赖就把整个发布工具打死。

**这个缺陷是「发布链路收拢」自己抓出来的**：2.2.0 的第一次 `publish release` 跑完，
allin 侧一跑 `pytest tests/test_gitee_release_pure.py` 就炸了 —— 新入口第一时间暴露了迁包漏项。

## 2.2.0 —— 2026-11-25

**发布链路收拢：`gitee` / `github` / `publish` 三个子命令 + `PUBLISHING.md`。**

背景是全哥一句话：「gitee、github、pypi 这些建仓推送与发版的内容建议收拢到一起，
并补上对应 readme。不然你那句『下次发版请用 release.sh --upload』大概就是形同虚设 ——
**就好像纪律没有门禁支持，就会被当作结果而不是依据**。」

### 新增

- `gitee` 子命令：`_gitee/release.py`（从 allin 仓 `tools/gitee_release.py` 迁入）。
  建仓/转公开/发发行版，与 `github` 配对成「Gitee 权威 → 本地 → GitHub 镜像」双核。
- `publish` 子命令：`_publish/pypi.py`，四道可独立执行的判据 ——
  `check`（版本三处一致）/ `build`（**双产物**）/ `verify`（**各装一次**）/ `upload`，
  外加 `release` 一条龙。
- `PUBLISHING.md`：三条链路的完整命令 + 判据表 + **6 条真踩过的坑**。

### 为什么这能防住 2.1.0 那种漏

2.1.0 漏 sdist 不是「不知道要 sdist」，而是**判据存在但被绕过**：
CI 里写着「构建 sdist + wheel」，人手打 `build --wheel` 上传。
现在 `publish build` / `publish upload` **在代码里硬要求双产物**，
且 CI 改走**同一条命令** —— 命令层与 CI 层不再存在「一条有判据、一条没有」。

## 2.1.0 —— 2026-11-25

**新增判据 D19：共有月账不按机归属。**

`记录/记忆/YYYY-MM.md` 是三台机**共有**的月账，只该记「跨机成立的结论」；
「这件事是哪台机做的」属于过程，该进各机的 `事实源/机器/<机>/月账/`。
早期用 `- **2026-11-01 23:50（@SIWPAN）**` 给条目标归属，这个格式已废弃。

### 判据

- 范围：`记录/记忆/YYYY-MM.md`（月卷）
- 只判**时间戳里**的 `（@机名）` —— 明确的条目归属；
  正文里的 `（@SIWPAN 实证）` 是**教训来源**，合法，不判；
- 与 D18 同精神：只判 `>= 2026-11-25` 的条目，存量历史不逼迫改写；
- 自检加了 `mem-owned-entry` 用例并断言「拦下它的必须是 D19 自己」
  （`caught 23 → 24`，`misreports=0`）。

### 为什么想到这个角度

起初想判「共有月账里有没有本机路径」，一测发现**72 处全是教训里的反面例证**
（"载荷写死 OS 会假绿"这类）—— 说明路径本身在教训里是必要的，那个角度站不住。
最后选的是**唯一能自动判且不误伤的窄信号**：条目时间戳里的归属标注。

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
