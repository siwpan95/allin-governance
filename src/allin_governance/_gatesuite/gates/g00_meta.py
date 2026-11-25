"""Gate g00 -- the meta gate: a gate that cannot prove itself is not a gate.

Every other gate claims two things: that it still catches an injected defect,
and that it still stays quiet on clean input. This gate turns those claims into
exit codes. For each registry entry it asserts:

* the entry is complete and its script exists;
* the script exposes the whole gate contract (``--selfcheck``, ``--quiet`` and
  the four baseline switches) instead of a private subset;
* ``--selfcheck`` exits 0 and reports ``caught > 0`` with ``misreports == 0``,
  so "it printed something" can never stand in for "it detected something";
* the gate id is documented in ``governance/RULES.md``, so a rule can always be
  traced to the check that enforces it.

It also audits the audit inventory itself (2026-11-10 deep audit H-7):

* every ``gates/gXX_*.py`` on disk must be declared in ``registry.json`` — the
  registry is the aggregator's only list, so an undeclared gate script is never
  run ("a gate that is written but not wired is no gate at all");
* numbering holes must be explainable. ``g02`` is a documented historical hole
  (built into ``KNOWN_NUMBERING_GAPS``); any other hole must be declared in the
  registry's ``declared_numbering_gaps`` as ``{"number": N, "reason": "..."}``.
  Declarations are themselves audited: the number must really be a hole, the
  reason must be non-trivial, and duplicates are rejected. This lets a shipped
  subset (allin-governance init's 14-gate scaffold) state its omissions as data
  instead of widening a global whitelist — 2026-11-13, closing the CI red where
  the scaffold failed its own meta gate on undeclared g14..g19.

Exit codes follow the governance vocabulary: 0 clean, 1 blocking finding,
2 usage error, 3 environment error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))

import govlib  # noqa: E402  (path is fixed up immediately above)
from govlib import EXIT_BLOCKING, EXIT_OK, GateError, Report  # noqa: E402

GATE = "g00-meta"
TITLE = "governance: gate contract self-verification"

#: 当前平台代号，取值与 registry 的 ``platforms`` 字段同一套词汇（``nt`` / ``posix``）。
#: 用它决定「这道门禁在本机能不能自证」—— 见下方 selfcheck 段的说明。
CURRENT_PLATFORM = "nt" if os.name == "nt" else "posix"

REQUIRED_FIELDS = ("id", "title", "script", "tier", "rules", "platforms", "requires", "selfcheck")
REQUIRED_SWITCHES = ("--selfcheck", "--quiet", "--baseline", "--no-baseline", "--write-baseline", "--list-all")
# 契约要求的摘要格式：`SELFCHECK {"caught":N,"clean":M,"misreports":K}`（紧凑分隔符、无空格）。
# 用契约文本本身当正则：格式偏差即判「没打印摘要」，不留「打印了就算」的模糊地带。
SELFCHECK_LINE = re.compile(r'^SELFCHECK (\{"caught":\d+,"clean":\d+,"misreports":\d+\})$', re.MULTILINE)
SELFCHECK_TIMEOUT_SECONDS = 600

#: 与 check_arch_assets.py B 项同一命名口径。
GATE_SCRIPT_RE = re.compile(r"^(g\d{2})_.+\.py$")
#: registry id 形如 ``g00-meta``。
GATE_ID_RE = re.compile(r"^g(\d{2})-")
#: registry 的 ``script`` 字段用**仓内布局**书写（``governance/gates/x.py``）。
#: 包内形态没有这一层 ``governance/``（``gates/`` 与 registry.json 同级），
#: 与 run.ts 的 ``IN_PACKAGE`` 分支同一口径。见 ``resolve_gate_script()``。
PACKAGED_SCRIPT_PREFIX = "governance/"

#: 依赖安装面 / 解释器环境 / 解释器缓存：这些目录里的 ``*.py`` 不是「本仓支撑模块」，
#: 而是**某个已安装的包**或构建副产物。判据的对象是仓内支撑模块 ——
#: 把它们算进来等于把「装在哪」误判成「漏提交」。
#:
#: 为什么必须有这条排除（2026-11-25 实测，套件提仓后第一现场）：
#: run.ts 走的是 ``.venv`` 里**已安装的包**，于是
#: ``.venv/Lib/site-packages/allin_governance/_gatesuite/gates/*.py`` 在盘上存在、
#: 却不在本仓 git index 里 ⇒ 20 道门禁被判成 20 条 untracked-support，
#: 而它们一条也没漏提交。判据本身没错，错在把安装位置当成了支撑文件。
#: 排除只对**安装面**生效：仓内某个目录被 ``.gitignore`` 吞掉的模块（判据真正要抓的
#: 那个缺陷）仍照旧判红 —— 见 selfcheck 里的两组负对照。
ENV_DIR_NAMES = frozenset({
    ".venv", "venv", ".tox", ".nox",
    "site-packages", "dist-packages", "node_modules", "__pycache__",
})
#: 编号缺口白名单：g02 是本仓既有设计缺口（registry.json 与 docs/INDEX.md 均
#: 明确登记「编号缺 g02」），不是遗漏。新增缺号必须在此显式声明，否则一律判红。
KNOWN_NUMBERING_GAPS = frozenset({2})

#: 声明式缺口（2026-11-13）：出厂子集（allin-governance init 的骨架）只带
#: 14 道门禁，g14~g19 是治理中枢专属/可选件，不随通用套装出厂 —— 编号必然
#: 出现缺口，但这是**设计**不是遗漏。让 registry 顶层用带理由的
#: ``declared_numbering_gaps`` 显式声明，比把 14~19 塞进全局白名单更严：
#: 每条声明必须真的是当前缺口、理由非空、编号不重复，否则照样判红。
#: 主仓 registry 不带该字段 ⇒ 行为与改动前逐字节一致（零回归）。
DECLARED_GAPS_KEY = "declared_numbering_gaps"
MIN_GAP_REASON_CHARS = 12


def run_gate_script(python: str, script: Path, args: list[str], root: Path) -> tuple[int, str]:
    """Run one gate script with the same interpreter, capturing its output.

    Line endings are normalized before any line-anchored parsing: a child on
    Windows writes CRLF, which would otherwise defeat a ``$``-anchored pattern.
    """
    completed = subprocess.run(
        [python, str(script), *args],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=SELFCHECK_TIMEOUT_SECONDS,
    )
    text = completed.stdout.decode("utf-8", "replace")
    return completed.returncode, text.replace("\r\n", "\n").replace("\r", "\n")


def resolve_gate_script(registry_path: Path, declared: str) -> Path:
    """把 registry 的 ``script`` 解析成盘上路径：仓内布局优先，包内布局兜底。

    2026-11-25 提仓后，**同一份** registry.json 要服务两种布局：

    * 仓内形态：``<root>/tools/gate/governance/registry.json``，
      ``script = "governance/gates/x.py"`` ⇒ ``registry.parent.parent / declared``
    * 包内形态：``<site-packages>/allin_governance/_gatesuite/registry.json``，
      ``gates/`` 就在 registry 旁边 ⇒ ``registry.parent / declared`` 去掉前缀

    此前只认第一种，于是「跑已安装的包」时每道门禁都报
    ``script not found at governance/gates/x.py``（20 条）—— 判据本身没错，
    错在把「仓内布局」当成了唯一布局。这里只做**兜底**不做替换：
    仓内形态存在时行为与改动前逐字节一致。
    """
    repo_form = registry_path.parent.parent / declared
    if repo_form.is_file():
        return repo_form
    if declared.startswith(PACKAGED_SCRIPT_PREFIX):
        packaged = registry_path.parent / declared[len(PACKAGED_SCRIPT_PREFIX):]
        if packaged.is_file():
            return packaged
    # 两种布局都没有：返回仓内形态，由调用方如实判「script not found」。
    return repo_form


def _in_env(path: Path) -> bool:
    """``path`` 是否位于依赖安装面 / 虚拟环境里（判据不该把这类 .py 当支撑模块）。

    两条判据合起来才够：
      * 路径段里有 ``site-packages`` / ``node_modules`` / ``__pycache__`` 等安装面名字
        （覆盖全局安装，那种环境没有 ``pyvenv.cfg``）；
      * 任一祖先目录带 ``pyvenv.cfg``（覆盖任意命名的虚拟环境，``env/`` / ``.env/`` 也算，
        名字列表永远列不全，环境标记文件才是可判据的事实）。
    """
    if any(part in ENV_DIR_NAMES for part in path.parts):
        return True
    return any((candidate / "pyvenv.cfg").is_file()
               for candidate in (path, *path.parents))


def untracked_support(root: Path, registry_path: Path,
                      tracked: set[str]) -> tuple[list[str], int]:
    """Governance Python files present on disk but absent from the index.

    An ignore rule can hide a support module: the repository-wide ``lib/`` rule
    already swallowed one gate runtime, and `git status` stays silent about
    ignored files, so the loss is invisible until a fresh clone fails. The
    governance directory is derived from the registry's own location, so the
    check tracks wherever the subtree is placed.

    Returns ``(missing, skipped)``. ``skipped`` counts ``.py`` files this scan
    deliberately did not judge because they live on an install surface (see
    ``_in_env``) or outside the repository being judged; the caller prints the
    count so an exclusion is never silent.

    2026-11-25: the subtree is now usually an **installed package**, so the
    install-surface exclusion is what keeps this gate from reporting the package
    as un-committed source. Directories are pruned while walking rather than
    filtered afterwards: a venv holds enough files that walking it is a real cost
    inside a gate that runs on every aggregate run.
    """
    base = registry_path.parent
    if not base.is_dir():
        return [], 0
    missing: list[str] = []
    skipped = 0
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                if entry.name in ENV_DIR_NAMES or (entry / "pyvenv.cfg").is_file():
                    # 整棵剪掉，但如实计数（exclusion 不许静默）。
                    skipped += sum(1 for _ in entry.rglob("*.py"))
                    continue
                stack.append(entry)
                continue
            if entry.suffix != ".py":
                continue
            if _in_env(entry):
                skipped += 1
                continue
            relative = _rel_or_none(entry, root)
            if relative is None:
                # 不在被判的仓里（全局安装的包）⇒ 不是「本仓漏提交」。
                skipped += 1
                continue
            if relative not in tracked:
                missing.append(relative)
    return sorted(missing), skipped


def _rel_or_none(path: Path, root: Path) -> str | None:
    """``path`` 相对 ``root`` 的 POSIX 路径；在 ``root`` 之外则返回 ``None``。

    ``Path.relative_to`` 在跨盘/跨树时抛 ``ValueError``。判据现在允许 ``--root``
    指向任意仓，而治理子树可能在那个仓之外（包被装到别处），所以这里必须能回答
    「这个文件不属于本仓」，而不是让整道门禁崩掉。
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _rel(path: Path, root: Path) -> str:
    """报告用路径：仓内给相对路径，仓外退回绝对路径（只用于显示，不用于判断）。"""
    return _rel_or_none(path, root) or path.as_posix()


