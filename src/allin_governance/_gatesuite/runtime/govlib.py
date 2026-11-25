"""Shared runtime for the governance gates of this distribution repository.

Every gate under ``governance/gates/`` imports this module, so four contracts
have exactly one implementation:

* the **exit-code vocabulary** (``0`` clean, ``1`` new blocking finding outside
  the baseline, ``2`` usage error, ``3`` environment error),
* **fail-loud repository discovery** -- a gate refuses to run when it cannot
  name the work tree it is judging, instead of silently scanning nothing,
* the **baseline ratchet**, in both directions -- existing findings are frozen once
  so a gate can switch on while the repository is still being cleaned up, and the
  frozen set is only ever allowed to **shrink, because something was fixed**
  (growth needs an explicit opt-in, and a key that no longer fires is a blocking
  stale bill),
* the **self-check harness** -- a gate proves both that it still catches an
  injected defect and that it still stays quiet on clean input.

Reported counts are never dropped silently: every exemption and every skip is
printed with a reason and a count, because an unaccounted exclusion is how a
gate reports "clean" while a whole class of defects walks through.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

#: Clean run: nothing blocking, or nothing outside the frozen baseline.
EXIT_OK = 0
#: At least one blocking finding that the baseline does not cover.
EXIT_BLOCKING = 1
#: The caller asked for something the gate does not support.
EXIT_USAGE = 2
#: The environment cannot support the judgment (no interpreter, no work tree).
EXIT_ENV = 3

#: ``governance/`` -- this module lives in ``governance/lib/``.
GOVERNANCE_DIR = Path(__file__).resolve().parent.parent

#: Git must never wait for a terminal in an unattended run.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_PAGER": "cat",
    "LC_ALL": "C.UTF-8",
}


def git_env() -> dict[str, str]:
    """The process environment plus the no-prompt guard.

    Merging is the whole point: replacing the environment outright drops
    ``HOME``/``USERPROFILE``, so git cannot read the user's global config --
    including the ``safe.directory`` exceptions that make a checkout owned by
    another account readable at all. A gate that loses them either stops loudly
    (g03, g05) or, worse, reads an empty ``stdout`` as "nothing is wrong" (g12).
    """
    return {**os.environ, **GIT_ENV}


#: How many sample findings a gate prints before summarizing the rest.
SAMPLE_LIMIT = 20


def configure_stdio() -> None:
    """Force UTF-8 with LF-only line endings so output is identical on every host.

    Windows console translation would otherwise turn every ``\\n`` into ``\\r\\n``
    and break line-anchored parsing of a gate's own output.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace", newline="\n")


class GateError(RuntimeError):
    """A failure that must exit non-zero with a named, actionable cause."""

    def __init__(self, message: str, code: int = EXIT_ENV) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# repository discovery and git plumbing
# --------------------------------------------------------------------------- #


def repo_root(start: Path | None = None) -> Path:
    """Return the work-tree root, discovered from this file's location.

    The default value is derived, never hard-coded: the gate ships inside the
    repository, so walking up from ``governance/lib/`` finds the root on every
    machine. Passing an explicit ``--root`` (used by the self-check) overrides it.
    """
    current = (start or GOVERNANCE_DIR).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise GateError(
        f"no git work tree above {current}; run the gate from inside the repository "
        "or pass --root <work-tree>"
    )


def resolve_root(value: str | None = None) -> Path:
    """The work tree to judge: an explicit ``--root`` wins, otherwise discovery.

    Why this exists (2026-11-25, package extraction): the suite now runs from
    *inside an installed package*, so ``--root`` is the only way to say which
    repository is being judged. Walking up from ``GOVERNANCE_DIR`` still works
    when the package sits in a venv inside that repository, but it silently
    answers a different question once the package lives anywhere else (a global
    install, a shared tool env, another project's venv) -- and "silently judges
    the wrong tree" is the worst failure mode a gate has.

    A caller that passes ``--root`` gets exactly that directory; it is never
    re-discovered, and a missing/non-directory value is a usage error (exit 2)
    rather than an environment error (exit 3): the operator mistyped, the
    environment is fine.
    """
    if value is None or str(value).strip() == "":
        return repo_root()
    candidate = Path(str(value)).expanduser().resolve()
    if not candidate.is_dir():
        raise GateError(f"--root 不是目录：{value}", EXIT_USAGE)
    return candidate


def current_machine() -> str:
    """Current hostname, lowercased.

    The single implementation of "which machine am I": ``g20-facts`` judges the
    partition file whose stem equals this, ``g19-docs-refs`` exempts every other
    machine's file (SPEC-001 铁律 6「门禁同源」—— 机器事实的「写权限」与
    「校验范围」都按 hostname 收敛). Two copies of this one-liner is exactly how
    the two gates would drift apart, so it lives here.
    """
    return platform.node().lower()


def run_git_result(args: list[str], root: Path) -> subprocess.CompletedProcess[bytes]:
    """Run git and hand back the process, for callers that judge the exit code themselves."""
    command = ["git", "-c", "core.quotepath=false", *args]
    return subprocess.run(
        command,
        cwd=str(root),
        env=git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_git(args: list[str], root: Path, check: bool = True) -> str:
    """Run git with prompts disabled, path quoting off, and UTF-8 decoding."""
    completed = run_git_result(args, root)
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"git {' '.join(args)} failed in {root}: {detail}")
    return completed.stdout.decode("utf-8", "replace")


