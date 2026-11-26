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

import datetime as dt
import hashlib
import json
import os
import platform
import re
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


# --------------------------------------------------------------------------- #
# baseline file format (v2): count:<n> quota + header metadata
# --------------------------------------------------------------------------- #
#
# 格式（ADR-0011 §D1.1 键格式 / §D1.3 头部元数据，2026-11-25 起）：
#
#     # format: 2
#     # written: 2026-11-25T15:40:12Z
#     # commit: 0e8270d1fadd…
#     # reason: <原话>
#     count:3 docs/x.md|739784d1e1c12436|path-ref
#     count:1 docs/y.md|83eb8c25f756be0b|path-ref
#
# 两个语义，各解一个旧格式解决不了的病：
#
# * **``count:<n>`` 是额度，不是"有几条"**（借鉴 ESLint bulk suppressions）：同一键
#   最多抑制 ``n`` 条；实际条数**超过**额度时，整组**全量报出**（不是只报差额）。
#   「同一个缺陷同一句话出现 5 处」按旧的一行一键写法只会留下 1 条，另外 4 条
#   被顺手藏进默认；额度写 3 时，第 4、5 条一出现就该红，且要红得看得见全部 5 条
#   —— 只报 2 条差额会让人以为"只多了 2 条"。
# * **头部三行是账本的签名**：谁在什么时候、基于哪个提交、为什么冻结。旧基线只是一串
#   键，「这批存量是谁批的」只能靠外部文档；文档和文件一旦漂移，账本就成了自证。
#
# 向后兼容（硬要求，不是客气话）：**裸键 = ``count:1``**。为什么这是忠实解释而不是近似：
# 2026-11-25 迁移前逐份实测，13 份核内基线里每个键当下都恰好命中 1 条
# （``g19`` 那 32 个键/71 条命中的情形基线是空的，不含任何键），所以把裸键读成
# ``count:1`` 不会改变任何一份基线的红/绿结论。
#
# 为什么旧格式**只 WARN 不判红**：格式升级那天把全部历史基线一起变红，红的原因是
# "文件老"而不是"账错了" —— 这恰好训练人把红色当噪音。新写入的（v2）缺头则判红：
# 机器自己写的文件缺自己的签名，那才是真缺陷。

#: 新格式必须带的三个元数据字段（缺任一 ⇒ v2 判红）。
HEADER_FIELDS = ("written", "commit", "reason")
#: 头部里的格式版本。**可选但必须对**：写了就得是 ``2``；不写也认（三个元数据齐即 v2）。
HEADER_FORMAT_KEY = "format"
BASELINE_FORMAT = "2"
#: 键的行格式：``count:<n> <key>``；旧格式（裸键）按 ``count:1`` 解释。
COUNT_ENTRY = re.compile(r"^count:(\d+)\s+(\S.*)$")
#: 头部一行：``# <key>: <value>``。值里允许冒号（"reason: 因为 x: y" 不该被截断）。
HEADER_LINE = re.compile(r"^#\s*([A-Za-z][A-Za-z_-]*)\s*:\s*(.*)$")
#: 合法 commit 取值：7~40 位十六进制，或 ``none``（写基线时没有 git 可问）。
COMMIT_SHAPE = re.compile(r"^(?:[0-9a-f]{7,40}|none)$")
#: ``reason`` 至少这么长 —— ``# reason: -`` 不是一句可核的理由。
MIN_REASON_CHARS = 8
#: 写基线时没给 ``--reason`` 的占位文本。**非空是刻意的**：空值会让下一次 ``--check``
#: 直接判红（缺 reason），而那是一次正常写入造成的自杀式账本；占位文本让账本完整，
#: 同时让读的人一眼看见「这一条没写理由」。
REASON_PLACEHOLDER = "未提供 --reason（本次写基线未声明理由）"