def check_gate_inventory(root: Path, registry_path: Path, identifiers: set[str],
                         report: Report) -> None:
    """H-7：registry 之外不得存在「看起来像门禁」的脚本。

    ``untracked_support()`` 只能发现「在盘上但没进 git index」的支持模块；它
    发现不了「进了 index、却没登记进 registry.json」的门禁脚本。而 registry 是
    聚合器 run.ts 的唯一清单 —— 没登记的脚本永远不会被跑到，这正是本仓反复
    踩过的「门禁写了不跑 = 没有门禁」。
    """
    gates_dir = registry_path.parent / "gates"
    if not gates_dir.is_dir():
        return
    declared = {int(m.group(1)) for ident in identifiers
                if (m := GATE_ID_RE.match(ident))}
    for path in sorted(gates_dir.glob("g??_*.py")):
        m = GATE_SCRIPT_RE.fullmatch(path.name)
        if not m:
            continue
        num = int(m.group(1)[1:])
        if num not in declared:
            report.block("rogue-gate", _rel(path, root), 0,
                         f"{path.name}: 命名像门禁但 {_rel(registry_path, root)} 未登记 —— "
                         f"该脚本不会被聚合器执行（门禁写了不跑 = 没有门禁）")


def declared_numbering_gaps(registry: dict, gaps: list[int], registry_path: Path,
                            root: Path, report: Report) -> set[int]:
    """校验 registry 顶层的 ``declared_numbering_gaps`` 并返回**有效**声明编号。

    判据比全局白名单更严，四条缺一即判红（不放宽门禁）：
      ① 字段必须是数组，元素是对象；
      ② ``number`` 必须是 0~99 的整数、不得重复；
      ③ 声明的编号必须**真的是当前缺口**（防止拿过期声明当橡皮图章）；
      ④ ``reason`` 必须是非空字符串且不少于 ``MIN_GAP_REASON_CHARS`` 字符。
    """
    raw = registry.get(DECLARED_GAPS_KEY)
    if raw is None:
        return set()
    rel = _rel(registry_path, root)
    if not isinstance(raw, list):
        report.block("registry", rel, 0, f"{DECLARED_GAPS_KEY} 必须是数组")
        return set()
    valid: set[int] = set()
    seen: set[int] = set()
    for index, item in enumerate(raw):
        label = f"{DECLARED_GAPS_KEY}[{index}]"
        if not isinstance(item, dict):
            report.block("registry", rel, 0, f"{label} 必须是对象（含 number / reason）")
            continue
        number = item.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 99:
            report.block("registry", rel, 0, f"{label}.number 必须是 0~99 的整数")
            continue
        if number in seen:
            report.block("registry", rel, 0, f"{label} 重复声明 g{number:02d}")
            continue
        seen.add(number)
        reason = item.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < MIN_GAP_REASON_CHARS:
            report.block("registry", rel, 0,
                         f"{label} 缺少可核查理由（reason 至少 {MIN_GAP_REASON_CHARS} 字符）")
            continue
        if number not in gaps:
            report.block("registry", rel, 0,
                         f"{label} 声明 g{number:02d} 并非当前编号缺口（声明不得覆盖已有门禁/过期）")
            continue
        valid.add(number)
    return valid