def tracked_files(root: Path) -> list[str]:
    """Every path in the index, in git's own order, with ``/`` separators."""
    raw = run_git(["ls-files", "-z"], root)
    files = [entry for entry in raw.split("\0") if entry]
    if not files:
        raise GateError(f"{root} lists no tracked files; refusing to judge an empty corpus")
    return files


def blob_of(root: Path, path: str) -> str:
    """The staged content of one tracked path, or an empty string when absent."""
    return run_git(["show", f":{path}"], root, check=False)


# --------------------------------------------------------------------------- #
# file reading
# --------------------------------------------------------------------------- #


def read_text(path: Path, limit_bytes: int | None = None) -> str | None:  # gov-python: allow
    """Read UTF-8 text, or return ``None`` for missing, binary, or oversized input.

    Why the annotation: `g09-python-hygiene` flags any `read_text(` that does not
    spell ``encoding=`` on the same line, because the stdlib `Path.read_text`
    defaults to the locale codec. This one is a **wrapper**: it reads bytes and
    decodes explicitly below, so the locale never gets a vote. The gate's
    local-helper heuristic only recognises a helper whose body contains
    ``encoding=`` (it looks for the keyword, not for ``.decode("utf-8")``), so
    this safe wrapper reads as a defect. Annotating is the mechanism the gate
    itself documents for that gap; broadening the heuristic is the better fix
    and is left as a known limitation (2026-10-31).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if limit_bytes is not None:
        data = data[:limit_bytes]
    if b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


def line_of(text: str, offset: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, offset) + 1


def line_text(text: str, offset: int) -> str:
    """The full source line containing a character offset."""
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return text[start:] if end == -1 else text[start:end]


def digest(value: str) -> str:
    """Short stable hash used to key baseline entries by content, not line number."""
    # SHA1 此处仅作「内容 → 稳定短键」的非安全用途（基线按内容索引而非行号），
    # 不是签名/口令哈希。显式声明 usedforsecurity=False，消除 bandit B324 假阳性。
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    """One blocking defect, identified by file, line, and content key."""

    path: str
    line: int
    text: str
    kind: str
    key: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            self.key = f"{self.path}|{digest(self.text.strip())}|{self.kind}"


@dataclass
class Report:
    """The outcome of one gate run: what blocked, what was exempted, what was skipped."""

    title: str
    findings: list[Finding] = field(default_factory=list)
    exempted: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    scanned: int = 0

    def block(self, kind: str, path: str, line: int, text: str) -> None:
        self.findings.append(Finding(path=path, line=line, text=text, kind=kind))

    def exempt(self, reason: str, count: int = 1) -> None:
        """Record an exclusion so the count stays visible in the output."""
        self.exempted[reason] += count

    def skip(self, reason: str, count: int = 1) -> None:
        """Record work the gate could not perform. A skip is never a pass."""
        self.skipped[reason] += count

    def note(self, message: str) -> None:
        self.notes.append(message)

    def exit_code(self, findings: list | None = None) -> int:
        """门禁统一出口：blocking > 严格模式下的 skip > clean。

        ``findings`` 供「基线过滤后判红」的门禁传入 ``new_findings``：这些门禁在
        ``--list-all`` 时会把 ``report.findings`` 换成**全量**列表，若直接用
        self.findings 判定，列出基线内条目也会被误判成 blocking。传显式列表即可
        把「展示用 findings」与「判红用 findings」分开。

        2026-11-10 深度审计 H-2：此前 11 道门禁绕过本方法直接返回
        ``EXIT_BLOCKING if new_findings else EXIT_OK``，使 ``ALLIN_GATE_STRICT_SKIP=1``
        只对其余门禁生效 —— 同一个环境变量在不同门禁上语义不一致，本身就是假绿。
        """
        effective = self.findings if findings is None else findings
        if effective:
            return EXIT_BLOCKING
        # 2026-11-08 深度审核 A1：SKIP ≠ pass。此前 render() 明写
        # 「SKIPPED (not a pass)」，退出码却恒 0，聚合器把整类未检查当成 clean。
        # 默认保持兼容（部分门禁在无对象时会记 skip）；严格模式下由
        # ALLIN_GATE_STRICT_SKIP=1 让「没检查」以非零退出码暴露，供 CI 准出消费。
        if self.skipped and os.environ.get('ALLIN_GATE_STRICT_SKIP') == '1':
            return EXIT_USAGE
        return EXIT_OK

    def render(self, sample_limit: int = SAMPLE_LIMIT) -> str:
        lines = [
            f"{self.title}: scanned {self.scanned} file(s); "
            f"blocking {len(self.findings)}; exempted {sum(self.exempted.values())}; "
            f"skipped {sum(self.skipped.values())}."
        ]
        for index, finding in enumerate(self.findings[:sample_limit]):
            lines.append(f"  BLOCK {finding.path}:{finding.line}: {finding.kind}: {finding.text.strip()[:160]}")
        remaining = len(self.findings) - sample_limit
        if remaining > 0:
            lines.append(f"  ... and {remaining} more blocking finding(s)")
        if self.exempted:
            detail = ", ".join(f"{reason}={count}" for reason, count in sorted(self.exempted.items()))
            lines.append(f"  exempted: {detail}")
        if self.skipped:
            detail = ", ".join(f"{reason}={count}" for reason, count in sorted(self.skipped.items()))
            lines.append(f"  SKIPPED (not a pass): {detail}")
        lines.extend(f"  note: {message}" for message in self.notes)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# baseline ratchet
# --------------------------------------------------------------------------- #


class Baseline:
    """A frozen set of finding keys; only keys outside it block a run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: set[str] = set()

    def load(self) -> "Baseline":
        if self.path.is_file():
            # gov-python: allow —— 调的是本文件的 read_text 包装，编码已写死。
            for entry in read_text(self.path, limit_bytes=4 * 1024 * 1024).splitlines() or []:
                entry = entry.strip()
                if entry and not entry.startswith("#"):
                    self.keys.add(entry)
        return self

    def filter_new(self, findings: list[Finding]) -> list[Finding]:
        return [finding for finding in findings if finding.key not in self.keys]

    def stale(self, findings: list[Finding]) -> list[str]:
        live = {finding.key for finding in findings}
        return sorted(self.keys - live)

    def write(self, findings: list[Finding]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "# Baseline ratchet for this gate. One finding key per line.",
            "# Refresh only after the remaining findings were reviewed in full:",
            "#   python governance/gates/<gate>.py --no-baseline --list-all",
        ]
        body.extend(sorted({finding.key for finding in findings}))
        self.path.write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")


