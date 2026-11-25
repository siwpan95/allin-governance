# 变更记录

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