def check_numbering(identifiers: list[str], registry: dict, registry_path: Path,
                    root: Path, report: Report) -> None:
    """H-7：编号缺口必须可解释；重复编号同样判红。

    自检沙箱夹具用的是 ``ok`` / ``bad`` 这类非 gNN- 形式的 id，没有编号可判，
    此时直接跳过，避免给夹具制造假红。
    """
    nums = [int(m.group(1)) for ident in identifiers
            if (m := GATE_ID_RE.match(ident))]
    if not nums:
        return
    duplicates = sorted({n for n in nums if nums.count(n) > 1})
    if duplicates:
        report.block("registry", _rel(registry_path, root), 0,
                     f"门禁编号重复：{', '.join(f'g{n:02d}' for n in duplicates)}")
    present = set(nums)
    gaps = [n for n in range(0, max(present) + 1) if n not in present]
    declared = declared_numbering_gaps(registry, gaps, registry_path, root, report)
    allowed = KNOWN_NUMBERING_GAPS | declared
    unexpected = [n for n in gaps if n not in allowed]
    if unexpected:
        builtin = ', '.join(f"g{n:02d}" for n in sorted(KNOWN_NUMBERING_GAPS)) or '（无）'
        report.block("registry", _rel(registry_path, root), 0,
                     f"门禁编号出现未声明缺口：{', '.join(f'g{n:02d}' for n in unexpected)}；"
                     f"内置白名单 {builtin}；其余须在 {DECLARED_GAPS_KEY} 逐条声明并给出理由"
                     f"（声明必须是真缺口、理由不少于 {MIN_GAP_REASON_CHARS} 字符）")