def add_baseline_argument(parser) -> None:
    """Install the four baseline switches every gate exposes."""
    parser.add_argument("--baseline", default=None, help="baseline file (default: governance/baseline/<gate>.txt)")
    parser.add_argument("--no-baseline", action="store_true", help="ignore the baseline and judge everything")
    parser.add_argument("--write-baseline", action="store_true", help="freeze current findings as the baseline")
    parser.add_argument("--list-all", action="store_true", help="print every finding, not just new ones")


#: 基线在**被判的仓**里的落点（相对仓根）：消费仓把套件放在 tools/gate/governance 时的位置。
BASELINE_SUBTREE = "tools/gate/governance/baseline"
#: 同一份基线在**本仓**（源码在 src/allin_governance/_gatesuite 下）里的位置。
BASELINE_SUBTREE_PACKAGE = "src/allin_governance/_gatesuite/baseline"


def baseline_path(root: Path, gate_id: str, explicit: str | None = None) -> Path:
    """基线的落点：显式 ``--baseline`` > 仓内既有 > 仓内默认落点（待建）。

    为什么基准必须是**仓根**、不能是包的安装位置（2026-11-25 提仓后实测的缺陷）：
    基线棘轮的全部含义是「这些存量已复核、冻结在**这份提交**里」。基线一旦解析到
    ``site-packages``，它就不在任何 git 索引里 —— 不可评审、不可版本化、换台机器
    装同一个版本却可能看到另一份冻结集合。棘轮于是只剩「有个文件被读了」，
    失去了「接受的风险被记录过」这个唯一价值。

    两种仓内落点都认（与 ``g00`` 的脚本解析、``g18`` 的台账定位同一口径），
    都不存在时返回「仓内默认落点」——由 ``--write-baseline`` 去建，而不是
    悄悄写进包里。
    """
    if explicit:
        return Path(explicit)
    for subtree in (BASELINE_SUBTREE, BASELINE_SUBTREE_PACKAGE):
        candidate = root / subtree / f"{gate_id}.txt"
        if candidate.is_file():
            return candidate
    return root / BASELINE_SUBTREE / f"{gate_id}.txt"