class Baseline:
    """一个冻结的键集合 + 每个键的抑制额度 + 头部元数据。

    ``keys`` 仍是键集合（19 道门禁与本模块的其余函数都按它读）；``counts`` 多带了
    额度，两者始终自洽（``set(counts) == keys``）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: set[str] = set()
        #: key -> 声明的抑制额度（裸键 = 1）
        self.counts: dict[str, int] = {}
        #: 头部 ``# key: value`` 解析结果（小写键）
        self.header: dict[str, str] = {}
        #: body 里出现过 ``count:<n>`` 条目 ⇒ 这份文件是新写的（v2 身份证据之一）
        self.v2_body = False
        #: 同一键写了多行（账本歧义，不是"两条"）
        self.duplicates: list[str] = []
        #: 解析不出键的 body 行（``count:0``、空键等）
        self.bad_entries: list[str] = []

    # -- 读 ---------------------------------------------------------------- #
    def _reset(self) -> None:
        """把解析状态清空。``load()`` 必须是**幂等重读**，不是"往已有状态上追加"。

        为什么这不是洁癖：``write()`` 写完会把账写回内存（见那里的注释），而各门禁的
        自检紧接着调 ``load()`` —— 追加语义下，文件里每个键都会被当成"同一键写了多行"
        并按重复键判缺陷，g01 的 ``--selfcheck`` 就是这样整条哑掉、再由 g00 的契约自检
        报成 `self-check printed no SELFCHECK summary`。同一份文件读两遍必须得到同一个
        结论，否则"重读"这件事本身成了判据的一部分。
        """
        self.keys = set()
        self.counts = {}
        self.header = {}
        self.v2_body = False
        self.duplicates = []
        self.bad_entries = []

    def load(self) -> "Baseline":
        self._reset()
        if self.path.is_file():
            # gov-python: allow —— 调的是本文件的 read_text 包装，编码已写死。
            text = read_text(self.path, limit_bytes=4 * 1024 * 1024) or ""
            for raw in text.splitlines():
                entry = raw.strip()
                if not entry:
                    continue
                if entry.startswith("#"):
                    match = HEADER_LINE.match(entry)
                    if match:
                        # setdefault：同一字段写两遍时以第一遍为准，不静默改写账本
                        self.header.setdefault(match.group(1).lower(), match.group(2).strip())
                    continue
                match = COUNT_ENTRY.match(entry)
                if match:
                    self.v2_body = True
                    count, key = int(match.group(1)), match.group(2).strip()
                    if count < 1 or not key:
                        self.bad_entries.append(entry)
                        continue
                else:
                    count, key = 1, entry   # 旧格式：裸键按 count:1 读
                if key in self.counts:
                    self.duplicates.append(key)
                    self.counts[key] = max(self.counts[key], count)
                    continue
                self.counts[key] = count
                self.keys.add(key)
        return self

    # -- 格式身份 ---------------------------------------------------------- #
    def format_state(self) -> str:
        """``legacy``（v1）/ ``v2`` / ``v2-defective``。

        身份靠两条硬证据，不靠"文件名里有没有 written"这类脆弱迹象：
        ① 头部解析出 format/written/commit/reason 任意一个；② body 出现 ``count:`` 条目。
        两条都不成立 ⇒ v1（历史基线，只 WARN）。
        """
        declares = any(field in self.header for field in (HEADER_FORMAT_KEY, *HEADER_FIELDS))
        if not (declares or self.v2_body):
            return "legacy"
        return "v2-defective" if self.header_defects() else "v2"

    def header_defects(self) -> list[str]:
        """新格式头部的缺陷清单；空列表 = 头完整。"""
        defects: list[str] = []
        version = self.header.get(HEADER_FORMAT_KEY)
        if version is not None and version.strip() != BASELINE_FORMAT:
            defects.append(f"# {HEADER_FORMAT_KEY}: {version} —— 只认 {BASELINE_FORMAT}")
        written = self.header.get("written", "")
        if not written:
            defects.append("缺 # written: <ISO8601>")
        else:
            try:
                moment = dt.datetime.fromisoformat(written.replace("Z", "+00:00"))
            except ValueError:
                defects.append(f"# written: {written} 不是 ISO8601")
            else:
                if moment.tzinfo is None:
                    defects.append(f"# written: {written} 没有时区 —— 无法确定是哪一刻写的")
        commit = self.header.get("commit", "").strip().lower()
        if not commit:
            defects.append("缺 # commit: <sha>（无 git 可问时写 none）")
        elif not COMMIT_SHAPE.match(commit):
            defects.append(f"# commit: {commit} 不是 7~40 位十六进制，也不是 none")
        reason = self.header.get("reason", "").strip()
        if not reason:
            defects.append("缺 # reason: <原话>")
        elif len(reason) < MIN_REASON_CHARS:
            defects.append(f"# reason 太短（少于 {MIN_REASON_CHARS} 字符）—— 不是一句可核的理由")
        if self.duplicates:
            defects.append(f"同一键写了多行：{_sample_keys(sorted(set(self.duplicates)))}")
        if self.bad_entries:
            defects.append(f"解析不出键的行：{_sample_keys(self.bad_entries)}")
        return defects

    # -- 判 ---------------------------------------------------------------- #
    def assert_header_ok(self) -> None:
        """新格式（v2）但头不全 ⇒ 抛 ``GateError(EXIT_BLOCKING)``；旧格式不抛。

        为什么把判红放在**读判这一步**而不是每道门禁里各判一次：19 道门禁的查账
        都要走 ``filter_new``，一处实现即全部生效 —— 否则这件事只有试点门禁管，
        其余 18 道悄悄读一份没有签名的账本。而 ``--write-baseline`` 不经过这里，
        所以"缺头 ⇒ 判红"不会连唯一的修法一起堵死。

        旧格式（v1）刻意不抛：格式升级那天把历史基线一起变红，红的原因是"文件老"，
        不是"账错了"。那种红会训练人忽略红色。
        """
        if self.format_state() != "v2-defective":
            return
        raise GateError(
            f"基线 {self.path.as_posix()} 是新格式但头不完整："
            + "；".join(self.header_defects())
            + "。修法：跑 --write-baseline 重写（会带上头），或手工补齐"
            " written/commit/reason 三行；补的时候别删 count: 前缀，那是额度不是注释。",
            EXIT_BLOCKING)

    def filter_new(self, findings: list[Finding], *, strict: bool = True) -> list[Finding]:
        """按额度抑制；**额度不够时整组全报**（ESLint 口径）。

        为什么不是"报差额"：额度 3、实际 5 时，报 2 条读起来像"只多了 2 条"，
        但真正要人看的是"这句话现在出现在 5 个地方" —— 差额口径把定位信息
        留给了一个已经不完整的视图。

        ``strict=False`` 只给"要把缺陷积累成报告而不是抛异常"的调用方（``baseline_check``
        与 g19 的 main）：它们自己会把缺陷写进退出码，抛异常反而会把要打印的行丢掉。
        """
        if strict:
            self.assert_header_ok()
        occurrences = Counter(finding.key for finding in findings)
        over = {key for key, count in occurrences.items() if count > self.counts.get(key, 0)}
        return [finding for finding in findings if finding.key in over]

    def stale(self, findings: list[Finding]) -> list[str]:
        """基线里有、当下一条都不命中的键（过期账单）。"""
        live = {finding.key for finding in findings}
        return sorted(self.keys - live)

    def underrun(self, findings: list[Finding]) -> list[str]:
        """额度虚高：键还在命中，但条数少于声明的额度（账本写宽了）。

        不判红：额度写宽的成因多半是"原来有 3 处、修掉 2 处"，那是棘轮在正转；
        但必须说出来 —— 额度只增不减，久了就变成无限豁免。
        """
        live = Counter(finding.key for finding in findings)
        return sorted(f"{key} (count:{self.counts[key]} > 实际 {live[key]})"
                      for key in self.keys if 0 < live.get(key, 0) < self.counts[key])

    # -- 写 ---------------------------------------------------------------- #
    def write(self, findings: list[Finding], *, root: Path | None = None, reason: str = "",
              written: str | None = None) -> None:
        """把当下 findings 冻成**新格式**基线（count 额度 + 头部元数据）。

        ``root`` 只用来问 HEAD；不给就从基线文件的落点向上找 ``.git``，再找不到就写
        ``# commit: none``（合法值，不是缺字段）。``written`` 只为可复现的迁移脚本留口，
        正常调用一律取当下 UTC 时刻。
        """
        counts = Counter(finding.key for finding in findings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        base = root if root is not None else discover_root(self.path)
        body = baseline_header_lines(
            written=written or utc_stamp(), commit=commit_of(base), reason=reason)
        body.extend(f"count:{counts[key]} {key}" for key in sorted(counts))
        self.path.write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")
        # 写回内存状态：紧接着 load() 的调用方（各门禁的自检就这样）不该看到旧账。
        self.keys, self.counts, self.v2_body = set(counts), dict(counts), True
        self.duplicates, self.bad_entries = [], []
        self.header = {match.group(1).lower(): match.group(2).strip()
                       for line in body if (match := HEADER_LINE.match(line))}


#: 头部之后的 body 说明行（默认：govlib 的 ``count:<n> <key>`` 格式）。
COUNT_BODY_NOTE = ("# count:<n> <key> —— 抑制额度：同一键最多抑制 n 条，"
                   "超量则整组全报（ESLint 口径）。")
REFRESH_NOTE = "# 刷新前逐条复核：python governance/gates/<gate>.py --no-baseline --list-all"


def baseline_header_lines(*, written: str, commit: str, reason: str,
                          body_note: str = COUNT_BODY_NOTE) -> list[str]:
    """新格式的头部行。**所有写入点共用这一处措辞**（g15 也调它）。

    写法上有意单向：字段名固定、值里换行被压平（一行一字段是解析的前提），
    ``reason`` 缺省时写占位文本而不是空值（空值 = 下一次 --check 判红）。
    ``body_note`` 只替换"body 是什么格式"那一句：有一份基线（g15）用的是自有的
    ``code|message`` 键，写 ``count:`` 会在它自己的文件里说谎。
    """
    why = " ".join((reason or "").split()) or REASON_PLACEHOLDER
    return [
        f"# {HEADER_FORMAT_KEY}: {BASELINE_FORMAT}",
        f"# written: {written}",
        f"# commit: {commit}",
        f"# reason: {why}",
        body_note,
        REFRESH_NOTE,
    ]


def utc_stamp(moment: dt.datetime | None = None) -> str:
    """ISO8601 UTC 时刻（秒级，``Z`` 后缀）。带时区是刻意的：无时区的时刻跨机器不可比。"""
    stamp = moment or dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def commit_of(root: Path | None) -> str:
    """写基线时的 HEAD；问不到就 ``none``（合法取值，不是缺字段）。"""
    if root is None:
        return "none"
    try:
        sha = run_git(["rev-parse", "HEAD"], Path(root)).strip().lower()
    except (GateError, OSError):
        return "none"
    return sha if COMMIT_SHAPE.match(sha) else "none"


def discover_root(path: Path) -> Path | None:
    """从基线文件的落点向上找工作树（写头部要问 HEAD，但写基线的人不一定传 root）。"""
    for candidate in (path.parent, *path.parent.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


@dataclass
class BaselineHeader:
    """一份基线文件"头"的判定结果。"""

    #: legacy（旧格式，只 WARN）/ v2（完整）/ v2-defective（新格式但缺元数据 ⇒ 判红）
    state: str
    header: dict[str, str] = field(default_factory=dict)
    defects: list[str] = field(default_factory=list)
    #: 旧格式要打的那句 WARN；v2 时为空
    warning: str = ""


def baseline_header_verdict(path: Path, root: Path | None = None) -> BaselineHeader:
    """判一份基线文件的头。g19（govlib 口径）与 g15（自有 Report）共用这一处。

    兼容口径写在返回值的 ``state`` 里，而不是让每个调用方各判一次：
    一处实现，一份措辞，一种红/绿。
    """
    baseline = Baseline(path).load()
    state = baseline.format_state()
    verdict = BaselineHeader(state=state, header=dict(baseline.header),
                             defects=baseline.header_defects() if state != "legacy" else [])
    if state == "legacy":
        where = display_path(path, root) if root is not None else path.as_posix()
        verdict.warning = (f"WARN 基线 {where} 是旧格式（无 # written/commit/reason 头）："
                           "只提示、不判红 —— 历史基线不该因为文件老而变红。"
                           "下次 --write-baseline 会自动带上头。")
    return verdict


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
    new_counts = Counter(finding.key for finding in findings)
    new_keys = set(new_counts)
    added = sorted(new_keys - baseline.keys)
    removed = sorted(baseline.keys - new_keys)
    # 同一键的**条数**也会涨（多了一处同样的缺陷）：那也是"把新问题藏进默认"，
    # 而且比新增键更安静 —— 键集一个字节没变，旧实现完全看不见这件事。
    raised = sorted(key for key, count in new_counts.items()
                    if baseline.counts.get(key, 0) and count > baseline.counts[key])
    lowered = sorted(key for key in baseline.keys
                     if baseline.counts.get(key, 0) > new_counts.get(key, 0))
    revision = BaselineRevision(ok=False, code=EXIT_BLOCKING, state=state,
                                added=added, removed=removed)
    where = display_path(baseline.path, root)
    scope = display_scope(baseline.path, root)
    # 纯增长：旧键全在（一个没修好）、没有哪个键的额度被下调、而且总有一处变多
    # （新键或额度上调）。``{} ⊆ X`` 恒真，所以零键基线也必须单独判 —— 它最松。
    pure_growth = (state != BASELINE_ABSENT and baseline.keys <= new_keys
                   and not lowered and bool(added or raised))

    if pure_growth and not allow_growth:
        existing = (f"既有基线 {len(baseline.keys)} 键" if state == BASELINE_PRESENT
                    else "既有基线为空（存量已清零的记录）")
        revision.lines = [
            f"refused to write baseline {baseline.path.resolve()}",
            f"  基线 {where} [{scope}] {existing}；本次是**纯增长**：新增 {len(added)} 键、"
            f"额度上调 {len(raised)} 键、修好 0 键（{len(baseline.keys)} → {len(new_keys)}）："
            f"{_sample_keys(added or raised)}",
            "  基线只许因「已修」而缩短：把新问题冻进默认，等于让它以后永远不再被拦"
            " —— 比不加基线更坏。额度上调同理（同一个键多出几处，同样是在藏新问题）。",
            "  若这些确实是本次要一并接管的存量（已逐条复核过），显式加"
            " --allow-baseline-growth（建议同时给 --reason \"...\"），并在提交信息里写明理由。",
            "  未写入：基线文件保持原样（字节未变）。",
        ]
        return revision

    baseline.write(findings, root=root, reason=reason)
    revision.ok = True
    revision.code = EXIT_OK
    revision.lines = [
        f"wrote baseline {baseline.path.resolve()} ({len(new_keys)} key(s), "
        f"{sum(new_counts.values())} 条额度)"
        f" [{scope}]" + (" [首次建账]" if state == BASELINE_ABSENT else ""),
    ]
    if state != BASELINE_ABSENT:
        # 两列账目（D1.3）：一次写到底是「修了几条」还是「接管了几条」，
        # 不许只有一个总数 —— 总数相等的一删一增，读起来和"什么都没变"一样。
        revision.lines.append(f"  -fixed {len(removed)}  +new {len(added)}")
        if raised or lowered:
            # 额度变化必须单独成列：键集没变而条数变了，是最容易被读成"什么都没变"的形态。
            revision.lines.append(
                f"  ^quota +{len(raised)} / -{len(lowered)}"
                + (f"（上调：{_sample_keys(raised)}）" if raised else "")
                + (f"（下调：{_sample_keys(lowered)}）" if lowered else ""))
        revision.lines.append(
            f"  header: written/commit/reason 已写入（commit={commit_of(root)}）"
            if reason.strip() else
            f"  header: 已写入，但**未给 --reason**（记为占位文本）；提交信息里必须写明理由。")
    if pure_growth:
        # 走到这里的纯增长只能是 allow_growth 放行的：放行也必须说出来 ——
        # 一次被允许的增长，日志里留不下痕迹就还是静默增长。
        revision.lines.append(
            f"  已允许增长：+{len(added)} 键 / ^quota {len(raised)} 键（--allow-baseline-growth）"
            f"：{_sample_keys(added or raised)}")
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
    fresh_findings = (baseline.filter_new(findings, strict=False) if state != BASELINE_ABSENT
                      else list(findings))
    fresh = sorted({finding.key for finding in fresh_findings})
    underrun = baseline.underrun(findings) if state != BASELINE_ABSENT else []
    # ``new`` 数的是**条数**（= 真正会阻断的 finding 数），不是键数。
    # 旧实现数键：g19 实测 ``blocking 71`` 却打印 ``new=32`` —— 两个数在同一行里
    # 互相否认，而人只会记住小的那个。``keys=`` 保留旧的量，谁要键数谁取它。
    counts = (f"scanned={len(findings) if scanned is None else scanned}"
              f" baseline={len(baseline.keys)} stale={len(stale)} new={len(fresh_findings)}"
              f" keys={len(fresh)}")
    if state == BASELINE_ABSENT:
        return BaselineRevision(
            ok=True, code=EXIT_OK, state=state, fresh=fresh, counts=counts,
            lines=[f"baseline: {where} 不存在 ⇒ 棘轮失效，本次按全量判定"
                   "（要冻结存量用 --write-baseline）",
                   f"  counts: {counts}"])
    header = BaselineHeader(state=baseline.format_state(), header=dict(baseline.header),
                            defects=baseline.header_defects())
    if header.state == "legacy":
        header = baseline_header_verdict(baseline.path, root)
    head = f"baseline: {where} [{scope}] ({len(baseline.keys)} key(s)"
    if not stale:
        head += f", 额度 {sum(baseline.counts.values())} 条)"
    else:
        head += f", 过期 {len(stale)} 键)"
    blocked = bool(stale) and not pass_on_stale
    defective = header.state == "v2-defective"
    revision = BaselineRevision(ok=not blocked and not defective,
                                code=EXIT_BLOCKING if (blocked or defective) else EXIT_OK,
                                state=state, stale=stale, removed=stale,
                                fresh=fresh, counts=counts)
    lines = [head]
    # 头部的两种非绿形态，措辞分开（它们是两种不同的病，修法也不同）：
    #   legacy      —— 文件老：只 WARN，绝不判红（否则历史基线在格式升级当天全红）
    #   v2-defective—— 机器自己写的文件缺自己的签名：判红
    if header.warning:
        lines.append(f"  {header.warning}")
    if defective:
        lines.append("  BLOCK 基线头不完整（新格式必须带 written/commit/reason）："
                     + "；".join(header.defects))
        lines.append("  修法：跑 --write-baseline 重写（会带上头），或手工补齐这三行。"
                     "手补时别删 count: 前缀 —— 那是额度，不是注释。")
    if stale and pass_on_stale:
        lines.append(f"  -stale {_sample_keys(stale)}（已按 --pass-on-stale 放行，CI 禁止使用）")
    elif stale:
        lines.append(f"  -stale {_sample_keys(stale)}")
        lines.append("  要么它们已被修好（跑 --write-baseline 让基线缩短），"
                     "要么它们已被豁免/移出判定范围（账本要跟着改）；"
                     "两种修法不同，继续留着永远不对。")
        lines.append("  本地应急可加 --pass-on-stale 放行；CI 禁止使用 —— 放行一次，账本就失真一次。")
    if underrun:
        lines.append(f"  under-quota {len(underrun)} 键额度虚高（键还在，条数少了）："
                     f"{_sample_keys(underrun)}")
        lines.append("  不判红：修掉几处是棘轮在正转。但额度只增不减会变成无限豁免 —— "
                     "下次 --write-baseline 会按实际条数收窄。")
    lines.append(f"  counts: {counts}")
    revision.lines = lines
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
            """**旧格式**夹具：一行一键、无头（v1）。"""
            path = folder / name
            path.write_text("\n".join(["# baseline"] + keys) + "\n", encoding="utf-8",
                            newline="\n")
            return Baseline(path).load()

        def legacy_file(name: str, keys: list[str]) -> Baseline:
            return fixture(name, keys)

        def v2_file(name: str, entries: dict[str, int], *, header: bool = True) -> Baseline:
            """**新格式**夹具：``count:<n> <key>`` + 头（``header=False`` 用来造缺头件）。"""
            path = folder / name
            lines = (baseline_header_lines(written="2026-11-25T00:00:00Z", commit="0e8270d",
                                           reason="selfcheck 夹具：内容未变")
                     if header else [])
            lines += [f"count:{count} {key}" for key, count in sorted(entries.items())]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
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

        # ---- 以下四条是 2.4.x「格式迁移」的自证（ADR-0011 D1.1/D1.3） ---------- #
        # 11. **旧格式仍可读，且与同内容的 v2 逐字同结论**。
        #     这是「迁移不为了改结论」的自证：裸键读成 count:1，红绿不变，只多一句 WARN。
        #     「红」这一侧用**过期账单**造：a 仍在命中、b 已不命中 ⇒ 两边的码与计数必须逐字相同。
        legacy_same = legacy_file("legacy-same.txt", [a, b])
        v2_same = v2_file("v2-same.txt", {a: 1, b: 1})
        legacy_red = baseline_check(legacy_same, [finding(a)], folder, scanned=2)
        v2_red = baseline_check(v2_same, [finding(a)], folder, scanned=2)
        legacy_ok = baseline_check(legacy_same, [finding(a), finding(b)], folder, scanned=2)
        v2_ok = baseline_check(v2_same, [finding(a), finding(b)], folder, scanned=2)
        legacy_verdict = baseline_header_verdict(legacy_same.path, folder)
        v2_verdict = baseline_header_verdict(v2_same.path, folder)
        check.detects("ratchet/legacy-reads-as-count-1",
                      legacy_same.counts == {a: 1, b: 1}
                      and legacy_red.code == v2_red.code == EXIT_BLOCKING
                      and legacy_ok.code == v2_ok.code == EXIT_OK
                      and legacy_red.counts == v2_red.counts
                      and legacy_ok.counts == v2_ok.counts
                      and legacy_verdict.state == "legacy" and v2_verdict.state == "v2"
                      and any("WARN" in line for line in legacy_red.lines)
                      and not any("WARN" in line for line in v2_red.lines))

        # 12. **新写入的基线缺头 ⇒ 判红**。与 11 是同一条规则的两种形态，必须分开证：
        #     「文件老」只 WARN，「机器自己写的缺签名」才判红 —— 混成一条，
        #     要么历史基线在升级当天全红，要么新基线缺头也没人管。
        headless = v2_file("headless.txt", {a: 1}, header=False)
        headless_verdict = baseline_header_verdict(headless.path, folder)
        headless_rev = baseline_check(Baseline(headless.path).load(), [finding(a)], folder, scanned=1)
        check.detects("ratchet/v2-without-header-blocked",
                      headless_verdict.state == "v2-defective"
                      and headless_rev.code == EXIT_BLOCKING
                      and any("written" in line for line in headless_rev.lines))

        # 13. count:3 而实际 5 条 ⇒ **全量报出 5 条**（不是只报 2 条差额）；
        #     额度内（3 条）必须安静 —— 两条一起证，否则"永远全报"也能让上半条通过。
        quota = v2_file("quota.txt", {a: 3})
        five = [finding(a) for _ in range(5)]
        three = [finding(a) for _ in range(3)]
        over = baseline_check(quota, five, folder, scanned=5)
        within = baseline_check(quota, three, folder, scanned=3)
        check.detects("ratchet/quota-over-reports-all",
                      len(quota.filter_new(five)) == 5
                      and "new=5" in over.counts and "keys=1" in over.counts
                      and "new=0" in within.counts and len(quota.filter_new(three)) == 0)
        check.quiet("ratchet/quota-within-silent",
                    within.code != EXIT_OK or bool(within.stale) or bool(within.fresh))

        # 14. 额度上调也**算纯增长**：键集一个字节没变、只是同一键多了几处，
        #     旧实现对此完全无感 —— 那正是"把新问题藏进默认"最安静的形态。
        quota_baseline = v2_file("quota-grow.txt", {a: 1})
        quota_before = quota_baseline.path.read_bytes()
        quota_refused = baseline_write(quota_baseline, [finding(a), finding(a)], folder)
        check.detects("ratchet/quota-growth-refused",
                      quota_refused.code == EXIT_BLOCKING
                      and quota_baseline.path.read_bytes() == quota_before
                      and any("额度上调 1 键" in line for line in quota_refused.lines))

        # 15. **重读必须幂等**：`write()` 会把账写回内存（紧接着 load() 的调用方不该
        #     看到旧账），而各门禁的自检正是 `write()` → `load()` → `filter_new()`。
        #     解析器若"往已有状态上追加"，文件里每个键都会被当成「同一键写了多行」⇒
        #     整条 --selfcheck 哑掉，再由 g00 的契约自检报成
        #     `self-check printed no SELFCHECK summary`（2026-11-26 真实发生过：
        #     g01 的 --selfcheck 就是这样整条哑的，而 g00 因此从绿转红）。
        reloadable = v2_file("reload.txt", {a: 2, b: 1})
        first_read = dict(reloadable.counts)
        reloadable.load()
        check.detects("ratchet/reload-is-idempotent",
                      reloadable.counts == first_read and not reloadable.duplicates
                      and reloadable.format_state() == "v2")
        roundtrip = v2_file("roundtrip.txt", {})
        roundtrip.write([finding(a), finding(a), finding(b)], root=folder,
                        reason="selfcheck 夹具：write→load 往返")
        roundtrip.load()
        check.detects("ratchet/write-then-load-keeps-counts",
                      roundtrip.counts == {a: 2, b: 1} and not roundtrip.duplicates
                      and roundtrip.format_state() == "v2")


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