def inspect(root: Path, registry_path: Path, rules_path: Path, python: str) -> Report:
    """Judge every registered gate and return the classified report."""
    report = Report(title=TITLE)
    if not registry_path.is_file():
        raise GateError(f"registry not found: {registry_path}")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GateError(f"{registry_path}: invalid JSON: {error}") from error
    entries = registry.get("gates")
    if not isinstance(entries, list) or not entries:
        raise GateError(f"{registry_path}: 'gates' must be a non-empty list")
    tracked = set(govlib.tracked_files(root))
    identifiers = [str(entry.get("id", "<missing id>")) for entry in entries
                    if isinstance(entry, dict)]
    # H-7：先审「审计清单本身」——未登记的门禁脚本 / 未声明的编号缺口，
    # 再逐条审 registry 里登记过的门禁。
    check_gate_inventory(root, registry_path, set(identifiers), report)
    check_numbering(identifiers, registry, registry_path, root, report)
    missing_support, skipped_support = untracked_support(root, registry_path, tracked)
    for missing in missing_support:
        report.block("untracked-support", missing, 0,
                     f"{missing}: present on disk but absent from the git index (an ignore rule can hide it)")
    if skipped_support:
        # 排除必须留痕：否则「少了 20 条」与「少了 20 个真缺陷」在日志里长得一样。
        report.note(f"untracked-support: 跳过 {skipped_support} 个位于依赖安装面/虚拟环境"
                    f"（{' / '.join(sorted(ENV_DIR_NAMES))}）或本仓之外的 .py —— 它们不是本仓支撑模块")
    rules_text = rules_path.read_text(encoding="utf-8") if rules_path.is_file() else ""
    if not rules_text:
        report.block("registry", _rel(rules_path, root), 0, "RULES.md is missing or empty")

    for entry in entries:
        if not isinstance(entry, dict):
            report.block("registry", _rel(registry_path, root), 0, f"entry is not an object: {entry!r}")
            continue
        identifier = str(entry.get("id", "<missing id>"))
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            report.block("registry", _rel(registry_path, root), 0,
                         f"{identifier}: missing field(s) {', '.join(missing)}")
            continue
        if not entry["rules"]:
            report.block("registry", _rel(registry_path, root), 0, f"{identifier}: declares no rules")
        # Registry script paths are expressed relative to the governance subtree's
        # parent (tools/gate) — matching the base run.ts uses to resolve gate
        # scripts. Derive it from the *registry file* we were handed, not the module
        # constant GOVERNANCE_DIR: the meta gate's own --selfcheck runs against a
        # Sandbox() copy whose registry lives under a temp dir, so a fixed constant
        # would point at the real tree and the fixture scripts would never resolve.
        # 2026-11-25：提仓后同一份 registry 还要服务**包内布局**（gates/ 就在
        # registry 旁边），两种基址由 resolve_gate_script() 统一处理。
        script = resolve_gate_script(registry_path, str(entry["script"]))
        if not script.is_file():
            report.block("registry", _rel(registry_path, root), 0,
                         f"{identifier}: script not found at {entry['script']}")
            continue
        report.scanned += 1

        if identifier not in rules_text:
            report.block("rules-map", _rel(rules_path, root), 0,
                         f"{identifier}: not documented in RULES.md")

        code, help_text = run_gate_script(python, script, ["--help"], root)
        if code != 0:
            report.block("contract", _rel(script, root), 0, f"{identifier}: --help exited {code}")
            continue
        for switch in REQUIRED_SWITCHES:
            if switch not in help_text:
                report.block("contract", _rel(script, root), 0,
                             f"{identifier}: does not expose {switch}")
        # A gate that claims to read the host profile must be able to stop doing so,
        # because the aggregate runs repository-only.
        if entry.get("profileProbe") is True and "--no-profile" not in help_text:
            report.block("contract", _rel(script, root), 0,
                         f"{identifier}: declares profileProbe but does not expose --no-profile")

        if entry.get("selfcheck") is not True:
            report.block("contract", _rel(script, root), 0,
                         f"{identifier}: registry does not claim a self-check")
            continue
        # 平台不匹配的门禁不能要求它在**本机**自证。
        # 起因（2026-10-31，真实 CI 事故）：`g20-facts` 在 registry 里声明
        # ``platforms: ["nt"]``（它判的是 Windows 本机磁盘路径的存在性声称），
        # 但本循环此前**不看 platforms 字段**，一律跑 `--selfcheck`。
        # 于是 Linux CI 上 g20 的自检必然红：它注入的「破坏用例」需要在 NT 上
        # 才成立，在 posix 上连用例本身都跑不起来 → selfcheck 报
        # 「injected no defect it could catch」+「2 false positive(s)」→ g00 红
        # → 整条流水线红，而真正的结论是「这道门禁在 Linux 上不适用」。
        #
        # 处置口径（与 SPEC-002 / 门禁契约的「SKIP ≠ 通过」一致）：
        # **记 SKIP 并计数，不算通过、也不算失败**。这样 Linux CI 的结论是
        # 「19 道自证 + 1 道按平台跳过」，而不是假装 20 道全过。
        platforms = entry.get("platforms") or []
        if platforms and CURRENT_PLATFORM not in platforms:
            report.skip(f"{identifier}: not applicable on {CURRENT_PLATFORM} "
                        f"(declares {','.join(str(p) for p in platforms)})")
            continue
        # 2026-11-25：门禁自检跑在**同一棵被判的仓**上。``--root`` 只在对方的
        # --help 里真的出现时才传（契约里它还不是 REQUIRED_SWITCHES，出厂骨架
        # 未必带它），这样既让真实门禁不靠包的位置猜仓根，又不会给合法骨架造假红。
        root_flag = ["--root", str(root)] if "--root" in help_text else []
        code, output = run_gate_script(python, script, ["--selfcheck", *root_flag], root)
        match = SELFCHECK_LINE.search(output)
        if match is None:
            report.block("selfcheck", _rel(script, root), 0,
                         f"{identifier}: self-check printed no SELFCHECK summary")
            continue
        payload = json.loads(match.group(1))
        caught = int(payload.get("caught", 0))
        clean = int(payload.get("clean", 0))
        misreports = int(payload.get("misreports", 0))
        if code != 0:
            report.block("selfcheck", _rel(script, root), 0,
                         f"{identifier}: self-check exited {code}")
        if caught < 1:
            report.block("selfcheck", _rel(script, root), 0,
                         f"{identifier}: self-check injected no defect it could catch")
        if clean < 1:
            report.block("selfcheck", _rel(script, root), 0,
                         f"{identifier}: self-check proved nothing about clean input")
        if misreports != 0:
            report.block("selfcheck", _rel(script, root), 0,
                         f"{identifier}: self-check reported {misreports} false positive(s)")
        if code == 0 and caught >= 1 and clean >= 1 and misreports == 0:
            report.note(f"{identifier}: contract ok (caught {caught}, clean {clean})")
    return report