def display_path(path: Path, root: Path) -> str:
    """报告用路径：仓内给相对路径，仓外退回绝对路径（只用于显示，不用于判断）。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def display_scope(path: Path, root: Path) -> str:
    """基线是**哪一份**：仓内 / 仓外（包内、任意路径）。

    「读到了哪一份」必须进输出，理由与 ``baseline_path`` 的 docstring 同一件事：
    基线解析到 ``site-packages`` 时，棘轮只剩「有个文件被读了」，而报告里看不出
    这个区别 —— 换台机器装同一个版本，同一条命令念的却是另一份账本。
    """
    try:
        path.resolve().relative_to(root.resolve())
        return "仓内"
    except ValueError:
        return "仓外（包内/任意路径）"


# --------------------------------------------------------------------------- #
# baseline ratchet -- 双向
# --------------------------------------------------------------------------- #
#
# 单向棘轮只回答「这条 finding 是不是存量」。它有两种坏法，都很安静：
#
# 1. **静默增长**：`--write-baseline` 把当下所有 finding 一把冻进去。基线于是
#    越写越长，新问题被顺手藏进默认 —— 比不加基线更坏，因为现在它连"没人管"
#    这个信号都消失了（ESLint 的 bulk suppressions 明确禁止这种一次性收编）。
# 2. **过期账单**：基线里的键已经不再命中（问题修好了，或被豁免/移出了判定
#    范围），却没人清账。账本只增不减，一年后没人敢信它，棘轮退化成装饰。
#
# 所以「只许因已修而缩短」必须由机制保证，而不是靠纪律提醒：
#
# * 写：**纯增长**（旧键集 ⊂ 新键集，一条都没修好、只是长胖）⇒ 拒绝写入、退出非 0、
#   **基线文件一个字节都不动**，除非显式 ``--allow-baseline-growth``（并在提交信息里
#   写明理由）。有修好、也有新增的「混账」允许写，但必须先打出 ``-fixed/-new`` 两列。
# * 读：基线里有、当下不命中 ⇒ 退出非 0 并指名哪些键 stale；``--pass-on-stale``
#   供人本地放行，**CI 禁止使用**（D1.4：CI 下出现即 exit 2，宁可红在参数解析）。
#
# 口径来源：ADR-0011 §D1（2026-11-25 accepted，"计数与基线从手写改为可判"）。
# 为什么拒「纯增长」而不是拒「任何增长」：修好 3 条又新增 1 条是**正常收账**，
# 对每一次正常收账都喊狼，喊到第三次就没人看开关了 —— 一个总在喊的开关等于没有开关。
#
# 两条都不进 ``Baseline`` 类：类是数据（键集合 + 落点），judgment 在这些函数里，
# 这样 2.4.x 让其余门禁接上时是**接线**，不是重写判据。

#: 基线文件的三种状态：不存在 / 存在但零键 / 存在且有键。
BASELINE_ABSENT = "absent"
BASELINE_EMPTY = "empty"
BASELINE_PRESENT = "present"


@dataclass
class BaselineRevision:
    """一次基线操作的决定、账目与**要打印的说明**。

    说明和决定放在同一个返回值里，是因为拒绝写入时"为什么不写、差几个键、
    差在哪、要放行该加什么开关"就是这件事的全部价值。让每道门禁各写一份措辞，
    同一个语义迟早有 16 种说法，而 16 种说法里的任何一条都可能被当成噪音跳过。
    """

    #: 这次操作是否被允许发生（写：允许写；查：账本是否诚实）。
    ok: bool
    #: 调用方应当返回的退出码。
    code: int
    #: 逐行输出（已含缩进，直接 print）。
    lines: list[str] = field(default_factory=list)
    state: str = BASELINE_ABSENT
    #: 这次写会让基线**变长**的键（= 想把新问题冻成默认）。
    added: list[str] = field(default_factory=list)
    #: 这次写会让基线**缩短**的键（= 已修）。
    removed: list[str] = field(default_factory=list)
    #: 查账时发现的过期键（基线里有、当下不命中）。
    stale: list[str] = field(default_factory=list)
    #: 查账时发现的**不在账内**的新键（当下 − 基线）。
    fresh: list[str] = field(default_factory=list)
    #: 机读计数行（ADR-0011 D1.2 要求 `scanned=… baseline=… stale=… new=…`）。
    counts: str = ""


def baseline_state(baseline: Baseline) -> str:
    """基线眼下处于三种状态里的哪一种。

    ``empty`` 与 ``absent`` 必须分开：空文件是「存量已清零」这条**已得出的结论**，
    而缺失是「从未记过账」。前者再变长就是把新问题藏进默认，后者是第一次建账 ——
    混成一种状态，就只能二选一：要么放过第一次建账，要么逼人给首次建账也加开关。
    """
    if not baseline.path.is_file():
        return BASELINE_ABSENT
    return BASELINE_PRESENT if baseline.keys else BASELINE_EMPTY


def _sample_keys(keys: list[str], limit: int = 3, width: int = 110) -> str:
    """键的样本：够人认出是哪一条，又不会把日志淹成一行墙。"""
    shown = "; ".join(key if len(key) <= width else key[:width] + "…" for key in keys[:limit])
    if len(keys) > limit:
        shown += f"（其余 {len(keys) - limit} 个略）"
    return shown


def baseline_write(
    baseline: Baseline,
    findings: list[Finding],
    root: Path,
    *,
    allow_growth: bool = False,
    reason: str = "",
) -> BaselineRevision:
    """把当下的 findings 冻结成基线；**默认拒绝「纯增长」**。

    ADR-0011 D1.3 的两分法（不是「任何增长都拒」）：

    * **纯增长**（旧键集 ⊂ 新键集：一条都没修好，只是长胖）⇒ 拒绝写入，除非
      ``allow_growth``。这是唯一会把新问题**藏进默认**的形态。
    * **混账 / 收缩**（有修好，可能也有新增）⇒ 允许写，但必须先打出
      ``-fixed <n>`` / ``+new <n>`` 两列：长度可能不变或变长，读者必须一眼看见
      「哪几条是这次修好的、哪几条是这次接管的」。

    收缩永远免费 —— 基线缩短的唯一合法解释就是"问题被修了"，那是棘轮在正转。

    拒绝时**不碰文件**（连父目录都不建）：为了证明"什么都没发生"，输出里给出
    落点，调用方可以自己 sha256 前后对比 —— 一个拒绝了却写了一半的基线，
    比干脆写下去更难查。
    """
    state = baseline_state(baseline)
    new_keys = {finding.key for finding in findings}
    added = sorted(new_keys - baseline.keys)
    removed = sorted(baseline.keys - new_keys)
    revision = BaselineRevision(ok=False, code=EXIT_BLOCKING, state=state,
                                added=added, removed=removed)
    where = display_path(baseline.path, root)
    scope = display_scope(baseline.path, root)
    # 纯增长：旧 ⊆ 新 且 旧 ≠ 新（空账本被撑长同样算 —— 那是「存量已清零」这条
    # 结论被悄悄推翻）。``{} ⊆ X`` 恒真，所以零键基线必须单独判，否则它最松。
    pure_growth = (state != BASELINE_ABSENT and baseline.keys != new_keys
                   and (not baseline.keys or baseline.keys < new_keys))

    if pure_growth and not allow_growth:
        existing = (f"既有基线 {len(baseline.keys)} 键" if state == BASELINE_PRESENT
                    else "既有基线为空（存量已清零的记录）")
        revision.lines = [
            f"refused to write baseline {baseline.path.resolve()}",
            f"  基线 {where} [{scope}] {existing}；本次是**纯增长**：新增 {len(added)} 键、"
            f"修好 0 键（{len(baseline.keys)} → {len(new_keys)}）：{_sample_keys(added)}",
            "  基线只许因「已修」而缩短：把新问题冻进默认，等于让它以后永远不再被拦"
            " —— 比不加基线更坏。",
            "  若这些确实是本次要一并接管的存量（已逐条复核过），显式加"
            " --allow-baseline-growth（建议同时给 --reason \"...\"），并在提交信息里写明理由。",
            "  未写入：基线文件保持原样（字节未变）。",
        ]
        return revision

    baseline.write(findings)
    revision.ok = True
    revision.code = EXIT_OK
    revision.lines = [
        f"wrote baseline {baseline.path.resolve()} ({len(new_keys)} key(s))"
        f" [{scope}]" + (" [首次建账]" if state == BASELINE_ABSENT else ""),
    ]
    if state != BASELINE_ABSENT:
        # 两列账目（D1.3）：一次写到底是「修了几条」还是「接管了几条」，
        # 不许只有一个总数 —— 总数相等的一删一增，读起来和"什么都没变"一样。
        revision.lines.append(f"  -fixed {len(removed)}  +new {len(added)}")
    if pure_growth:
        # 走到这里的纯增长只能是 allow_growth 放行的：放行也必须说出来 ——
        # 一次被允许的增长，日志里留不下痕迹就还是静默增长。
        revision.lines.append(
            f"  已允许增长：+{len(added)} 键（--allow-baseline-growth）：{_sample_keys(added)}")
        revision.lines.append(
            f"  reason: {reason.strip()}" if reason.strip()
            else "  未给 --reason：提交信息里必须写明「为什么这次可以变长」。")
    elif added and state != BASELINE_ABSENT:
        # 混账不是放行：它按 ADR D1.3 本来就允许写。措辞必须与「已允许增长」分开，
        # 否则读日志的人分不清"这次是政策允许的"和"这次是有人按了开关的"。
        revision.lines.append(
            f"  混账：修好 {len(removed)} 键的同时新增 {len(added)} 键 ⇒ 按 ADR-0011 D1.3 "
            f"允许写（无需开关）；新增：{_sample_keys(added)}")
    if state == BASELINE_ABSENT:
        # 残余缺口（与 ESLint 同一处）：删掉基线文件再 --write-baseline 会绕过增长拦阻。
        # 机制层拦不住"从没记过账"这个借口，只能让它在日志和 diff 里留下痕迹。
        revision.lines.append(
            f"  首次建账：{len(new_keys)} 键全部按存量接管；若此前曾有基线，"
            "删除后重写会绕过增长拦阻 —— 提交前看 git diff 里那份文件是不是被整体重写了。")
    return revision


def baseline_check(
    baseline: Baseline,
    findings: list[Finding],
    root: Path,
    *,
    pass_on_stale: bool = False,
    scanned: int | None = None,
) -> BaselineRevision:
    """查账：基线里当下已不再命中的键就是**过期账单**，默认判红。

    过期不是小事：要么问题修好了（那就该 ``--write-baseline`` 让它缩短），
    要么它被豁免或移出了判定范围（那账本本身在说谎）。两种情况的修法不同，
    但"继续留着"永远不对 —— 账本只增不减，久了没人敢信它。

    ``pass_on_stale`` 只为人放行：本地重构到一半、账本先不清是常态。CI 里用
    它是自杀 —— 一次放行之后，后续所有的 stale 都被同一句话盖住，账本从此
    不可信，而屏幕上永远只有一行绿。ADR-0011 D1.4 因此把它在 CI 下判成用法错误
    （见 :func:`ratchet_human_only`）。

    输出里必带机读计数行 ``scanned=<n> baseline=<n> stale=<n> new=<n>``（D1.2）：
    「挂了 12 条」与「一条都没有」在只有 exit code 的日志里长得一样，这四个数
    是唯一能区分它们的证据。
    """
    state = baseline_state(baseline)
    where = display_path(baseline.path, root)
    scope = display_scope(baseline.path, root)
    stale = baseline.stale(findings) if state != BASELINE_ABSENT else []
    fresh = sorted({finding.key for finding in findings} - baseline.keys)
    counts = (f"scanned={len(findings) if scanned is None else scanned}"
              f" baseline={len(baseline.keys)} stale={len(stale)} new={len(fresh)}")
    if state == BASELINE_ABSENT:
        return BaselineRevision(
            ok=True, code=EXIT_OK, state=state, fresh=fresh, counts=counts,
            lines=[f"baseline: {where} 不存在 ⇒ 棘轮失效，本次按全量判定"
                   "（要冻结存量用 --write-baseline）",
                   f"  counts: {counts}"])
    revision = BaselineRevision(ok=not stale or pass_on_stale, code=EXIT_OK,
                                state=state, stale=stale, removed=stale,
                                fresh=fresh, counts=counts)
    head = f"baseline: {where} [{scope}] ({len(baseline.keys)} key(s)"
    if not stale:
        revision.lines = [head + ", 无过期键)", f"  counts: {counts}"]
        return revision
    if pass_on_stale:
        revision.lines = [
            head + f", 过期 {len(stale)} 键 —— 已按 --pass-on-stale 放行（CI 禁止使用）)"
            f"：{_sample_keys(stale)}",
            f"  counts: {counts}",
        ]
        return revision
    revision.code = EXIT_BLOCKING
    revision.lines = [
        head + f", 过期 {len(stale)} 键)",
        f"  -stale {_sample_keys(stale)}",
        "  要么它们已被修好（跑 --write-baseline 让基线缩短），要么它们已被豁免/移出判定范围"
        "（账本要跟着改）；两种修法不同，继续留着永远不对。",
        "  本地应急可加 --pass-on-stale 放行；CI 禁止使用 —— 放行一次，账本就失真一次。",
        f"  counts: {counts}",
    ]
    return revision


def add_ratchet_argument(parser) -> None:
    """装上双向棘轮的开关（试点期只有 g19 调用，2.4.x 再逐门禁接线）。

    刻意**不**并进 ``add_baseline_argument``：那会让其余 16 道门禁的 ``--help``
    声明几个它们并不遵守的开关 —— 一个只是嘴上有的开关，比没有更坏。
    """
    parser.add_argument(
        "--allow-baseline-growth", action="store_true", dest="allow_baseline_growth",
        help="写入基线时纯增长默认拒绝；显式给出才允许（需在提交信息里写明理由）")
    parser.add_argument(
        "--pass-on-stale", action="store_true", dest="pass_on_stale",
        help="查账时存在过期键默认判红；显式给出才放行（仅限本地，CI 下为用法错误）")
    parser.add_argument(
        "--reason", default="", help="写入基线时的理由，随增长一起打印（记进提交信息与评审）")
    parser.add_argument(
        "--ci", action="store_true",
        help="声明处于 CI：人手专用开关（--pass-on-stale / --allow-baseline-growth）在此模式下退 2")


#: 只给人用的开关：CI 下出现即用法错误（ADR-0011 D1.4）。
HUMAN_ONLY_SWITCHES = (("pass_on_stale", "--pass-on-stale"),
                       ("allow_baseline_growth", "--allow-baseline-growth"))


def in_ci(flag: bool = False) -> bool:
    """是否处于 CI。``--ci`` 显式声明优先，否则读 ``CI`` 环境变量。

    值判据写死成几个已知的真值而不是"非空即为真"：`CI=0`、`CI=false` 是有人在
    明确关掉它，把它读成真会把一台开发机的本地放行判成用法错误 —— 误伤比漏判
    更难查，因为它的症状是「本地好好的，CI 红」的反面。
    """
    if flag:
        return True
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def controlled_env(**values: str | None):
    """在一个**完全指定**的环境窗口里执行代码：``None`` = 这个变量必须不存在。

    给自检用。存在的理由不是方便，是一次真实故障（2026-11-25）：自检用例读取了
    宿主的 ``CI``，于是在 GitHub Actions（注入 ``CI=true``）里必红、在开发机里必绿。
    一个结果随宿主环境变化的自检，测的是 runner 而不是门禁。

    ``None`` 走 ``pop`` 而不是 `""`：`""` 与"不存在"在 `in os.environ` 上不同，
    而下游实现随时可能改用存在性判断 —— 让自检表达"没有这个变量"，不是"有这个空变量"。
    """
    previous: dict[str, str | None] = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


#: ``ratchet_selfcheck`` 内部的短名：selfcheck 里写 `_env(CI="1")` 比 `controlled_env(CI="1")` 短。
_env = controlled_env


def ratchet_human_only(args) -> list[str]:
    """列出「在 CI 下不该出现的人手开关」。空列表 = 允许继续。

    单独成函数而不是塞进 ``main``：这样 ``--selfcheck`` 能直接证明这条拦阻真的会响
    （给它一个 Namespace 就够了），而不必真的去 fork 一个带 ``CI=1`` 的子进程。
    """
    if not in_ci(bool(getattr(args, "ci", False))):
        return []
    return [name for attribute, name in HUMAN_ONLY_SWITCHES if getattr(args, attribute, False)]


# --------------------------------------------------------------------------- #
# 双向棘轮的自检（供任何门禁的 --selfcheck 调用）
# --------------------------------------------------------------------------- #


def ratchet_selfcheck(check: SelfCheck) -> None:
    """在门禁的 ``--selfcheck`` 里证明双向棘轮**真的会红**。

    五组负对照在这里一次定义，2.4.x 铺开时每道门禁只要调一次本函数，就有了与
    g19 逐字相同的证明 —— 否则 16 份"我自己也测过"会各自漂移。

    计数器口径：``detects`` 记「机制按设计动了」的断言（拦、放行、收缩都算它动了），
    ``quiet`` 记「不该有声时没有声」的断言。两者都不是"我跑通了"，都有明确的反面。
    """
    import argparse
    import tempfile

    with tempfile.TemporaryDirectory(prefix="gov-ratchet-") as workspace:
        folder = Path(workspace)

        def fixture(name: str, keys: list[str]) -> Baseline:
            path = folder / name
            path.write_text("\n".join(["# baseline"] + keys) + "\n", encoding="utf-8",
                            newline="\n")
            return Baseline(path).load()

        def finding(key: str) -> Finding:
            return Finding(path="a.md", line=1, text=key.split("|")[0], kind="path-ref", key=key)

        a, b = "a.md|aaa|path-ref", "b.md|bbb|path-ref"

        # 1. **纯增长**（旧 ⊂ 新，一条没修）：拒绝写、退出非 0、**字节不变**。
        grown = fixture("grow.txt", [a])
        before = grown.path.read_bytes()
        refused = baseline_write(grown, [finding(a), finding(b)], folder)
        check.detects("ratchet/pure-growth-refused",
                      refused.code != EXIT_OK and grown.path.read_bytes() == before)

        # 2. 同一场景加开关：写入成功，且输出明说"已允许增长"。
        allowed_baseline = fixture("allow.txt", [a])
        allowed = baseline_write(allowed_baseline, [finding(a), finding(b)], folder,
                                 allow_growth=True)
        check.detects("ratchet/growth-opt-in-named",
                      allowed.code == EXIT_OK
                      and b in Baseline(allowed_baseline.path).load().keys
                      and any("已允许增长" in line for line in allowed.lines))

        # 3. **混账**（既修好又新增）：允许写，但账目必须两列都报出来。
        #    这条是 ADR-0011 D1.3 与「任何增长都拒」的分界：正常收账不该被拦，
        #    但读者必须看见"修了 1 条、接管了 1 条"，而不是只看到一个总数。
        mixed_baseline = fixture("mixed.txt", [a, b])
        mixed = baseline_write(mixed_baseline, [finding(a), finding("c.md|ccc|path-ref")], folder)
        check.detects("ratchet/mixed-growth-reported",
                      mixed.code == EXIT_OK
                      and any("-fixed 1" in line and "+new 1" in line for line in mixed.lines))

        # 4. 收缩永远免费（只许因已修而缩短 —— 这正是正转）。
        shrunk = fixture("shrink.txt", [a, b])
        shrink = baseline_write(shrunk, [finding(a)], folder)
        check.detects("ratchet/shrink-allowed",
                      shrink.code == EXIT_OK
                      and Baseline(shrunk.path).load().keys == {a})

        # 5. 过期账单：判红并指名 stale 键。
        stale_baseline = fixture("stale.txt", [a, b])
        stale = baseline_check(stale_baseline, [finding(a)], folder, scanned=1)
        check.detects("ratchet/stale-blocked",
                      stale.code != EXIT_OK and stale.stale == [b])

        # 6. --pass-on-stale：退 0，但 stale 键仍在输出里（放行 ≠ 装作没看见）。
        passed = baseline_check(stale_baseline, [finding(a)], folder, pass_on_stale=True)
        check.detects("ratchet/stale-passed-but-printed",
                      passed.code == EXIT_OK and any(b in line for line in passed.lines))

        # 7. 机读计数行（D1.2）：只有 exit code 的日志里「挂了 12 条」与「一条都没有」一样。
        exact = fixture("counts.txt", [a])
        exact_revision = baseline_check(exact, [finding(a)], folder, scanned=1)
        check.detects("ratchet/counts-line-machine-readable",
                      any("scanned=1 baseline=2 stale=1 new=0" in line for line in stale.lines)
                      and any("scanned=1 baseline=1 stale=0 new=0" in line
                              for line in exact_revision.lines))

        # 8. ADR-0011 D1.4：人手开关在 CI 下是**用法错误**，CI 外才放行。
        #
        #    ⚠ 2026-11-25 CI 事故（本函数自己踩的坑，必须记住）：这一条原先写
        #    `Namespace(ci=False, …)` 就走「应放行」分支，却**继承了宿主环境的 CI**。
        #    GitHub Actions 全局注入 `CI=true` ⇒ 这一支在 CI 里永远走「被拒」⇒
        #    `detects` 判失败 ⇒ SELFCHECK 报 `missed injected defect` ⇒ 门禁退 1。
        #    本地永远复现不出来（本地没有 CI），CI 上 100% 红 —— 自检测的成了 runner。
        #
        #    规矩：**自检用例显式设定它要测的环境，不读取宿主的。**
        #    三条断言各自钉死环境：① 显式 --ci；② 只靠 CI=1；③ 环境里没有 CI。
        ambient = os.environ.get("CI")
        with _env(CI=None):
            by_flag = ratchet_human_only(
                argparse.Namespace(ci=True, pass_on_stale=True,
                                   allow_baseline_growth=False)) == ["--pass-on-stale"]
        with _env(CI="1"):
            by_env = ratchet_human_only(
                argparse.Namespace(ci=False, pass_on_stale=True,
                                   allow_baseline_growth=False)) == ["--pass-on-stale"]
        with _env(CI=None):
            local_only = ratchet_human_only(
                argparse.Namespace(ci=False, pass_on_stale=True,
                                   allow_baseline_growth=True)) == []
        check.detects("ratchet/human-only-in-ci-rejected", by_flag and by_env and local_only)

        # 9. 环境窗口必须**关得回去**：一个泄漏出去的 CI=1 会让后面所有 selfcheck
        #    用例（以及同进程其它门禁）悄悄换一种行为 —— 比没有窗口更难查。
        check.detects("ratchet/env-window-restored", os.environ.get("CI") == ambient)

        # 10. 账本与现状一致：退 0，且没有任何过期噪声。
        #    断言不能只搜"过期"二字：干净时的输出是「…, 无过期键)」，搜子串会自伤
        #    （selfcheck 立刻报 misreport）—— 噪声的定义是**喊有 stale**，不是提到这个词。
        quiet = baseline_check(exact, [finding(a)], folder)
        check.quiet("ratchet/exact-match-silent",
                    quiet.code != EXIT_OK or bool(quiet.stale)
                    or any("-stale" in line or "STALE" in line for line in quiet.lines))


# --------------------------------------------------------------------------- #
# self-check harness
# --------------------------------------------------------------------------- #


@dataclass
class SelfCheck:
    """Assertions that prove a gate catches defects and stays quiet when clean."""

    gate: str
    caught: int = 0
    clean: int = 0
    misreports: int = 0
    failures: list[str] = field(default_factory=list)

    def detects(self, label: str, condition: bool) -> None:
        """Assert the gate caught an injected defect."""
        if condition:
            self.caught += 1
        else:
            self.failures.append(f"missed injected defect: {label}")

    def quiet(self, label: str, flagged: bool) -> None:
        """Assert the gate stayed quiet on clean input."""
        if flagged:
            self.misreports += 1
            self.failures.append(f"false positive on clean input: {label}")
        else:
            self.clean += 1

    def finish(self) -> int:
        # 紧凑分隔符：契约（RULES.md §自证有效）写的是 `SELFCHECK {"caught":N,"clean":M,"misreports":K}`，
        # json.dumps 的默认分隔符会带空格，与契约文本不一致。格式统一后元门禁才能按契约严格机检。
        payload = json.dumps({"caught": self.caught, "clean": self.clean, "misreports": self.misreports},
                             separators=(",", ":"))
        for failure in self.failures:
            print(f"  FAIL {failure}")
        print(f"SELFCHECK {payload}")
        if self.failures or self.caught == 0 or self.clean == 0:
            print(f"selfcheck failed for {self.gate}: a gate must catch at least one injected defect "
                  "and stay quiet on at least one clean sample")
            return EXIT_BLOCKING
        print(f"selfcheck ok for {self.gate}: caught {self.caught}, quiet on {self.clean}, misreports 0")
        return EXIT_OK


class Sandbox:
    """A throwaway git work tree the self-check injects fixtures into."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="gov-selfcheck-"))
        run_git(["init", "-q"], self.path)

    def write(self, relative: str, content: str, track: bool = True) -> None:
        """Write one fixture; ``track=False`` leaves it out of the index on purpose."""
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        if track:
            run_git(["add", "--", relative], self.path)

    def close(self) -> None:
        # `git init` writes .git/objects as read-only on Windows. A bare
        # `rmtree(..., ignore_errors=True)` swallowed WinError 5 and leaked one
        # work tree per self-check (21k+ dirs in %TEMP%). Clear the read-only
        # bit on the way down, then verify the directory is actually gone.
        if not self.path.exists():
            return

        def _make_writable(func, path, _exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except OSError:
                pass

        shutil.rmtree(self.path, onerror=_make_writable)
        if self.path.exists():
            # Never drop a cleanup failure silently: that is how the leak hid.
            print(f"warning: selfcheck sandbox could not be removed: {self.path}",
                  file=sys.stderr)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def run_gate(gate_main, argv: list[str] | None = None) -> int:
    """Run a gate entry point, mapping GateError onto its declared exit code."""
    configure_stdio()
    try:
        return int(gate_main(sys.argv[1:] if argv is None else argv))
    except GateError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ENV