def _fixture_gate(epilogue: str) -> str:
    """Build a compliant gate skeleton for the self-check, with a chosen ending."""
    return (
        "import argparse, json, sys\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--selfcheck', action='store_true')\n"
        "parser.add_argument('--quiet', action='store_true')\n"
        "parser.add_argument('--baseline')\n"
        "parser.add_argument('--no-baseline', action='store_true')\n"
        "parser.add_argument('--write-baseline', action='store_true')\n"
        "parser.add_argument('--list-all', action='store_true')\n"
        "args = parser.parse_args()\n"
        + epilogue
    )


def selfcheck() -> int:
    """Prove the meta gate rejects a broken gate and accepts a compliant one."""
    check = govlib.SelfCheck(gate=GATE)
    good_gate = _fixture_gate(
        "if args.selfcheck:\n"
        "    print('SELFCHECK ' + json.dumps({'caught': 1, 'clean': 1, 'misreports': 0}, separators=(',', ':')))\n"
        "sys.exit(0)\n"
    )
    bad_gate = _fixture_gate("sys.exit(1)\n")
    silent_gate = _fixture_gate("sys.exit(0)\n")
    with govlib.Sandbox() as sandbox:
        sandbox.write("governance/gates/ok_gate.py", good_gate)
        sandbox.write("governance/gates/bad_gate.py", bad_gate)
        sandbox.write("governance/gates/silent_gate.py", silent_gate)
        # H-7：进了 git index、却没登记进 registry 的门禁脚本 —— 聚合器永远不会跑它。
        sandbox.write("governance/gates/g99_rogue.py", good_gate)
        registry = {
            "version": 1,
            "gates": [
                {"id": "ok", "title": "ok", "script": "governance/gates/ok_gate.py", "tier": "P0",
                 "rules": ["r1"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True},
                {"id": "bad", "title": "bad", "script": "governance/gates/bad_gate.py", "tier": "P0",
                 "rules": ["r2"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True},
                {"id": "silent", "title": "silent", "script": "governance/gates/silent_gate.py", "tier": "P0",
                 "rules": ["r3"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True},
                {"id": "undocumented", "title": "undocumented", "script": "governance/gates/ok_gate.py",
                 "tier": "P0", "rules": ["r4"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True},
                {"id": "incomplete", "title": "incomplete", "script": "governance/gates/ok_gate.py"},
                # Claims to read the host profile while exposing no way to stop.
                {"id": "profile-claim", "title": "profile-claim", "script": "governance/gates/ok_gate.py",
                 "tier": "P0", "rules": ["r5"], "platforms": ["nt", "posix"], "requires": ["python"],
                 "selfcheck": True, "profileProbe": True},
                # 声明一个「非本机」的平台：它必须被跳过并留痕，而不是判红。
                # 平台取反而不是写死 posix —— 因为夹具要在两种宿主上都跑得通。
                {"id": "other-platform", "title": "other-platform", "script": "governance/gates/ok_gate.py",
                 "tier": "P0", "rules": ["r6"],
                 "platforms": ["posix" if CURRENT_PLATFORM == "nt" else "nt"],
                 "requires": ["python"], "selfcheck": True},
            ],
        }
        sandbox.write("governance/registry.json", json.dumps(registry, indent=2) + "\n")
        sandbox.write("governance/RULES.md",
                      "# Rules\n\n| id | rules |\n| --- | --- |\n| ok | r1 |\n| bad | r2 |\n"
                      "| silent | r3 |\n| incomplete | r5 |\n| profile-claim | r5 |\n"
                      "| other-platform | r6 |\n")

        report = inspect(sandbox.path, sandbox.path / "governance/registry.json",
                         sandbox.path / "governance/RULES.md", sys.executable)
        blocked = {f"{finding.kind}:{finding.text.split(':')[0]}" for finding in report.findings}
        check.detects("self-check that exits non-zero", any(kind.startswith("selfcheck:bad") for kind in blocked))
        check.detects("self-check that prints no summary",
                      any(kind.startswith("selfcheck:silent") for kind in blocked))
        check.detects("gate missing from RULES.md",
                      any(kind.startswith("rules-map:undocumented") for kind in blocked))
        check.detects("registry entry missing fields",
                      any(kind.startswith("registry:incomplete") for kind in blocked))
        check.detects("a gate claiming a profile probe it cannot disable",
                      any("profile-claim" in finding.text for finding in report.findings))
        # H-7：gates/ 里有未登记的 gXX_*.py ⇒ 必须判红，否则该门禁永不执行。
        check.detects("an undeclared gXX gate script",
                      any(finding.kind == "rogue-gate" and "g99_rogue.py" in finding.text
                          for finding in report.findings))
        # 平台跳过自证（2026-10-31 新增）：声明「不支持本机平台」的门禁必须被
        # **跳过而不是判红**，且跳过要留痕（SKIP ≠ 通过）。
        # 这个夹具补的是一个真实事故：g20-facts 声明 platforms=["nt"]，
        # 而本循环曾不看该字段 → Linux CI 上它的自检必然红，整条流水线跟着红。
        check.detects("platform skip leaves a trace",
                      any("not applicable on" in reason for reason in report.skipped))
        check.quiet("compliant gate", any(kind.startswith(("selfcheck:ok", "contract:ok", "rules-map:ok"))
                                          for kind in blocked))

    with govlib.Sandbox() as sandbox:
        sandbox.write("governance/gates/ok_gate.py", good_gate)
        sandbox.write("governance/RULES.md", "# Rules\n\n| ok |\n")
        sandbox.write("governance/registry.json", json.dumps({"version": 1, "gates": [
            {"id": "ok", "title": "ok", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r1"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True}]}) + "\n")
        clean = inspect(sandbox.path, sandbox.path / "governance/registry.json",
                        sandbox.path / "governance/RULES.md", sys.executable)
        check.quiet("a fully compliant registry", bool(clean.findings))

    with govlib.Sandbox() as sandbox:
        sandbox.write("governance/gates/ok_gate.py", good_gate)
        # On disk but never added: exactly how an ignore rule hides a support module.
        sandbox.write("governance/runtime/hidden.py", "# support module kept out of the index\n", track=False)
        sandbox.write("governance/RULES.md", "# Rules\n\n| ok |\n")
        sandbox.write("governance/registry.json", json.dumps({"version": 1, "gates": [
            {"id": "ok", "title": "ok", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r1"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True}]}) + "\n")
        report = inspect(sandbox.path, sandbox.path / "governance/registry.json",
                         sandbox.path / "governance/RULES.md", sys.executable)
        check.detects("a support module missing from the index",
                      any(finding.kind == "untracked-support" for finding in report.findings))

    # 负对照 A（2026-11-25，第 141 行 ENV_DIR_NAMES 的正向证明）：
    # 同一棵子树里，**安装面**上的 .py 是在盘不在 index 的，但它们不是本仓支撑模块，
    # 判据必须闭嘴。不排除的话，跑已安装的包时 20 道门禁会各判一条 untracked-support
    # （提仓后真实发生过）。
    with govlib.Sandbox() as sandbox:
        sandbox.write("governance/gates/ok_gate.py", good_gate)
        sandbox.write("governance/.venv/Lib/site-packages/allin_governance/_gatesuite/gates/g01_paths.py",
                      good_gate, track=False)
        sandbox.write("governance/.venv/Lib/site-packages/allin_governance/_gatesuite/runtime/govlib.py",
                      "# installed copy\n", track=False)
        sandbox.write("governance/node_modules/pkg/helper.py", "# vendored dep\n", track=False)
        sandbox.write("governance/__pycache__/cached.py", "# bytecode stand-in\n", track=False)
        sandbox.write("governance/RULES.md", "# Rules\n\n| ok |\n")
        sandbox.write("governance/registry.json", json.dumps({"version": 1, "gates": [
            {"id": "ok", "title": "ok", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r1"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True}]}) + "\n")
        report = inspect(sandbox.path, sandbox.path / "governance/registry.json",
                         sandbox.path / "governance/RULES.md", sys.executable)
        check.quiet("files on an install surface (venv / site-packages / node_modules / __pycache__)",
                    any(finding.kind == "untracked-support" for finding in report.findings))

    # 负对照 B（同上，反向）：判据原本要抓的那个缺陷 —— **仓内目录被 .gitignore 吞掉**
    # 而 git status 沉默 —— 必须照旧判红。这一条是为了证明「排除安装面」没有
    # 顺手把整条判据关掉（上一轮吃过「判据空转还报绿」的亏）。
    with govlib.Sandbox() as sandbox:
        sandbox.write(".gitignore", "lib/\n")
        sandbox.write("governance/gates/ok_gate.py", good_gate)
        sandbox.write("governance/RULES.md", "# Rules\n\n| ok |\n")
        sandbox.write("governance/registry.json", json.dumps({"version": 1, "gates": [
            {"id": "ok", "title": "ok", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r1"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True}]}) + "\n")
        # governance/lib/x.py 被 .gitignore 的 `lib/` 规则吞掉：git add 会失败，
        # 所以只能落盘不入库 —— 这正是「git status 沉默的丢失」。
        sandbox.write("governance/lib/swallowed.py", "# hidden by the repo-wide lib/ rule\n", track=False)
        report = inspect(sandbox.path, sandbox.path / "governance/registry.json",
                         sandbox.path / "governance/RULES.md", sys.executable)
        check.detects("a support module inside a .gitignore-swallowed directory",
                      any(finding.kind == "untracked-support" and "swallowed.py" in finding.text
                          for finding in report.findings))

    # H-7 反向：白名单只声明了 g02，故意缺 g01 ⇒ 必须判红。
    with govlib.Sandbox() as sandbox:
        sandbox.write("governance/gates/ok_gate.py", good_gate)
        sandbox.write("governance/RULES.md", "# Rules\n\n| g00-a | r1 |\n| g02-c | r2 |\n")
        sandbox.write("governance/registry.json", json.dumps({"version": 1, "gates": [
            {"id": "g00-a", "title": "a", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r1"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True},
            {"id": "g02-c", "title": "c", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r2"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True}]}) + "\n")
        report = inspect(sandbox.path, sandbox.path / "governance/registry.json",
                         sandbox.path / "governance/RULES.md", sys.executable)
        check.detects("an undeclared numbering hole",
                      any("未声明缺口" in finding.text for finding in report.findings))

    # H-7 正向：本仓真实形态「缺 g02」在白名单内，不得误报（防真实跑批被拖红）。
    with govlib.Sandbox() as sandbox:
        sandbox.write("governance/gates/ok_gate.py", good_gate)
        sandbox.write("governance/RULES.md", "# Rules\n\n| g00-a | r1 |\n| g01-b | r2 |\n| g03-c | r3 |\n")
        sandbox.write("governance/registry.json", json.dumps({"version": 1, "gates": [
            {"id": "g00-a", "title": "a", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r1"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True},
            {"id": "g01-b", "title": "b", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r2"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True},
            {"id": "g03-c", "title": "c", "script": "governance/gates/ok_gate.py", "tier": "P0",
             "rules": ["r3"], "platforms": ["nt", "posix"], "requires": ["python"], "selfcheck": True}]}) + "\n")
        report = inspect(sandbox.path, sandbox.path / "governance/registry.json",
                         sandbox.path / "governance/RULES.md", sys.executable)
        check.quiet("the whitelisted g02 numbering hole",
                    any("未声明缺口" in finding.text for finding in report.findings))

    # 声明式编号缺口（2026-11-13）：出厂子集只有 14 道门禁，g14~g19 是
    # 治理中枢专属件、不随包出厂。判据允许它们在 registry 里逐条声明，
    # 但声明本身必须能被核查 —— 下面四组分别证明「合法声明放行」与
    # 「缺理由 / 声明非缺口 / 重复声明」三种伪造都必须判红。
    def declared_gap_sandbox(entries: list[dict]) -> Report:
        with govlib.Sandbox() as sandbox:
            sandbox.write("governance/gates/ok_gate.py", good_gate)
            sandbox.write("governance/RULES.md", "# Rules\n\n| g00-a | r1 |\n| g02-c | r2 |\n")
            sandbox.write("governance/registry.json", json.dumps({
                "version": 1,
                DECLARED_GAPS_KEY: entries,
                "gates": [
                    {"id": "g00-a", "title": "a", "script": "governance/gates/ok_gate.py",
                     "tier": "P0", "rules": ["r1"], "platforms": ["nt", "posix"],
                     "requires": ["python"], "selfcheck": True},
                    {"id": "g02-c", "title": "c", "script": "governance/gates/ok_gate.py",
                     "tier": "P0", "rules": ["r2"], "platforms": ["nt", "posix"],
                     "requires": ["python"], "selfcheck": True},
                ],
            }, indent=2) + "\n")
            return inspect(sandbox.path, sandbox.path / "governance/registry.json",
                           sandbox.path / "governance/RULES.md", sys.executable)

    valid_reason = "g01 属治理中枢专属门禁，不随通用治理套装出厂"
    legal = declared_gap_sandbox([{"number": 1, "reason": valid_reason}])
    check.quiet("a declared numbering gap with a verifiable reason",
                any("未声明缺口" in finding.text for finding in legal.findings))
    no_reason = declared_gap_sandbox([{"number": 1, "reason": "略"}])
    check.detects("a declared gap without a verifiable reason",
                  any("缺少可核查理由" in finding.text for finding in no_reason.findings))
    not_a_gap = declared_gap_sandbox([{"number": 0, "reason": valid_reason}])
    check.detects("a declaration that is not an actual gap",
                  any("并非当前编号缺口" in finding.text for finding in not_a_gap.findings))
    duplicated = declared_gap_sandbox([
        {"number": 1, "reason": valid_reason},
        {"number": 1, "reason": valid_reason},
    ])
    check.detects("a duplicated gap declaration",
                  any("重复声明" in finding.text for finding in duplicated.findings))
    return check.finish()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--root", default=None, help="work tree to judge (default: discovered from this file)")
    parser.add_argument("--registry", default=None, help="registry file (default: governance/registry.json)")
    parser.add_argument("--rules", default=None, help="rules map (default: governance/RULES.md)")
    parser.add_argument("--python", default=sys.executable, help="interpreter used to run each gate")
    parser.add_argument("--quiet", action="store_true", help="print the summary line only")
    parser.add_argument("--selfcheck", action="store_true", help="inject fixtures and prove the gate works")
    govlib.add_baseline_argument(parser)
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()

    # The governance subtree lives at <root>/tools/gate/governance/, so the work
    # tree root is discovered by walking up to the git toplevel (layout-independent)
    # rather than assuming a fixed number of parent segments.
    # 2026-11-25：``--root`` 一旦给出就直接用，不再被上溯覆盖 —— 套件跑在已安装的
    # 包里时，「正在判哪棵仓」只能由调用方说，不能由包的位置猜（见 resolve_root）。
    root = govlib.resolve_root(args.root)
    registry = Path(args.registry) if args.registry else govlib.GOVERNANCE_DIR / "registry.json"
    rules = Path(args.rules) if args.rules else govlib.GOVERNANCE_DIR / "RULES.md"
    report = inspect(root, registry, rules, args.python)
    print(report.render().splitlines()[0] if args.quiet else report.render())
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(govlib.run_gate(main))
